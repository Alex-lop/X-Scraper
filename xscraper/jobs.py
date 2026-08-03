from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import replace

from .errors import CollectionCancelled, ScraperError
from .models import CollectionRequest, JobStatus
from .providers.base import CollectionProvider
from .sentiment import analyze
from .storage import Storage

logger = logging.getLogger(__name__)


class JobService:
    def __init__(
        self, storage: Storage, provider: CollectionProvider, *, start_worker: bool = True
    ):
        self.storage = storage
        self.provider = provider
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._lock_path = storage.path.with_name(f"{storage.path.name}.worker.lock")
        self._lock_owned = False
        self._shutdown = False
        if start_worker:
            self.start()

    def _acquire_process_lock(self) -> None:
        for _ in range(2):
            try:
                descriptor = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    owner = self._lock_path.read_text().strip().split(" ", 1)[0]
                    owner_pid = int(owner)
                except (OSError, ValueError):
                    try:
                        age = time.time() - self._lock_path.stat().st_mtime
                    except OSError:
                        age = 0
                    if age < 5:
                        raise RuntimeError(
                            "Another xscraper worker is acquiring the process lock."
                        ) from None
                    self._lock_path.unlink(missing_ok=True)
                    continue
                try:
                    os.kill(owner_pid, 0)
                except ProcessLookupError:
                    self._lock_path.unlink(missing_ok=True)
                    continue
                except PermissionError:
                    pass
                raise RuntimeError(
                    "Another xscraper worker process is already using this database. "
                    "The local MVP supports one server process."
                ) from None
            with os.fdopen(descriptor, "w") as lock_file:
                lock_file.write(f"{os.getpid()} {self.worker_id}\n")
            self._lock_owned = True
            atexit.register(self.shutdown)
            return
        raise RuntimeError("Could not acquire the xscraper worker process lock.")

    def _release_process_lock(self) -> None:
        if not self._lock_owned:
            return
        try:
            contents = self._lock_path.read_text()
            if self.worker_id in contents:
                self._lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._lock_owned = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self._lock_owned:
            self._acquire_process_lock()
        try:
            recovered_jobs = self.storage.recover_jobs()
        except Exception:
            self._release_process_lock()
            raise
        self._thread = threading.Thread(target=self._worker, name="xscraper-worker", daemon=True)
        self._thread.start()
        for job_id in recovered_jobs:
            self.enqueue(job_id)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._stop_event.set()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5)
        if not self._thread or not self._thread.is_alive():
            self._release_process_lock()

    def submit(self, request: CollectionRequest) -> str:
        if self._shutdown:
            raise RuntimeError("Job service is shutting down.")
        job_id = self.storage.create_job(request)
        self.enqueue(job_id)
        return job_id

    def enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)

    def cancel(self, job_id: str) -> bool:
        return self.storage.request_cancel(job_id)

    def resume(self, job_id: str) -> bool:
        if not self.storage.resume_job(job_id):
            return False
        self.enqueue(job_id)
        return True

    def run_once(self, job_id: str) -> None:
        job = self.storage.claim_job(job_id, self.worker_id)
        if not job:
            return

        request = CollectionRequest.from_dict(job["request"])
        remaining = request.max_tweets - int(job["collected_count"])
        if remaining <= 0:
            self.storage.finish_job(
                job_id,
                job["warnings"],
                completion_reason="target_reached",
            )
            return
        active_request = replace(request, max_tweets=remaining)

        def on_batch(tweets, cursor, cursor_context, raw_posts_seen):
            enrichments = None
            if request.analyze_sentiment:
                enrichments = {}
                for tweet in tweets:
                    label, score, analyzer_version = analyze(tweet.text)
                    enrichments[tweet.tweet_id] = (label, score, "vader", analyzer_version)
            return self.storage.add_tweets(
                job_id,
                tweets,
                cursor,
                cursor_context=cursor_context,
                raw_posts_seen=raw_posts_seen,
                enrichments=enrichments,
            )

        try:
            summary = self.provider.collect(
                active_request,
                cursor=job["cursor"],
                cursor_context=job["cursor_context"],
                on_batch=on_batch,
                should_cancel=lambda: self._stop_event.is_set()
                or self.storage.cancel_requested(job_id),
            )
            current = self.storage.get_job(job_id)
            if current and current["collected_count"] >= request.max_tweets:
                summary.completion_reason = "target_reached"
                summary.partial = False
            elif summary.completion_reason == "target_reached":
                summary.completion_reason = "target_not_reached"
                summary.partial = True
                summary.warnings.append(
                    "Provider stopped before the requested number of unique posts was stored."
                )
            self.storage.finish_job(
                job_id,
                list(dict.fromkeys([*job["warnings"], *summary.warnings])),
                completion_reason=summary.completion_reason,
                partial=summary.partial,
            )
        except CollectionCancelled as exc:
            if self._stop_event.is_set() and not self.storage.cancel_requested(job_id):
                self.storage.fail_job(
                    job_id,
                    JobStatus.INTERRUPTED,
                    "interrupted",
                    "Worker stopped; resume the job to continue.",
                    True,
                )
            else:
                self.storage.fail_job(
                    job_id, JobStatus.CANCELLED, exc.code, str(exc), exc.retryable
                )
        except ScraperError as exc:
            self.storage.fail_job(job_id, JobStatus.FAILED, exc.code, str(exc), exc.retryable)
        except Exception as exc:
            logger.exception("Unexpected collection failure for job %s", job_id)
            if self._stop_event.is_set() and not self.storage.cancel_requested(job_id):
                self.storage.fail_job(
                    job_id,
                    JobStatus.INTERRUPTED,
                    "interrupted",
                    "Worker stopped unexpectedly; resume the job to continue.",
                    True,
                )
            elif self.storage.cancel_requested(job_id):
                self.storage.fail_job(
                    job_id, JobStatus.CANCELLED, "cancelled", "Collection cancelled.", True
                )
            else:
                self.storage.fail_job(
                    job_id, JobStatus.FAILED, "unexpected_error", str(exc), True
                )

    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                self._queue.task_done()
                return
            try:
                self.run_once(job_id)
            finally:
                self._queue.task_done()
