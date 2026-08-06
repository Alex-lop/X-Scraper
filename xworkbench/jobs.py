from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import uuid
from typing import Any

from .errors import CollectionCancelled, CollectionError, RateLimitWaiting
from .models import CollectionRequest, JobStatus
from .providers import CollectionProvider, ProviderRegistry
from .storage import Storage

logger = logging.getLogger(__name__)


class JobService:
    def __init__(
        self,
        storage: Storage,
        providers: ProviderRegistry | CollectionProvider,
        *,
        start_worker: bool = True,
    ):
        self.storage = storage
        self.registry = (
            providers if isinstance(providers, ProviderRegistry) else ProviderRegistry([providers])
        )
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
                descriptor = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    owner_pid = int(self._lock_path.read_text().strip().split(" ", 1)[0])
                    os.kill(owner_pid, 0)
                except (OSError, ValueError, ProcessLookupError):
                    self._lock_path.unlink(missing_ok=True)
                    continue
                raise RuntimeError(
                    "Another xworkbench worker process is already using this database."
                ) from None
            with os.fdopen(descriptor, "w") as lock_file:
                lock_file.write(f"{os.getpid()} {self.worker_id}\n")
            self._lock_owned = True
            atexit.register(self.shutdown)
            return
        raise RuntimeError("Could not acquire the xworkbench worker process lock.")

    def _release_process_lock(self) -> None:
        if not self._lock_owned:
            return
        try:
            if self.worker_id in self._lock_path.read_text():
                self._lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._lock_owned = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._acquire_process_lock()
        try:
            recovered = self.storage.recover_jobs()
        except Exception:
            self._release_process_lock()
            raise
        self._thread = threading.Thread(target=self._worker, name="xworkbench-worker", daemon=True)
        self._thread.start()
        for job_id in recovered:
            self.enqueue(job_id)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._stop_event.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5)
        if not self._thread or not self._thread.is_alive():
            self._release_process_lock()

    def submit(self, request: CollectionRequest, execution_plan: dict[str, Any]) -> str:
        if self._shutdown:
            raise RuntimeError("Job service is shutting down.")
        job_id = self.storage.create_job(request, execution_plan)
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
        job = self.storage.claim_job(job_id)
        if not job:
            return

        def on_batch(posts, provider_state, metadata):
            return self.storage.add_posts(job_id, posts, provider_state, metadata)

        try:
            request = CollectionRequest.from_dict(job["request"])
            if job["collected_count"] >= request.max_posts:
                self.storage.finish_job(
                    job_id, job["warnings"], completion_reason="target_reached"
                )
                return
            provider = self.registry.get(request.provider)
            summary = provider.collect(
                request,
                execution_plan=job["execution_plan"],
                checkpoint=job["checkpoint"],
                on_batch=on_batch,
                should_cancel=lambda: (
                    self._stop_event.is_set() or self.storage.cancel_requested(job_id)
                ),
            )
            current = self.storage.get_job(job_id)
            if current and current["collected_count"] >= request.max_posts:
                summary.completion_reason = "target_reached"
                summary.partial = False
            elif summary.completion_reason == "target_reached":
                summary.completion_reason = "target_not_reached"
                summary.partial = True
                summary.warnings.append(
                    "Provider stopped before the requested number of unique posts was stored."
                )
            persisted_warnings = current["warnings"] if current else job["warnings"]
            self.storage.finish_job(
                job_id,
                list(dict.fromkeys([*persisted_warnings, *summary.warnings])),
                completion_reason=summary.completion_reason,
                partial=summary.partial,
            )
        except RateLimitWaiting as exc:
            self.storage.wait_job(job_id, exc.retry_at, exc.remaining, exc.reset, str(exc))
        except CollectionCancelled as exc:
            status = (
                JobStatus.INTERRUPTED
                if self._stop_event.is_set() and not self.storage.cancel_requested(job_id)
                else JobStatus.CANCELLED
            )
            self.storage.fail_job(job_id, status, status.value, str(exc), True)
        except CollectionError as exc:
            current = self.storage.get_job(job_id)
            status = (
                JobStatus.PARTIAL
                if current and current["collected_count"] > 0
                else JobStatus.FAILED
            )
            self.storage.fail_job(job_id, status, exc.code, str(exc), exc.retryable)
        except Exception as exc:
            logger.exception("Unexpected collection failure for job %s", job_id)
            current = self.storage.get_job(job_id)
            status = (
                JobStatus.PARTIAL
                if current and current["collected_count"] > 0
                else JobStatus.FAILED
            )
            self.storage.fail_job(job_id, status, "unexpected_error", str(exc), True)

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=1)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                for due_id in self.storage.requeue_due_jobs():
                    self.enqueue(due_id)
                continue
            if job_id is None:
                self._queue.task_done()
                return
            try:
                self.run_once(job_id)
            finally:
                self._queue.task_done()
