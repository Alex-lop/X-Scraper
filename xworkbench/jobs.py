from __future__ import annotations

import atexit
import ctypes
import logging
import math
import os
import sys
import threading
import traceback
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, process_time
from typing import Any

from .errors import CollectionCancelled, CollectionError, RateLimitWaiting
from .models import CollectionRequest, JobStatus, source_fingerprint
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
    "queue_full": "The local collection queue is full.",
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


class QueueFullError(CollectionError):
    code = "queue_full"
    retryable = True


class _ProcessResourceProbe:
    # ponytail: stdlib process metrics only; add an optional tree probe if browser-tree
    # admission becomes portable enough to test on every supported OS.
    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._last_wall: float | None = None
        self._last_cpu: float | None = None
        self._cpu_percent: float | None = None

    @staticmethod
    def _rss_bytes() -> int | None:
        if sys.platform.startswith("linux"):
            try:
                resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
                return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
            except (OSError, ValueError, IndexError):
                return None
        if sys.platform == "darwin":

            class ProcTaskInfo(ctypes.Structure):
                _fields_ = [
                    ("virtual_size", ctypes.c_uint64),
                    ("resident_size", ctypes.c_uint64),
                    ("total_user", ctypes.c_uint64),
                    ("total_system", ctypes.c_uint64),
                    ("threads_user", ctypes.c_uint64),
                    ("threads_system", ctypes.c_uint64),
                    *[
                        (name, ctypes.c_int32)
                        for name in (
                            "policy",
                            "faults",
                            "pageins",
                            "cow_faults",
                            "messages_sent",
                            "messages_received",
                            "syscalls_mach",
                            "syscalls_unix",
                            "csw",
                            "threadnum",
                            "numrunning",
                            "priority",
                        )
                    ],
                ]

            try:
                libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
                info = ProcTaskInfo()
                size = ctypes.sizeof(info)
                returned = libproc.proc_pidinfo(os.getpid(), 4, 0, ctypes.byref(info), size)
                return int(info.resident_size) if returned == size else None
            except (AttributeError, OSError):
                return None
        return None

    def __call__(self) -> dict[str, Any]:
        wall = self._clock()
        cpu = process_time()
        if self._last_wall is not None and wall - self._last_wall >= 0.1:
            self._cpu_percent = max(
                0.0, (cpu - (self._last_cpu or 0.0)) / (wall - self._last_wall) * 100
            )
        self._last_wall = wall
        self._last_cpu = cpu
        return {"rssBytes": self._rss_bytes(), "cpuPercent": self._cpu_percent}


@dataclass(slots=True)
class _QueuedJob:
    job_id: str
    priority: int
    sequence: int
    source_id: str
    auth_id: str
    enqueued_at: float


