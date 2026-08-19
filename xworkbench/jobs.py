from __future__ import annotations

import atexit
import logging
import os
import queue
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any

from .errors import CollectionCancelled, CollectionError, RateLimitWaiting
from .models import CollectionRequest, JobStatus
from .providers import CollectionProvider, ProviderRegistry
from .storage import Storage

logger = logging.getLogger(__name__)

ERROR_MESSAGES = {
    "billing_failure": "The provider account cannot run this collection.",
    "browser_failure": "The browser collection failed.",
    "browser_rate_limited": (
        "The browser collection was rate-limited. Start a new approved job later."
    ),
    "browser_schema_failure": "The visible page could not be parsed safely.",
    "browser_unavailable": "The browser became unavailable during collection.",
    "cancelled": "Collection cancelled.",
    "credential_or_access_failure": "Provider credentials or access are unavailable.",
    "invalid_persisted_job": "The saved job is invalid and cannot be run.",
    "invalid_request": "The saved collection request is invalid.",
    "interrupted": "Collection interrupted during shutdown.",
    "job_timeout": "The collection reached its approved time limit.",
    "manual_action_required": "The browser requires manual action before another approved job.",
    "network_failure": "The provider could not be reached.",
    "provider_error": "The collection provider failed.",
    "rate_limited": "The collection was rate-limited. Start a new approved job later.",
    "resume_incompatible": "The saved checkpoint is incompatible with this collection.",
    "schema_mismatch": "The provider response could not be parsed safely.",
    "session_expired": "The saved browser session expired. Run: xworkbench auth",
    "session_invalid": "The saved browser session is invalid. Run: xworkbench auth",
    "session_missing": "No usable saved browser session is available. Run: xworkbench auth",
    "storage_callback_failure": "The collection could not be saved.",
    "unexpected_error": "The collection failed unexpectedly.",
}
NO_RETRY_CODES = {
    "browser_rate_limited",
    "manual_action_required",
    "rate_limited",
    "session_expired",
    "session_invalid",
    "session_missing",
}


