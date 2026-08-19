import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from xworkbench.errors import (
    CollectionCancelled,
    InvalidRequestError,
    RateLimitWaiting,
    SchemaDriftError,
)
from xworkbench.jobs import JobService, QueueFullError
from xworkbench.models import CollectionRequest, CollectionSummary, Post, ProviderType
from xworkbench.playwright_browser import (
    BrowserManualActionRequired,
    BrowserRateLimitedError,
    BrowserSessionExpiredError,
)
from xworkbench.providers import ProviderRegistry
from xworkbench.storage import Storage
from xworkbench.x_api import compile_request

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "partial"}


def wait_for_jobs(storage, job_ids, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        statuses = {job_id: storage.get_job(job_id)["status"] for job_id in job_ids}
        if all(status in TERMINAL for status in statuses.values()):
            return statuses
        threading.Event().wait(0.01)
    raise AssertionError(f"Jobs did not finish: {statuses}")


class Provider:
    provider_id = ProviderType.OFFICIAL_X_API
    provider_version = 1

    def __init__(self, mode):
        self.mode = mode

    def collect(
        self,
        request,
        *,
        execution_plan,
        checkpoint,
        on_batch,
        should_cancel,
    ):
        assert execution_plan["provider"] == "official_x_api"
        assert checkpoint["storedCount"] == 0
        on_batch(
            [Post("1", "  persisted page\n", "tester", "https://x.com/tester/status/1", None)],
            "next",
            {
                "resourcesReturned": {"posts": 1, "users": 0, "media": 0},
                "warnings": ["page warning"],
            },
        )
        if self.mode == "wait":
            raise RateLimitWaiting(
                "try later",
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                0,
                123,
            )
        if self.mode == "fail":
            raise SchemaDriftError("schema changed")
        return CollectionSummary(
            warnings=["summary warning"],
            completion_reason=(
                "target_reached" if self.mode == "false_target" else "search_exhausted"
            ),
            partial=self.mode == "partial",
        )


def run(tmp_path, mode):
    storage = Storage(tmp_path / f"{mode}.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    job_id = storage.create_job(request, compile_request(request))
    JobService(storage, Provider(mode), start_worker=False).run_once(job_id)
    return storage, job_id


@pytest.mark.parametrize(("mode", "status"), [("ok", "succeeded"), ("partial", "partial")])
def test_completion_persists_status_warnings_resources_and_page(tmp_path, mode, status):
    storage, job_id = run(tmp_path, mode)
    job = storage.get_job(job_id)

    assert job["status"] == status
    assert job["completion_reason"] == "search_exhausted"
    assert job["warnings"] == ["page warning", "summary warning"]
    assert job["collected_count"] == job["post_resource_count"] == 1
    assert storage.get_job_posts(job_id)[0]["text"] == "  persisted page\n"


def test_rate_limit_is_terminal_and_preserves_page_without_automatic_retry(tmp_path):
    storage, job_id = run(tmp_path, "wait")
    job = storage.get_job(job_id)

    assert job["status"] == "partial"
    assert job["error_code"] == "rate_limited"
    assert job["error_retryable"] is False
    assert job["rate_limit_remaining"] == 0 and job["rate_limit_reset"] == 123
    assert job["collected_count"] == 1 and storage.count_job_posts(job_id) == 1


def test_provider_failure_preserves_completed_page(tmp_path):
    storage, job_id = run(tmp_path, "fail")
    job = storage.get_job(job_id)

    assert job["status"] == "partial" and job["error_code"] == "schema_mismatch"
    assert job["collected_count"] == job["post_resource_count"] == 1
    assert job["warnings"] == ["page warning"]
    assert storage.get_job_posts(job_id)[0]["post_id"] == "1"


def test_storage_callback_failure_is_not_misclassified_or_leaked(tmp_path, monkeypatch, caplog):
    sentinel = "storage-token-SENTINEL"
    storage = Storage(tmp_path / "storage-callback.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    job_id = storage.create_job(request, compile_request(request))
    monkeypatch.setattr(
        storage,
        "add_posts",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError(sentinel)),
    )

    JobService(storage, Provider("ok"), start_worker=False).run_once(job_id)

    job = storage.get_job(job_id)
    assert job["status"] == "failed"
    assert job["error_code"] == "storage_callback_failure"
    assert job["error_message"] == "The collection could not be saved."
    assert sentinel not in caplog.text


def test_false_target_reached_is_corrected_after_deduplication(tmp_path):
    storage, job_id = run(tmp_path, "false_target")
    job = storage.get_job(job_id)

    assert job["status"] == "partial"
    assert job["completion_reason"] == "target_not_reached"
    assert job["collected_count"] == 1
    assert "unique posts" in job["warnings"][-1]


class BrowserProvider:
    provider_id = ProviderType.PLAYWRIGHT_BROWSER
    provider_version = 1

    def capabilities(self):
        return {"sources": ["home"]}

    def connection_status(self):
        return {"status": "ready"}

    def prepare(self, request, supplied_plan=None):
        return supplied_plan or {"provider": self.provider_id.value, "providerVersion": 1}

    def collect(self, request, *, execution_plan, checkpoint, on_batch, should_cancel):
        assert "maximumPostResources" not in execution_plan
        assert checkpoint == {
            "providerState": None,
            "storedCount": 0,
            "metadata": {"captureSegment": 0},
        }
        on_batch(
            [Post("2", "visible", "tester", "https://x.com/tester/status/2", None)],
            {"scanIterations": 1},
            {"observationTime": "2026-08-05T00:00:00Z"},
        )
        return CollectionSummary(completion_reason="target_reached")


def test_registry_dispatches_browser_without_api_plan_fields(tmp_path):
    storage = Storage(tmp_path / "browser.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1}
    )
    plan = {"provider": "playwright_browser", "providerVersion": 1}
    job_id = storage.create_job(request, plan)
    registry = ProviderRegistry([Provider("unused"), BrowserProvider()])

    JobService(storage, registry, start_worker=False).run_once(job_id)

    job = storage.get_job(job_id)
    assert job["status"] == "succeeded" and job["provider"] == "playwright_browser"
    assert job["checkpoint"]["providerState"] == {"scanIterations": 1}
    assert job["checkpoint"]["metadata"]["observationTime"].endswith("Z")
    assert registry.capabilities("playwright_browser") == {"sources": ["home"]}
    with pytest.raises(InvalidRequestError, match="Unknown collection provider"):
        registry.get("not_real")
    with pytest.raises(ValueError, match="Duplicate"):
        ProviderRegistry([BrowserProvider(), BrowserProvider()])


def test_worker_does_not_poll_storage_after_shutdown_begins(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "shutdown.db")
    storage.initialize()
    service = JobService(storage, BrowserProvider(), start_worker=False)

    monkeypatch.setattr(
        storage,
        "requeue_due_jobs",
        lambda: (_ for _ in ()).throw(AssertionError("storage polled after shutdown")),
    )

    service._stop_event.set()
    service._worker()


def test_corrupt_job_is_terminal_and_worker_runs_next_job(tmp_path):
    storage = Storage(tmp_path / "corrupt-worker.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    plan = compile_request(request)
    corrupt_id = storage.create_job(request, plan)
    valid_id = storage.create_job(request, plan)
    with storage.connect() as connection:
        connection.execute("UPDATE jobs SET request_json = '{' WHERE id = ?", (corrupt_id,))

    service = JobService(storage, Provider("ok"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with storage.connect() as connection:
            statuses = dict(
                connection.execute(
                    "SELECT id, status FROM jobs WHERE id IN (?, ?)",
                    (corrupt_id, valid_id),
                )
            )
        if statuses == {corrupt_id: "failed", valid_id: "succeeded"}:
            break
        time.sleep(0.01)

    worker_alive = bool(service._thread and service._thread.is_alive())
    service.shutdown()
    assert statuses == {corrupt_id: "failed", valid_id: "succeeded"}
    assert worker_alive and not service._lock_path.exists()
    with storage.connect() as connection:
        corrupt_error = connection.execute(
            "SELECT error_code, error_message FROM jobs WHERE id = ?", (corrupt_id,)
        ).fetchone()
    assert tuple(corrupt_error) == (
        "invalid_persisted_job",
        "The saved job is invalid and cannot be run.",
    )


def test_unexpected_exception_contents_never_reach_logs_or_job(tmp_path, caplog):
    sentinel = "Bearer TOP-SECRET-SENTINEL"

    class UnexpectedProvider(Provider):
        def collect(self, *args, **kwargs):
            raise RuntimeError(sentinel)

    storage = Storage(tmp_path / "unexpected.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    job_id = storage.create_job(request, compile_request(request))

    JobService(storage, UnexpectedProvider("unused"), start_worker=False).run_once(job_id)

    job = storage.get_job(job_id)
    assert job["error_code"] == "unexpected_error"
    assert job["error_message"] == "The collection failed unexpectedly."
    assert sentinel not in caplog.text
    assert "RuntimeError" in caplog.text and "test_jobs.py" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        BrowserRateLimitedError("secret response"),
        BrowserManualActionRequired("secret response"),
        BrowserSessionExpiredError("secret response"),
    ],
)
def test_browser_rate_and_session_stops_are_terminal_without_retry(tmp_path, error):
    class StoppedProvider(Provider):
        def collect(self, *args, **kwargs):
            raise error

    storage = Storage(tmp_path / f"{error.code}.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    job_id = storage.create_job(request, compile_request(request))

    JobService(storage, StoppedProvider("unused"), start_worker=False).run_once(job_id)

    job = storage.get_job(job_id)
    assert job["status"] == "failed"
    assert job["error_code"] == error.code
    assert job["error_retryable"] is False


@pytest.mark.parametrize("mode", ["wait", "fail"])
def test_cancellation_wins_rate_limit_and_provider_error_races(tmp_path, mode):
    storage = Storage(tmp_path / f"cancel-{mode}.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    job_id = storage.create_job(request, compile_request(request))

    class CancellingProvider(Provider):
        def collect(self, *args, **kwargs):
            storage.request_cancel(job_id)
            if self.mode == "wait":
                raise RateLimitWaiting("secret response", datetime.now(UTC).isoformat(), 0, 1)
            raise SchemaDriftError("secret response")

    JobService(storage, CancellingProvider(mode), start_worker=False).run_once(job_id)

    job = storage.get_job(job_id)
    assert job["status"] == "cancelled"
    assert job["error_code"] == job["completion_reason"] == "cancelled"


class SchedulerTracker:
    def __init__(self, delay=0):
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.active_sources = {}
        self.active_auth = {}
        self.max_source = 0
        self.max_auth = 0
        self.order = []

    def enter(self, source, auth, label):
        with self.lock:
            self.active += 1
            self.active_sources[source] = self.active_sources.get(source, 0) + 1
            self.active_auth[auth] = self.active_auth.get(auth, 0) + 1
            self.max_active = max(self.max_active, self.active)
            self.max_source = max(self.max_source, self.active_sources[source])
            self.max_auth = max(self.max_auth, self.active_auth[auth])
            self.order.append(label)

    def leave(self, source, auth):
        with self.lock:
            self.active -= 1
            self.active_sources[source] -= 1
            self.active_auth[auth] -= 1


class SchedulingProvider:
    provider_id = ProviderType.OFFICIAL_X_API
    provider_version = 1

    def __init__(self, tracker):
        self.tracker = tracker

    def collect(self, request, *, execution_plan, checkpoint, on_batch, should_cancel):
        source = execution_plan["schedulerSource"]
        auth = execution_plan["schedulerAuth"]
        self.tracker.enter(source, auth, execution_plan["schedulerLabel"])
        try:
            threading.Event().wait(self.tracker.delay)
            if should_cancel():
                raise CollectionCancelled("cancelled")
            return CollectionSummary(completion_reason="search_exhausted")
        finally:
            self.tracker.leave(source, auth)


class PersistenceProvider:
    provider_id = ProviderType.OFFICIAL_X_API
    provider_version = 1

    def collect(self, request, *, execution_plan, checkpoint, on_batch, should_cancel):
        if execution_plan["schedulerLabel"] == "crash":
            raise RuntimeError("provider crash sentinel")
        on_batch(
            [
                Post(
                    execution_plan["schedulerLabel"],
                    "persisted",
                    "tester",
                    f"https://x.com/tester/status/{execution_plan['schedulerLabel']}",
                    None,
                )
            ],
            None,
            {},
        )
        return CollectionSummary(completion_reason="search_exhausted")


def scheduled_plan(request, source, auth, label):
    return {
        **compile_request(request),
        "schedulerSource": source,
        "schedulerAuth": auth,
        "schedulerLabel": label,
    }


def test_priority_fifo_and_round_robin_are_deterministic(tmp_path):
    storage = Storage(tmp_path / "fair.db")
    storage.initialize()
    tracker = SchedulerTracker()
    admission = JobService(
        storage,
        SchedulingProvider(tracker),
        start_worker=False,
        provider_factory=lambda: SchedulingProvider(tracker),
    )
    submitted = []
    for label, source, priority in [
        ("low", "low", 0),
        ("a1", "a", 1),
        ("a2", "a", 1),
        ("b1", "b", 1),
        ("b2", "b", 1),
    ]:
        request = CollectionRequest.from_dict(
            {"sourceType": "profile", "sourceValue": source, "maxPosts": 10}
        )
        submitted.append(
            admission.submit(
                request,
                scheduled_plan(request, source, "auth", label),
                priority=priority,
                auth_state_id="auth",
            )
        )

    admission.shutdown()
    service = JobService(
        storage,
        SchedulingProvider(tracker),
        start_worker=False,
        provider_factory=lambda: SchedulingProvider(tracker),
    )
    service.start()
    statuses = wait_for_jobs(storage, submitted)
    service.shutdown()

    assert tracker.order == ["a1", "b1", "a2", "b2", "low"]
    assert set(statuses.values()) == {"succeeded"}


def test_twenty_jobs_respect_caps_cancel_independently_and_clean_up(tmp_path):
    storage = Storage(tmp_path / "stress.db")
    storage.initialize()
    tracker = SchedulerTracker(delay=0.03)
    admission = JobService(
        storage,
        SchedulingProvider(tracker),
        start_worker=False,
        max_workers=4,
        max_queue=20,
        provider_factory=lambda: SchedulingProvider(tracker),
    )
    job_ids = []
    for index in range(20):
        source = f"s{index % 5}"
        auth = f"auth{index % 2}"
        request = CollectionRequest.from_dict(
            {"sourceType": "profile", "sourceValue": source, "maxPosts": 10}
        )
        job_ids.append(
            admission.submit(
                request,
                scheduled_plan(request, source, auth, str(index)),
                auth_state_id=auth,
            )
        )

    admission.shutdown()
    service = JobService(
        storage,
        SchedulingProvider(tracker),
        start_worker=False,
        max_workers=4,
        max_queue=20,
        provider_factory=lambda: SchedulingProvider(tracker),
    )
    service.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and service.metrics()["started"] == 0:
        threading.Event().wait(0.002)
    assert service.cancel(job_ids[0])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and service.metrics()["finished"] < 20:
        threading.Event().wait(0.01)
    before_shutdown = service.metrics()
    assert before_shutdown["finished"] == 20
    statuses = {job_id: storage.get_job(job_id)["status"] for job_id in job_ids}
    service.shutdown()
    after_shutdown = service.metrics()

    assert statuses[job_ids[0]] == "cancelled"
    assert all(status == "succeeded" for job_id, status in statuses.items() if job_id != job_ids[0])
    assert tracker.max_active <= 2
    assert tracker.max_source == tracker.max_auth == 1
    assert tracker.active == 0
    assert before_shutdown["queueDepth"] == before_shutdown["activeWorkers"] == 0
    assert before_shutdown["started"] == before_shutdown["finished"] == 20
    assert before_shutdown["cancelRequests"] == 1
    assert before_shutdown["queueWaitP50Ms"] >= 0
    assert before_shutdown["queueWaitP95Ms"] >= before_shutdown["queueWaitP50Ms"]
    assert after_shutdown["cleanupFailures"] == 0
    assert not service._lock_path.exists()
    assert not any(thread.is_alive() for thread in service._threads)


def test_simultaneous_idempotency_resolves_to_one_job(tmp_path):
    storage = Storage(tmp_path / "idempotency.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    plan = compile_request(request)
    service = JobService(storage, Provider("ok"), start_worker=False, max_queue=1)
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    approval = {
        "approvedAt": "2026-08-19T05:00:00+00:00",
        "confirmation": "test_fixture",
    }
    limits = {
        "maxPosts": 10,
        "deadlineSeconds": 30,
        "routeAlias": "direct",
        "maxConcurrency": 2,
    }

    with ThreadPoolExecutor(max_workers=20) as pool:
        job_ids = list(
            pool.map(
                lambda _: service.submit(
                    request,
                    plan,
                    idempotency_key="same-approval",
                    approval=approval,
                    limits=limits,
                    deadline_at=deadline,
                ),
                range(100),
            )
        )

    metrics = service.metrics()
    assert len(set(job_ids)) == 1
    assert len(storage.list_jobs(100)) == 1
    assert metrics["submitted"] == 1 and metrics["deduplicated"] == 99
    assert metrics["queueDepth"] == 1

    service.shutdown()
    restarted = JobService(storage, Provider("ok"), start_worker=False, max_queue=1)
    same_job = restarted.submit(
        request,
        plan,
        idempotency_key="same-approval",
        approval=approval,
        limits=limits,
        deadline_at=deadline,
    )
    restarted.start()
    statuses = wait_for_jobs(storage, [same_job])
    restarted.shutdown()

    assert same_job == job_ids[0]
    assert statuses[same_job] == "succeeded"
    assert restarted.metrics()["deduplicated"] == 1


def test_worker_heartbeats_its_lease(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "heartbeat.db")
    storage.initialize()
    tracker = SchedulerTracker(delay=0.5)
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "heartbeat", "maxPosts": 10}
    )
    service = JobService(
        storage,
        SchedulingProvider(tracker),
        start_worker=False,
        provider_factory=lambda: SchedulingProvider(tracker),
        lease_seconds=1,
    )
    job_id = service.submit(
        request,
        scheduled_plan(request, "heartbeat", "auth", "heartbeat"),
        auth_state_id="auth",
    )
    heartbeat = storage.heartbeat_job
    calls = 0

    def counted_heartbeat(*args, **kwargs):
        nonlocal calls
        calls += 1
        return heartbeat(*args, **kwargs)

    monkeypatch.setattr(storage, "heartbeat_job", counted_heartbeat)
    service.start()
    statuses = wait_for_jobs(storage, [job_id])
    service.shutdown()

    assert statuses[job_id] == "succeeded"
    assert calls >= 1


def test_slow_persistence_applies_bounded_backpressure(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "slow-persistence.db")
    storage.initialize()
    original = storage.add_posts
    persistence_lock = threading.Lock()
    persistence_active = 0
    persistence_peak = 0

    def slow_add(*args, **kwargs):
        nonlocal persistence_active, persistence_peak
        with persistence_lock:
            persistence_active += 1
            persistence_peak = max(persistence_peak, persistence_active)
        try:
            threading.Event().wait(0.02)
            return original(*args, **kwargs)
        finally:
            with persistence_lock:
                persistence_active -= 1

    monkeypatch.setattr(storage, "add_posts", slow_add)
    service = JobService(
        storage,
        PersistenceProvider(),
        start_worker=False,
        max_workers=4,
        max_queue=8,
        provider_factory=PersistenceProvider,
    )
    job_ids = []
    for index in range(8):
        request = CollectionRequest.from_dict(
            {"sourceType": "profile", "sourceValue": f"p{index}", "maxPosts": 10}
        )
        job_ids.append(
            service.submit(
                request,
                scheduled_plan(request, f"p{index}", f"auth{index}", str(index)),
                auth_state_id=f"auth{index}",
            )
        )
    service.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and service.metrics()["finished"] < 8:
        threading.Event().wait(0.01)
    metrics = service.metrics()
    service.shutdown()
    statuses = {job_id: storage.get_job(job_id)["status"] for job_id in job_ids}

    assert set(statuses.values()) == {"succeeded"}
    assert persistence_peak == 1
    assert 1 < metrics["maxPersistenceBacklog"] <= 4
    assert metrics["persistenceActive"] == metrics["persistenceWaiting"] == 0


def test_provider_crash_is_isolated_from_sibling_jobs(tmp_path):
    storage = Storage(tmp_path / "provider-crash.db")
    storage.initialize()
    service = JobService(
        storage,
        PersistenceProvider(),
        start_worker=False,
        max_workers=3,
        max_queue=6,
        provider_factory=PersistenceProvider,
    )
    job_ids = []
    for index in range(6):
        label = "crash" if index == 2 else str(index)
        request = CollectionRequest.from_dict(
            {"sourceType": "profile", "sourceValue": f"c{index}", "maxPosts": 10}
        )
        job_ids.append(
            service.submit(
                request,
                scheduled_plan(request, f"c{index}", f"auth{index}", label),
                auth_state_id=f"auth{index}",
            )
        )
    service.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and service.metrics()["finished"] < 6:
        threading.Event().wait(0.01)
    service.shutdown()
    jobs = {job_id: storage.get_job(job_id) for job_id in job_ids}

    assert jobs[job_ids[2]]["status"] == "failed"
    assert jobs[job_ids[2]]["error_code"] == "unexpected_error"
    assert all(jobs[job_id]["status"] == "succeeded" for job_id in job_ids if job_id != job_ids[2])
    assert storage.queue_counts()["leased"] == 0


def test_expired_lease_recovers_as_a_new_attempt_and_segment(tmp_path):
    storage = Storage(tmp_path / "expired-lease.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "recovery", "maxPosts": 10}
    )
    plan = compile_request(request)
    admission = JobService(storage, Provider("ok"), start_worker=False)
    job_id = admission.submit(request, plan)
    first = storage.lease_job(
        job_id,
        worker_id="dead-worker",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert first and first["attempt_number"] == 1 and first["capture_segment"] == 0
    with storage.connect() as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), job_id),
        )
    admission.shutdown()

    recovered = JobService(storage, Provider("ok"), start_worker=False)
    recovered.start()
    statuses = wait_for_jobs(storage, [job_id])
    recovered.shutdown()
    job = storage.get_job(job_id)

    assert statuses[job_id] == "succeeded"
    assert job["attempt_number"] == 2 and job["capture_segment"] == 1


def test_queue_bound_and_isolated_factory_are_required(tmp_path):
    storage = Storage(tmp_path / "bounded.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    plan = compile_request(request)
    service = JobService(storage, Provider("ok"), start_worker=False, max_queue=1)
    service.submit(request, plan)

    with pytest.raises(QueueFullError, match="queue is full"):
        service.submit(request, plan)
    with pytest.raises(ValueError, match="isolated provider_factory"):
        JobService(storage, Provider("ok"), start_worker=False, max_workers=2)

    assert len(storage.list_jobs(100)) == 1
    assert service.metrics()["rejected"] == 1