class JobService:
    def __init__(
        self,
        storage: Storage,
        providers: ProviderRegistry | CollectionProvider,
        *,
        start_worker: bool = True,
        max_workers: int = 1,
        max_queue: int = 100,
        provider_factory: Callable[[], ProviderRegistry | CollectionProvider] | None = None,
        lease_seconds: int = 30,
        event_capacity: int = 1_000,
        resource_max_rss_mb: int = 1_536,
        resource_max_cpu_percent: int = 300,
        resource_recovery_seconds: int = 5,
        resource_probe: Callable[[], dict[str, Any]] | None = None,
        resource_clock: Callable[[], float] = monotonic,
    ):
        if isinstance(max_workers, bool) or not 1 <= max_workers <= 4:
            raise ValueError("max_workers must be between 1 and 4.")
        if isinstance(max_queue, bool) or not 1 <= max_queue <= 10_000:
            raise ValueError("max_queue must be between 1 and 10,000.")
        if max_workers > 1 and provider_factory is None:
            raise ValueError("max_workers above 1 requires an isolated provider_factory.")
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 1 and 300.")
        if isinstance(event_capacity, bool) or not 1 <= event_capacity <= 10_000:
            raise ValueError("event_capacity must be between 1 and 10,000.")
        for name, value, minimum, maximum in (
            ("resource_max_rss_mb", resource_max_rss_mb, 128, 131_072),
            ("resource_max_cpu_percent", resource_max_cpu_percent, 1, 1_000),
            ("resource_recovery_seconds", resource_recovery_seconds, 1, 300),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be between {minimum} and {maximum}.")
        if (
            not callable(resource_clock)
            or resource_probe is not None
            and not callable(resource_probe)
        ):
            raise ValueError("resource probe and clock must be callable.")
        self.storage = storage
        self.registry = (
            providers if isinstance(providers, ProviderRegistry) else ProviderRegistry([providers])
        )
        self.max_workers = max_workers
        self.max_queue = max_queue
        self.lease_seconds = lease_seconds
        self.resource_max_rss_mb = resource_max_rss_mb
        self.resource_max_cpu_percent = resource_max_cpu_percent
        self.resource_recovery_seconds = resource_recovery_seconds
        self._resource_clock = resource_clock
        self._resource_probe = resource_probe or _ProcessResourceProbe(resource_clock)
        self._provider_factory = provider_factory
        self._condition = threading.Condition()
        # ponytail: serialize SQLite callbacks until Storage owns queue backpressure/leases.
        self._storage_lock = threading.Lock()
        self._pending: dict[int, dict[str, deque[_QueuedJob]]] = defaultdict(dict)
        self._source_order: dict[int, deque[str]] = defaultdict(deque)
        self._pending_jobs: dict[str, _QueuedJob] = {}
        self._active_jobs: dict[str, _QueuedJob] = {}
        self._active_sources: set[str] = set()
        self._active_auth: set[str] = set()
        self._sequence = 0
        self._started_at = monotonic()
        self._submitted = 0
        self._deduplicated = 0
        self._rejected = 0
        self._started = 0
        self._finished = 0
        self._cancelled = 0
        self._queue_waits: deque[float] = deque(maxlen=1_000)
        self._completed_by_status: dict[str, int] = defaultdict(int)
        self._cleanup_seconds = 0.0
        self._cleanup_failures = 0
        self._persistence_active = 0
        self._persistence_waiting = 0
        self._max_persistence_backlog = 0
        self._event_buffer: deque[dict[str, Any]] = deque(maxlen=event_capacity)
        self._event_epoch = uuid.uuid4().hex
        self._event_sequence = 0
        self._event_dropped = 0
        self._event_coalesced = 0
        self._resource_samples: deque[dict[str, Any]] = deque(maxlen=10)
        self._resource_sample_sequence = 0
        self._resource_next_sample_at = 0.0
        self._resource_paused = False
        self._resource_pause_reasons: list[str] = []
        self._resource_tripped_signals: set[str] = set()
        self._resource_recovery_since: float | None = None
        self._resource_probe_failures = 0
        self._resource_signal_status = {
            "rssBytes": "unsupported",
            "cpuPercent": "unsupported",
            "eventLoopLagMs": "not_applicable",
            "chromiumProcessCount": "unsupported",
            "liveContextCount": "unsupported",
            "livePageCount": "unsupported",
        }
        self._threads: list[threading.Thread] = []
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
        atexit.unregister(self.shutdown)

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        if self._shutdown:
            raise RuntimeError("Job service is shutting down.")
        self._acquire_process_lock()
        try:
            with self._storage_lock:
                self.storage.recover_jobs()
                queued = self.storage.list_queued_jobs()
            with self._condition:
                for job in queued:
                    self._enqueue_locked(
                        job["id"],
                        job["priority"],
                        job["source_id"],
                        job["auth_state_id"],
                        job["enqueue_sequence"],
                    )
            registries = [self._worker_registry() for _ in range(self.max_workers)]
            if len({id(registry) for registry in registries}) != len(registries):
                raise ValueError("provider_factory must return a new registry for each worker.")
            self._threads = [
                threading.Thread(
                    target=self._worker,
                    args=(registry,),
                    name=f"xworkbench-worker-{index + 1}",
                    daemon=True,
                )
                for index, registry in enumerate(registries)
            ]
            self._thread = self._threads[0]
            for thread in self._threads:
                thread.start()
        except Exception:
            self._shutdown = True
            self._stop_event.set()
            with self._condition:
                self._condition.notify_all()
            for thread in self._threads:
                thread.join(timeout=1)
            self._release_process_lock()
            raise

    def shutdown(self) -> None:
        started = monotonic()
        if not self._shutdown:
            self._shutdown = True
            self._stop_event.set()
            with self._condition:
                self._condition.notify_all()
        # ponytail: covers one 30s official HTTP call; process isolation is the upgrade path.
        deadline = monotonic() + max(5, self.lease_seconds + 5)
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - monotonic()))
        alive = sum(thread.is_alive() for thread in self._threads)
        with self._condition:
            self._cleanup_seconds = monotonic() - started
            self._cleanup_failures = alive
        if not alive:
            self._release_process_lock()

    def _worker_registry(self) -> ProviderRegistry:
        if self._provider_factory is None:
            return self.registry
        providers = self._provider_factory()
        return (
            providers if isinstance(providers, ProviderRegistry) else ProviderRegistry([providers])
        )

    @staticmethod
    def _identifier(value: str | None, default: str, name: str) -> str:
        value = default if value is None else value
        if not isinstance(value, str) or not value or len(value) > 128 or not value.isprintable():
            raise ValueError(f"{name} must contain 1 to 128 printable characters.")
        return value

    @staticmethod
    def _default_deadline(execution_plan: dict[str, Any]) -> datetime:
        raw = execution_plan.get("preparedAt", execution_plan.get("compiledAt"))
        try:
            prepared = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if prepared.utcoffset() is None:
                raise ValueError
        except ValueError:
            prepared = datetime.now(UTC)
        return prepared.astimezone(UTC) + timedelta(hours=1)

    def submit(
        self,
        request: CollectionRequest,
        execution_plan: dict[str, Any],
        *,
        priority: int = 0,
        source_id: str | None = None,
        auth_state_id: str | None = None,
        idempotency_key: str | None = None,
        batch_id: str | None = None,
        approval: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        deadline_at: str | datetime | None = None,
    ) -> str:
        if self._shutdown:
            raise RuntimeError("Job service is shutting down.")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            raise ValueError("priority must be between 0 and 100.")
        routing_source = source_id or source_fingerprint(request)
        if source_id is not None:
            source_id = self._identifier(source_id, "", "source_id")
        auth_state_id = self._identifier(
            auth_state_id,
            f"provider:{request.provider.value}",
            "auth_state_id",
        )
        if idempotency_key is not None:
            idempotency_key = self._identifier(idempotency_key, "", "idempotency_key")
        if batch_id is not None:
            batch_id = self._identifier(batch_id, "", "batch_id")
        with self._storage_lock:
            admitted = self.storage.admit_job(
                request,
                execution_plan,
                queue_capacity=self.max_queue,
                priority=priority,
                source_id=source_id,
                auth_state_id=auth_state_id,
                batch_id=batch_id,
                idempotency_key=idempotency_key,
                approval=approval or {},
                limits=limits or {},
                deadline_at=deadline_at or self._default_deadline(execution_plan),
            )
            job_id = admitted["job_id"]
            with self._condition:
                known_locally = bool(
                    job_id
                    and (str(job_id) in self._pending_jobs or str(job_id) in self._active_jobs)
                )
            existing = (
                self.storage.get_job(str(job_id))
                if admitted["result"] == "existing" and job_id and not known_locally
                else None
            )
            with self._condition:
                if admitted["result"] == "queue_full" or not job_id:
                    self._rejected += 1
                    raise QueueFullError(ERROR_MESSAGES["queue_full"])
                should_enqueue = admitted["result"] == "created" or bool(
                    existing and existing["status"] == JobStatus.QUEUED.value
                )
                if should_enqueue:
                    self._enqueue_locked(str(job_id), priority, routing_source, auth_state_id)
                if admitted["result"] == "existing":
                    self._deduplicated += 1
                else:
                    self._submitted += 1
                    self._emit_locked("admitted", str(job_id), status=JobStatus.QUEUED.value)
                self._condition.notify_all()
                return str(job_id)

    def submit_batch(self, items: list[dict[str, Any]], *, batch_id: str) -> list[str]:
        if self._shutdown:
            raise RuntimeError("Job service is shutting down.")
        batch_id = self._identifier(batch_id, "", "batch_id")
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("items must contain 1 to 100 prepared jobs.")
        allowed = {
            "request",
            "execution_plan",
            "priority",
            "source_id",
            "auth_state_id",
            "idempotency_key",
            "limits",
            "deadline_at",
        }
        required = {
            "request",
            "execution_plan",
            "idempotency_key",
            "limits",
            "deadline_at",
        }
        prepared = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not required <= set(item) or not set(item) <= allowed:
                raise ValueError(f"Batch item {index} has invalid fields.")
            request = item["request"]
            plan = item["execution_plan"]
            if not isinstance(request, CollectionRequest) or not isinstance(plan, dict):
                raise ValueError(f"Batch item {index} is not prepared.")
            priority = item.get("priority", 0)
            if (
                isinstance(priority, bool)
                or not isinstance(priority, int)
                or not 0 <= priority <= 100
            ):
                raise ValueError(f"Batch item {index} priority must be between 0 and 100.")
            source_id = item.get("source_id")
            if source_id is not None:
                source_id = self._identifier(source_id, "", "source_id")
            auth_state_id = self._identifier(
                item.get("auth_state_id"),
                f"provider:{request.provider.value}",
                "auth_state_id",
            )
            idempotency_key = item.get("idempotency_key")
            if idempotency_key is None:
                raise ValueError(f"Batch item {index} idempotency_key is required.")
            idempotency_key = self._identifier(idempotency_key, "", "idempotency_key")
            limits = item["limits"]
            if not isinstance(limits, dict):
                raise ValueError(f"Batch item {index} limits must be an object.")
            prepared.append(
                {
                    "request": request,
                    "plan": dict(plan),
                    "priority": priority,
                    "source_id": source_id,
                    "auth_state_id": auth_state_id,
                    "idempotency_key": idempotency_key,
                    "limits": dict(limits),
                    "deadline_at": item["deadline_at"],
                }
            )

        with self._storage_lock:
            preview = self.storage.batch_preview_fingerprint(prepared, batch_id)
            admitted = self.storage.admit_batch(
                prepared,
                queue_capacity=self.max_queue,
                batch_id=batch_id,
                approval_manifest={
                    "approvedAt": datetime.now(UTC).isoformat(),
                    "confirmation": True,
                    "previewFingerprint": preview,
                    "batchId": batch_id,
                },
            )
            with self._condition:
                if admitted["result"] == "queue_full":
                    self._rejected += len(prepared)
                    raise QueueFullError(ERROR_MESSAGES["queue_full"])
                job_ids = []
                for job in admitted["jobs"]:
                    job_id = str(job["job_id"])
                    job_ids.append(job_id)
                    if job["status"] == JobStatus.QUEUED.value:
                        self._enqueue_locked(
                            job_id,
                            job["priority"],
                            job["source_id"],
                            job["auth_state_id"],
                            job["enqueue_sequence"],
                        )
                    if job["result"] == "existing":
                        self._deduplicated += 1
                    else:
                        self._submitted += 1
                        self._emit_locked("admitted", job_id, status=JobStatus.QUEUED.value)
                self._condition.notify_all()
                return job_ids

    def _job_routing(self, job_id: str) -> tuple[str, str]:
        try:
            with self._storage_lock:
                job = self.storage.get_job(job_id)
            request = CollectionRequest.from_dict(job["request"]) if job else None
        except (CollectionError, KeyError, TypeError):
            job = None
            request = None
        return (
            str(job.get("source_id") or source_fingerprint(request))
            if job and request
            else job_id,
            str(job.get("auth_state_id") or f"provider:{request.provider.value}")
            if job and request
            else "provider:unknown",
        )

    def _enqueue_locked(
        self,
        job_id: str,
        priority: int,
        source_id: str,
        auth_id: str,
        enqueue_sequence: int | None = None,
    ) -> bool:
        if job_id in self._pending_jobs or job_id in self._active_jobs:
            return False
        if len(self._pending_jobs) >= self.max_queue:
            self._rejected += 1
            raise QueueFullError(ERROR_MESSAGES["queue_full"])
        sequence = self._sequence if enqueue_sequence is None else enqueue_sequence
        item = _QueuedJob(job_id, priority, sequence, source_id, auth_id, monotonic())
        self._sequence = max(self._sequence, sequence + 1)
        source_jobs = self._pending[priority].setdefault(source_id, deque())
        if not source_jobs:
            self._source_order[priority].append(source_id)
        source_jobs.append(item)
        self._pending_jobs[job_id] = item
        return True

    def enqueue(
        self,
        job_id: str,
        *,
        priority: int = 0,
        enqueue_sequence: int | None = None,
        source_id: str | None = None,
        auth_id: str | None = None,
    ) -> None:
        with self._condition:
            if job_id in self._pending_jobs or job_id in self._active_jobs:
                return
        if source_id is None or auth_id is None:
            routed_source, routed_auth = self._job_routing(job_id)
            source_id = source_id or routed_source
            auth_id = auth_id or routed_auth
        with self._condition:
            if self._enqueue_locked(job_id, priority, source_id, auth_id, enqueue_sequence):
                self._condition.notify_all()

    def cancel(self, job_id: str) -> bool:
        with self._storage_lock:
            cancelled = self.storage.request_cancel(job_id)
        if cancelled:
            with self._condition:
                if self._remove_pending_locked(job_id):
                    self._finished += 1
                    self._completed_by_status[JobStatus.CANCELLED.value] += 1
                self._cancelled += 1
                self._emit_locked("cancelled", job_id, status=JobStatus.CANCELLED.value)
                self._condition.notify_all()
        return cancelled

    def cancel_remaining(self, batch_id: str) -> int:
        batch_id = self._identifier(batch_id, "", "batch_id")
        with self._storage_lock:
            candidates = {
                job["id"]
                for job in self.storage.list_batch_jobs(batch_id)
                if job["status"]
                in {
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.WAITING.value,
                }
            }
            cancelled = self.storage.cancel_batch(batch_id)
        if cancelled:
            with self._condition:
                for job_id in candidates:
                    if self._remove_pending_locked(job_id):
                        self._finished += 1
                        self._completed_by_status[JobStatus.CANCELLED.value] += 1
                    self._emit_locked("cancelled", job_id, status=JobStatus.CANCELLED.value)
                self._cancelled += cancelled
                self._condition.notify_all()
        return cancelled

    def resume(self, job_id: str) -> bool:
        with self._condition:
            if len(self._pending_jobs) >= self.max_queue:
                return False
        with self._storage_lock:
            resumed = self.storage.resume_job(job_id)
        if not resumed:
            return False
        source_id, auth_id = self._job_routing(job_id)
        with self._condition:
            self._enqueue_locked(job_id, 0, source_id, auth_id)
            self._condition.notify_all()
        return True

    def _remove_pending_locked(self, job_id: str) -> bool:
        item = self._pending_jobs.pop(job_id, None)
        if item is None:
            return False
        source_jobs = self._pending[item.priority][item.source_id]
        source_jobs.remove(item)
        if not source_jobs:
            del self._pending[item.priority][item.source_id]
            self._source_order[item.priority].remove(item.source_id)
        if not self._pending[item.priority]:
            del self._pending[item.priority]
            del self._source_order[item.priority]
        return True

    @staticmethod
    def _resource_value(value: Any, *, integer: bool) -> int | float | None:
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or value < 0
            or not math.isfinite(value)
            or integer
            and not isinstance(value, int)
        ):
            return None
        return value

    def _resource_ready_locked(self) -> bool:
        try:
            now = float(self._resource_clock())
            if not math.isfinite(now):
                raise ValueError
        except Exception:
            self._resource_probe_failures += 1
            if self._resource_paused:
                self._resource_pause_reasons = ["signals_unavailable"]
                self._resource_recovery_since = None
            return not self._resource_paused
        if self._resource_samples and now < self._resource_next_sample_at:
            return not self._resource_paused
        try:
            raw = self._resource_probe()
            if not isinstance(raw, dict):
                raise TypeError
        except Exception:
            raw = {}
            self._resource_probe_failures += 1
        sample = {
            "rssBytes": self._resource_value(raw.get("rssBytes"), integer=True),
            "cpuPercent": self._resource_value(raw.get("cpuPercent"), integer=False),
            "eventLoopLagMs": None,
            "chromiumProcessCount": self._resource_value(
                raw.get("chromiumProcessCount"), integer=True
            ),
            "liveContextCount": self._resource_value(raw.get("liveContextCount"), integer=True),
            "livePageCount": self._resource_value(raw.get("livePageCount"), integer=True),
        }
        for name, value in sample.items():
            self._resource_signal_status[name] = (
                "not_applicable"
                if name == "eventLoopLagMs"
                else "supported"
                if value is not None
                else "unsupported"
            )
        self._resource_sample_sequence += 1
        self._resource_samples.append({"sequence": self._resource_sample_sequence, **sample})
        self._resource_next_sample_at = now + 0.25
        reasons = []
        if (
            sample["rssBytes"] is not None
            and sample["rssBytes"] > self.resource_max_rss_mb * 1024 * 1024
        ):
            reasons.append("rss")
        if (
            sample["cpuPercent"] is not None
            and sample["cpuPercent"] > self.resource_max_cpu_percent
        ):
            reasons.append("cpu")
        if reasons:
            self._resource_paused = True
            self._resource_pause_reasons = reasons
            self._resource_tripped_signals = set(reasons)
            self._resource_recovery_since = None
        elif self._resource_paused:
            signal_values = {"rss": sample["rssBytes"], "cpu": sample["cpuPercent"]}
            if any(signal_values[name] is None for name in self._resource_tripped_signals):
                self._resource_pause_reasons = ["signals_unavailable"]
                self._resource_recovery_since = None
            elif self._resource_recovery_since is None:
                self._resource_pause_reasons = ["recovery_window"]
                self._resource_recovery_since = now
            elif now - self._resource_recovery_since >= self.resource_recovery_seconds:
                self._resource_paused = False
                self._resource_pause_reasons = []
                self._resource_tripped_signals.clear()
                self._resource_recovery_since = None
                self._condition.notify_all()
        return not self._resource_paused

    def _next_job_locked(self) -> _QueuedJob | None:
        for priority in sorted(self._pending, reverse=True):
            sources = self._source_order[priority]
            for _ in range(len(sources)):
                source_id = sources.popleft()
                source_jobs = self._pending[priority][source_id]
                item = source_jobs[0]
                if source_id in self._active_sources or item.auth_id in self._active_auth:
                    sources.append(source_id)
                    continue
                source_jobs.popleft()
                if source_jobs:
                    sources.append(source_id)
                else:
                    del self._pending[priority][source_id]
                if not self._pending[priority]:
                    del self._pending[priority]
                    del self._source_order[priority]
                self._pending_jobs.pop(item.job_id)
                self._active_jobs[item.job_id] = item
                self._active_sources.add(item.source_id)
                self._active_auth.add(item.auth_id)
                self._started += 1
                self._queue_waits.append(monotonic() - item.enqueued_at)
                return item
        return None

    def _emit_locked(
        self,
        event_type: str,
        job_id: str,
        *,
        status: str | None = None,
        count: int | None = None,
    ) -> None:
        self._event_sequence += 1
        event: dict[str, Any] = {
            "sequence": self._event_sequence,
            "type": event_type,
            "jobId": job_id,
        }
        if status is not None:
            event["status"] = status
        if count is not None:
            event["count"] = count
        if (
            event_type == "persisted"
            and self._event_buffer
            and self._event_buffer[-1]["type"] == "persisted"
            and self._event_buffer[-1]["jobId"] == job_id
        ):
            event["count"] = self._event_buffer[-1].get("count", 0) + (count or 0)
            self._event_buffer[-1] = event
            self._event_coalesced += 1
            return
        if len(self._event_buffer) == self._event_buffer.maxlen:
            self._event_dropped += 1
        self._event_buffer.append(event)

    def events(self, after_sequence: int = 0) -> dict[str, Any]:
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a nonnegative integer.")
        with self._condition:
            events = [
                dict(event) for event in self._event_buffer if event["sequence"] > after_sequence
            ]
            last_sequence = self._event_sequence
            previous = after_sequence
            gap = False
            for event in events:
                if event["sequence"] != previous + 1:
                    gap = True
                previous = event["sequence"]
            if after_sequence < last_sequence and not events:
                gap = True
        with self._storage_lock:
            jobs = self.storage.list_jobs(100)
        return {
            "events": events,
            "jobs": jobs,
            "eventEpoch": self._event_epoch,
            "lastSequence": last_sequence,
            "gap": gap,
        }

    @staticmethod
    def _percentile(samples: deque[float], percentile: float) -> float | None:
        if not samples:
            return None
        ordered = sorted(samples)
        return ordered[round((len(ordered) - 1) * percentile)] * 1_000

    def metrics(self) -> dict[str, Any]:
        with self._condition:
            uptime = max(monotonic() - self._started_at, 0.000_001)
            latest_resources = (
                self._resource_samples[-1]
                if self._resource_samples
                else {
                    "rssBytes": None,
                    "cpuPercent": None,
                    "eventLoopLagMs": None,
                    "chromiumProcessCount": None,
                    "liveContextCount": None,
                    "livePageCount": None,
                }
            )
            return {
                "queueDepth": len(self._pending_jobs),
                "queueCapacity": self.max_queue,
                "activeWorkers": len(self._active_jobs),
                "maxWorkers": self.max_workers,
                "activeSources": len(self._active_sources),
                "activeAuthStates": len(self._active_auth),
                "submitted": self._submitted,
                "deduplicated": self._deduplicated,
                "rejected": self._rejected,
                "started": self._started,
                "finished": self._finished,
                "cancelRequests": self._cancelled,
                "completedByStatus": dict(self._completed_by_status),
                "queueWaitP50Ms": self._percentile(self._queue_waits, 0.50),
                "queueWaitP95Ms": self._percentile(self._queue_waits, 0.95),
                "throughputJobsPerSecond": self._finished / uptime,
                "cleanupSeconds": self._cleanup_seconds,
                "cleanupFailures": self._cleanup_failures,
                "persistenceActive": self._persistence_active,
                "persistenceWaiting": self._persistence_waiting,
                "maxPersistenceBacklog": self._max_persistence_backlog,
                "eventBufferCapacity": self._event_buffer.maxlen,
                "eventBufferSize": len(self._event_buffer),
                "eventLastSequence": self._event_sequence,
                "eventDropped": self._event_dropped,
                "eventCoalesced": self._event_coalesced,
                **{
                    name: latest_resources[name]
                    for name in (
                        "rssBytes",
                        "cpuPercent",
                        "eventLoopLagMs",
                        "chromiumProcessCount",
                        "liveContextCount",
                        "livePageCount",
                    )
                },
                "resourcePaused": self._resource_paused,
                "resourcePauseReasons": list(self._resource_pause_reasons),
                "resourceSamples": [dict(sample) for sample in self._resource_samples],
                "resourceSampleCapacity": self._resource_samples.maxlen,
                "resourceSampleCount": len(self._resource_samples),
                "resourceProbeFailures": self._resource_probe_failures,
                "resourceSignalStatus": dict(self._resource_signal_status),
                "resourceRecoverySeconds": self.resource_recovery_seconds,
                "resourceMaxRssMb": self.resource_max_rss_mb,
                "resourceMaxCpuPercent": self.resource_max_cpu_percent,
            }

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
        lease_owner: str | None = None,
    ) -> None:
        for _ in range(2):
            try:
                with self._storage_lock:
                    self.storage.fail_job(
                        job_id,
                        status,
                        code,
                        ERROR_MESSAGES[code],
                        retryable,
                        worker_id=lease_owner,
                    )
                return
            except Exception as exc:
                self._log_exception(job_id, "terminal transition", exc)

    def _failure_status(self, job_id: str) -> JobStatus:
        try:
            with self._storage_lock:
                current = self.storage.get_job(job_id)
        except Exception as exc:
            self._log_exception(job_id, "partial-result check", exc)
            return JobStatus.FAILED
        return JobStatus.PARTIAL if current and current["collected_count"] > 0 else JobStatus.FAILED

    def _handle_collection_error(
        self,
        job_id: str,
        exc: CollectionError,
        *,
        lease_owner: str | None = None,
    ) -> None:
        if isinstance(exc, CollectionCancelled):
            status = JobStatus.INTERRUPTED if self._stop_event.is_set() else JobStatus.CANCELLED
            self._fail_job(job_id, status, status.value, retryable=True, lease_owner=lease_owner)
            return
        if self._stop_event.is_set():
            self._fail_job(
                job_id,
                JobStatus.INTERRUPTED,
                "interrupted",
                retryable=True,
                lease_owner=lease_owner,
            )
            return
        if isinstance(exc, RateLimitWaiting):
            try:
                with self._storage_lock:
                    self.storage.wait_job(
                        job_id,
                        exc.retry_at,
                        exc.remaining,
                        exc.reset,
                        ERROR_MESSAGES["rate_limited"],
                        worker_id=lease_owner,
                    )
            except Exception as storage_exc:
                self._log_exception(job_id, "rate-limit transition", storage_exc)
                self._fail_job(
                    job_id,
                    self._failure_status(job_id),
                    "storage_callback_failure",
                    retryable=True,
                    lease_owner=lease_owner,
                )
            return
        code = exc.code if exc.code in ERROR_MESSAGES else "provider_error"
        self._fail_job(
            job_id,
            self._failure_status(job_id),
            code,
            retryable=bool(exc.retryable and code not in NO_RETRY_CODES),
            lease_owner=lease_owner,
        )

    def _lease_expiry(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.lease_seconds)

    def run_once(
        self,
        job_id: str,
        registry: ProviderRegistry | None = None,
        *,
        lease_owner: str | None = None,
    ) -> None:
        try:
            with self._storage_lock:
                job = (
                    self.storage.lease_job(
                        job_id,
                        worker_id=lease_owner,
                        lease_expires_at=self._lease_expiry(),
                    )
                    if lease_owner
                    else self.storage.claim_job(job_id)
                )
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
        with self._condition:
            self._emit_locked("started", job_id, status=JobStatus.RUNNING.value)
        if not job.get("stored_metadata_valid", True):
            self._fail_job(
                job_id,
                JobStatus.FAILED,
                "invalid_persisted_job",
                retryable=False,
                lease_owner=lease_owner,
            )
            return

        def on_batch(posts, provider_state, metadata):
            activated = False
            with self._condition:
                self._persistence_waiting += 1
                self._max_persistence_backlog = max(
                    self._max_persistence_backlog,
                    self._persistence_active + self._persistence_waiting,
                )
            try:
                with self._storage_lock:
                    with self._condition:
                        self._persistence_waiting -= 1
                        self._persistence_active += 1
                        activated = True
                    try:
                        added = self.storage.add_posts(job_id, posts, provider_state, metadata)
                        if not added and metadata.get("resourcesReturned"):
                            self.storage.record_cancelled_official_usage(job_id, metadata)
                        with self._condition:
                            self._emit_locked("persisted", job_id, count=added)
                        return added
                    finally:
                        with self._condition:
                            self._persistence_active -= 1
            except Exception as exc:
                with self._condition:
                    if not activated:
                        self._persistence_waiting -= 1
                self._log_exception(job_id, "batch persistence", exc)
                raise StorageCallbackError(ERROR_MESSAGES["storage_callback_failure"]) from exc

        next_heartbeat = monotonic() + self.lease_seconds / 3

        def should_cancel() -> bool:
            nonlocal next_heartbeat
            if self._stop_event.is_set():
                return True
            try:
                with self._storage_lock:
                    if lease_owner and monotonic() >= next_heartbeat:
                        if not self.storage.heartbeat_job(
                            job_id,
                            worker_id=lease_owner,
                            lease_expires_at=self._lease_expiry(),
                        ):
                            if self.storage.cancel_requested(job_id):
                                return True
                            raise StorageCallbackError(ERROR_MESSAGES["storage_callback_failure"])
                        next_heartbeat = monotonic() + self.lease_seconds / 3
                    return self.storage.cancel_requested(job_id)
            except Exception as exc:
                self._log_exception(job_id, "cancellation check", exc)
                raise StorageCallbackError(ERROR_MESSAGES["storage_callback_failure"]) from exc

        try:
            request = CollectionRequest.from_dict(job["request"])
            if job["collected_count"] >= request.max_posts:
                try:
                    with self._storage_lock:
                        self.storage.finish_job(
                            job_id,
                            job["warnings"],
                            completion_reason="target_reached",
                            worker_id=lease_owner,
                        )
                except Exception as exc:
                    self._log_exception(job_id, "job completion", exc)
                    raise StorageCallbackError(ERROR_MESSAGES["storage_callback_failure"]) from exc
                return
            provider = (registry or self.registry).get(request.provider)
            summary = provider.collect(
                request,
                execution_plan=job["execution_plan"],
                checkpoint=job["checkpoint"],
                on_batch=on_batch,
                should_cancel=should_cancel,
            )
            try:
                with self._storage_lock:
                    current = self.storage.get_job(job_id)
            except Exception as exc:
                self._log_exception(job_id, "result persistence check", exc)
                raise StorageCallbackError(ERROR_MESSAGES["storage_callback_failure"]) from exc
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
                with self._storage_lock:
                    self.storage.finish_job(
                        job_id,
                        list(dict.fromkeys([*persisted_warnings, *summary.warnings])),
                        completion_reason=summary.completion_reason,
                        partial=summary.partial,
                        worker_id=lease_owner,
                    )
            except Exception as exc:
                self._log_exception(job_id, "job completion", exc)
                raise StorageCallbackError(ERROR_MESSAGES["storage_callback_failure"]) from exc
        except CollectionError as exc:
            self._handle_collection_error(job_id, exc, lease_owner=lease_owner)
        except Exception as exc:
            self._log_exception(job_id, "collection", exc)
            status = (
                JobStatus.INTERRUPTED if self._stop_event.is_set() else self._failure_status(job_id)
            )
            code = "interrupted" if status is JobStatus.INTERRUPTED else "unexpected_error"
            self._fail_job(job_id, status, code, retryable=True, lease_owner=lease_owner)

    def _worker(self, registry: ProviderRegistry | None = None) -> None:
        registry = registry or self.registry
        lease_owner = f"{self.worker_id}:{threading.get_ident()}"
        while True:
            with self._condition:
                item = None
                while not self._stop_event.is_set():
                    if self._pending_jobs and not self._resource_ready_locked():
                        self._condition.wait(timeout=0.25)
                        continue
                    item = self._next_job_locked()
                    if item is not None:
                        break
                    self._condition.wait()
                if item is None:
                    return
            try:
                self.run_once(item.job_id, registry, lease_owner=lease_owner)
            except Exception as exc:
                self._log_exception(item.job_id, "worker dispatch", exc)
                self._fail_job(
                    item.job_id,
                    JobStatus.FAILED,
                    "unexpected_error",
                    retryable=True,
                    lease_owner=lease_owner,
                )
            finally:
                try:
                    with self._storage_lock:
                        job = self.storage.get_job(item.job_id)
                    status = job["status"] if job else "missing"
                except Exception as exc:
                    self._log_exception(item.job_id, "worker metrics", exc)
                    status = "unknown"
                with self._condition:
                    self._active_jobs.pop(item.job_id, None)
                    self._active_sources.discard(item.source_id)
                    self._active_auth.discard(item.auth_id)
                    self._finished += 1
                    self._completed_by_status[status] += 1
                    if status in {
                        JobStatus.SUCCEEDED.value,
                        JobStatus.FAILED.value,
                        JobStatus.CANCELLED.value,
                        JobStatus.INTERRUPTED.value,
                        JobStatus.PARTIAL.value,
                    }:
                        self._emit_locked("terminal", item.job_id, status=status)
                    self._condition.notify_all()
