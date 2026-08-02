from __future__ import annotations

import logging
import queue
import threading
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
        if start_worker:
            self.start()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, name="xscraper-worker", daemon=True)
        self._thread.start()
        for job_id in self.storage.recover_jobs():
            self.enqueue(job_id)

    def shutdown(self) -> None:
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5)

    def submit(self, request: CollectionRequest) -> str:
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
        job = self.storage.get_job(job_id)
        if not job:
            return
        if job["status"] != JobStatus.QUEUED.value:
            return
        if job["cancel_requested"]:
            self.storage.fail_job(
                job_id, JobStatus.CANCELLED, "cancelled", "Collection cancelled.", True
            )
            return

        request = CollectionRequest.from_dict(job["request"])
        remaining = request.max_tweets - int(job["collected_count"])
        if remaining <= 0:
            self.storage.finish_job(job_id, job["warnings"])
            return
        active_request = replace(request, max_tweets=remaining)
        self.storage.set_running(job_id)

        def on_batch(tweets, cursor):
            self.storage.add_tweets(job_id, tweets, cursor)
            if request.analyze_sentiment:
                for tweet in tweets:
                    label, score, analyzer_version = analyze(tweet.text)
                    self.storage.save_enrichment(
                        tweet.tweet_id, label, score, "vader", analyzer_version
                    )

        try:
            summary = self.provider.collect(
                active_request,
                cursor=job["cursor"],
                on_batch=on_batch,
                should_cancel=lambda: self.storage.cancel_requested(job_id),
            )
            self.storage.finish_job(job_id, summary.warnings)
        except CollectionCancelled as exc:
            self.storage.fail_job(job_id, JobStatus.CANCELLED, exc.code, str(exc), exc.retryable)
        except ScraperError as exc:
            self.storage.fail_job(job_id, JobStatus.FAILED, exc.code, str(exc), exc.retryable)
        except Exception as exc:
            logger.exception("Unexpected collection failure for job %s", job_id)
            self.storage.fail_job(job_id, JobStatus.FAILED, "unexpected_error", str(exc), True)

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