class StorageCallbackError(CollectionError):
    code = "storage_callback_failure"
    retryable = True


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
            self._thread = threading.Thread(
                target=self._worker, name="xworkbench-worker", daemon=True
            )
            self._thread.start()
        except Exception:
            self._release_process_lock()
            raise
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

    @staticmethod
    def _log_exception(job_id: str, stage: str, exc: Exception) -> None:
        frames = traceback.extract_tb(exc.__traceback__)
        location = "unknown"
        if frames:
            frame = frames[-1]
            location = f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        logger.error(
            "Job %s failed during %s with %s at %s",
            job_id,
            stage,
            type(exc).__name__,
            location,
        )

    def _fail_job(
        self,
        job_id: str,
        status: JobStatus,
        code: str,
        *,
        retryable: bool,
    ) -> None:
        for _ in range(2):
            try:
                self.storage.fail_job(
                    job_id,
                    status,
                    code,
                    ERROR_MESSAGES[code],
                    retryable,
                )
                return
            except Exception as exc:
                self._log_exception(job_id, "terminal transition", exc)

    def _failure_status(self, job_id: str) -> JobStatus:
        try:
            current = self.storage.get_job(job_id)
        except Exception as exc:
            self._log_exception(job_id, "partial-result check", exc)
            return JobStatus.FAILED
        return (
            JobStatus.PARTIAL
            if current and current["collected_count"] > 0
            else JobStatus.FAILED
        )

    def _handle_collection_error(self, job_id: str, exc: CollectionError) -> None:
        if isinstance(exc, CollectionCancelled):
            status = JobStatus.INTERRUPTED if self._stop_event.is_set() else JobStatus.CANCELLED
            self._fail_job(job_id, status, status.value, retryable=True)
            return
        if self._stop_event.is_set():
            self._fail_job(job_id, JobStatus.INTERRUPTED, "interrupted", retryable=True)
            return
        if isinstance(exc, RateLimitWaiting):
            try:
                self.storage.wait_job(
                    job_id,
                    exc.retry_at,
                    exc.remaining,
                    exc.reset,
                    ERROR_MESSAGES["rate_limited"],
                )
            except Exception as storage_exc:
                self._log_exception(job_id, "rate-limit transition", storage_exc)
                self._fail_job(
                    job_id,
                    self._failure_status(job_id),
                    "storage_callback_failure",
                    retryable=True,
                )
            return
        code = exc.code if exc.code in ERROR_MESSAGES else "provider_error"
        self._fail_job(
            job_id,
            self._failure_status(job_id),
            code,
            retryable=bool(exc.retryable and code not in NO_RETRY_CODES),
        )

    def run_once(self, job_id: str) -> None:
        try:
            job = self.storage.claim_job(job_id)
        except Exception as exc:
            self._log_exception(job_id, "job claim", exc)
            self._fail_job(
                job_id,
                JobStatus.FAILED,
                "storage_callback_failure",
                retryable=True,
            )
            return
        if not job:
            return
        if not job.get("stored_metadata_valid", True):
            self._fail_job(
                job_id,
                JobStatus.FAILED,
                "invalid_persisted_job",
                retryable=False,
            )
            return

        def on_batch(posts, provider_state, metadata):
            try:
                return self.storage.add_posts(job_id, posts, provider_state, metadata)
            except Exception as exc:
                self._log_exception(job_id, "batch persistence", exc)
                raise StorageCallbackError(ERROR_MESSAGES["storage_callback_failure"]) from exc

        def should_cancel() -> bool:
            if self._stop_event.is_set():
                return True
            try:
                return self.storage.cancel_requested(job_id)
            except Exception as exc:
                self._log_exception(job_id, "cancellation check", exc)
                raise StorageCallbackError(ERROR_MESSAGES["storage_callback_failure"]) from exc

        try:
            request = CollectionRequest.from_dict(job["request"])
            if job["collected_count"] >= request.max_posts:
                try:
                    self.storage.finish_job(
                        job_id, job["warnings"], completion_reason="target_reached"
                    )
                except Exception as exc:
                    self._log_exception(job_id, "job completion", exc)
                    raise StorageCallbackError(
                        ERROR_MESSAGES["storage_callback_failure"]
                    ) from exc
                return
            provider = self.registry.get(request.provider)
            summary = provider.collect(
                request,
                execution_plan=job["execution_plan"],
                checkpoint=job["checkpoint"],
                on_batch=on_batch,
                should_cancel=should_cancel,
            )
            try:
                current = self.storage.get_job(job_id)
            except Exception as exc:
                self._log_exception(job_id, "result persistence check", exc)
                raise StorageCallbackError(
                    ERROR_MESSAGES["storage_callback_failure"]
                ) from exc
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
            try:
                self.storage.finish_job(
                    job_id,
                    list(dict.fromkeys([*persisted_warnings, *summary.warnings])),
                    completion_reason=summary.completion_reason,
                    partial=summary.partial,
                )
            except Exception as exc:
                self._log_exception(job_id, "job completion", exc)
                raise StorageCallbackError(ERROR_MESSAGES["storage_callback_failure"]) from exc
        except CollectionError as exc:
            self._handle_collection_error(job_id, exc)
        except Exception as exc:
            self._log_exception(job_id, "collection", exc)
            status = (
                JobStatus.INTERRUPTED
                if self._stop_event.is_set()
                else self._failure_status(job_id)
            )
            code = "interrupted" if status is JobStatus.INTERRUPTED else "unexpected_error"
            self._fail_job(job_id, status, code, retryable=True)

    def _worker(self) -> None:
        try:
            while True:
                try:
                    job_id = self._queue.get(timeout=1)
                except queue.Empty:
                    if self._stop_event.is_set():
                        return
                    continue
                if job_id is None or self._stop_event.is_set():
                    self._queue.task_done()
                    return
                try:
                    self.run_once(job_id)
                except Exception as exc:
                    self._log_exception(job_id, "worker dispatch", exc)
                    self._fail_job(
                        job_id,
                        JobStatus.FAILED,
                        "unexpected_error",
                        retryable=True,
                    )
                finally:
                    self._queue.task_done()
        finally:
            self._release_process_lock()
