from __future__ import annotations

import gc
import hashlib
import json
import os
import platform
import resource
import subprocess
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from test_playwright_integration import TIMELINE, _FixturePlaywright

import xworkbench.api as api_module
from xworkbench.config import Settings
from xworkbench.jobs import JobService
from xworkbench.models import CollectionRequest, CollectionSummary, Post, ProviderType
from xworkbench.playwright_browser import PlaywrightBrowserProvider, _record_status
from xworkbench.providers import ProviderRegistry
from xworkbench.storage import Storage
from xworkbench.x_api import XApiProvider

RUN_BROWSER_MATRIX = os.environ.get("XWORKBENCH_RUN_BROWSER_MATRIX") == "1"
ASSERT_SCALE_THRESHOLDS = os.environ.get("XWORKBENCH_ASSERT_SCALE_THRESHOLDS") == "1"
RSS_GROWTH_LIMIT_BYTES = 32 * 1024 * 1024


def _wait_for_jobs(service: JobService, target_finished: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        metrics = service.metrics()
        if (
            metrics["finished"] >= target_finished
            and metrics["queueDepth"] == 0
            and metrics["activeWorkers"] == 0
        ):
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"Jobs did not finish: {service.metrics()}")


def _rss_bytes() -> int:
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return int(result.stdout.strip()) * 1024
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(maximum if platform.system() == "Darwin" else maximum * 1024)


def _process_tree_rss_bytes(root_pid: int) -> int:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return _rss_bytes()
    children: dict[int, list[int]] = defaultdict(list)
    rss: dict[int, int] = {}
    for line in result.stdout.splitlines():
        try:
            pid, parent, kib = (int(value) for value in line.split())
        except (TypeError, ValueError):
            continue
        children[parent].append(pid)
        rss[pid] = kib * 1024
    total = 0
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0)
        pending.extend(children.get(pid, ()))
    return total or _rss_bytes()


def _descendant_commands(root_pid: int) -> list[str]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = defaultdict(list)
    commands: dict[int, str] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3:
            continue
        try:
            pid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children[parent].append(pid)
        commands[pid] = parts[2]
    descendants: list[str] = []
    pending = list(children.get(root_pid, ()))
    while pending:
        pid = pending.pop()
        descendants.append(commands.get(pid, ""))
        pending.extend(children.get(pid, ()))
    return descendants


def _cpu_seconds() -> float:
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


class _ResourceSampler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self.peak_rss_bytes = _process_tree_rss_bytes(os.getpid())
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self._stop.wait(0.05):
            self.peak_rss_bytes = max(
                self.peak_rss_bytes, _process_tree_rss_bytes(os.getpid())
            )

    def __enter__(self) -> _ResourceSampler:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.peak_rss_bytes = max(
            self.peak_rss_bytes, _process_tree_rss_bytes(os.getpid())
        )


class _LifecycleTracker:
    def __init__(self, delay: float = 0) -> None:
        self.delay = delay
        self._lock = threading.Lock()
        self.active = 0
        self.active_sources: dict[str, int] = defaultdict(int)
        self.active_auth: dict[str, int] = defaultdict(int)
        self.max_active = 0
        self.max_source = 0
        self.max_auth = 0
        self.persistence_seconds = 0.0
        self.cleanup_seconds = 0.0
        self.cleanup_failures = 0
        self.cleanup_states: list[dict[str, object]] = []
        self.external_requests: list[str] = []

    def enter(self, source: str, auth: str) -> None:
        with self._lock:
            self.active += 1
            self.active_sources[source] += 1
            self.active_auth[auth] += 1
            self.max_active = max(self.max_active, self.active)
            self.max_source = max(self.max_source, self.active_sources[source])
            self.max_auth = max(self.max_auth, self.active_auth[auth])

    def leave(self, source: str, auth: str) -> None:
        with self._lock:
            self.active -= 1
            self.active_sources[source] -= 1
            self.active_auth[auth] -= 1

    def record_persistence(self, elapsed: float) -> None:
        with self._lock:
            self.persistence_seconds += elapsed


class _ConcurrentStorageReader:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.stop_event = threading.Event()
        self.errors: list[Exception] = []
        self.reads = 0
        self.thread = threading.Thread(target=self._poll, daemon=True)

    def _poll(self) -> None:
        while not self.stop_event.wait(0.001):
            try:
                self.storage.queue_counts()
                self.storage.list_jobs(1)
                self.reads += 2
            except Exception as exc:
                self.errors.append(exc)
                return

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)


class _LightweightProvider:
    provider_id = ProviderType.PLAYWRIGHT_BROWSER
    provider_version = 2

    def __init__(self, tracker: _LifecycleTracker) -> None:
        self.tracker = tracker

    def collect(self, request, *, execution_plan, checkpoint, on_batch, should_cancel):
        source = execution_plan["benchmarkSource"]
        auth = execution_plan["benchmarkAuth"]
        self.tracker.enter(source, auth)
        try:
            threading.Event().wait(self.tracker.delay)
            assert not should_cancel()
            started = time.monotonic()
            on_batch(
                [
                    Post(
                        execution_plan["benchmarkPostId"],
                        "sanitized fixture",
                        "fixture",
                        f"https://x.com/fixture/status/{execution_plan['benchmarkPostId']}",
                        None,
                    )
                ],
                None,
                {"fixture": "queue-performance"},
            )
            self.tracker.record_persistence(time.monotonic() - started)
            return CollectionSummary(completion_reason="target_reached")
        finally:
            self.tracker.leave(source, auth)


def _browser_request(handle: str) -> CollectionRequest:
    return CollectionRequest.from_dict(
        {
            "provider": "playwright_browser",
            "sourceType": "profile",
            "sourceValue": handle,
            "maxPosts": 1,
        }
    )


def test_repeated_hundred_job_drains_are_bounded_and_leave_no_resources(tmp_path):
    storage = Storage(tmp_path / "queue-stress.db")
    storage.initialize()
    tracker = _LifecycleTracker(delay=0.001)
    service = JobService(
        storage,
        _LightweightProvider(tracker),
        start_worker=False,
        max_workers=2,
        max_queue=100,
        provider_factory=lambda: _LightweightProvider(tracker),
    )
    reader = _ConcurrentStorageReader(storage)
    reader.start()
    rss_after_round: list[int] = []
    round_seconds: list[float] = []
    cpu_started = _cpu_seconds()

    for round_number in range(3):
        started = time.monotonic()
        job_ids = []
        for index in range(100):
            source = f"s{index % 7}"
            auth = f"auth{index % 3}"
            post_id = str(100_000 + round_number * 100 + index)
            request = _browser_request(source)
            job_ids.append(
                service.submit(
                    request,
                    {
                        "provider": "playwright_browser",
                        "providerVersion": 2,
                        "benchmarkSource": source,
                        "benchmarkAuth": auth,
                        "benchmarkPostId": post_id,
                    },
                    auth_state_id=auth,
                    idempotency_key=f"round-{round_number}-job-{index}",
                )
            )
        if round_number == 0:
            service.start()
        _wait_for_jobs(service, (round_number + 1) * 100)
        round_seconds.append(time.monotonic() - started)
        placeholders = ",".join("?" for _ in job_ids)
        with storage.connect() as connection:
            states = connection.execute(
                f"SELECT status, COUNT(*) FROM jobs WHERE id IN ({placeholders}) "  # noqa: S608
                "GROUP BY status",
                job_ids,
            ).fetchall()
            observations = connection.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT job_id) FROM post_observations "  # noqa: S608
                f"WHERE job_id IN ({placeholders})",
                job_ids,
            ).fetchone()
        assert [(row[0], row[1]) for row in states] == [("succeeded", 100)]
        assert tuple(observations) == (100, 100)
        assert storage.queue_counts() == {
            "queued": 0,
            "running": 0,
            "waiting": 0,
            "leased": 0,
            "active": 0,
        }
        metrics = service.metrics()
        assert metrics["queueDepth"] == metrics["activeWorkers"] == 0
        assert metrics["persistenceActive"] == metrics["persistenceWaiting"] == 0
        gc.collect()
        rss_after_round.append(_rss_bytes())

    metrics = service.metrics()
    service.shutdown()
    reader.stop()
    rss_growth = max(rss_after_round[1:]) - rss_after_round[0]
    summary = {
        "jobs": 300,
        "rounds": 3,
        "jobsPerRound": 100,
        "workers": 2,
        "wallSeconds": round(sum(round_seconds), 3),
        "roundSeconds": [round(value, 3) for value in round_seconds],
        "cpuSeconds": round(_cpu_seconds() - cpu_started, 3),
        "rssAfterRoundBytes": rss_after_round,
        "postWarmupRssGrowthBytes": rss_growth,
        "rssGrowthLimitBytes": RSS_GROWTH_LIMIT_BYTES,
        "persistenceSeconds": round(tracker.persistence_seconds, 3),
        "maxPersistenceBacklog": metrics["maxPersistenceBacklog"],
        "concurrentStorageReads": reader.reads,
    }
    print("QUEUE_STRESS_SUMMARY=" + json.dumps(summary, sort_keys=True))

    assert rss_growth <= RSS_GROWTH_LIMIT_BYTES
    assert reader.reads >= 100 and reader.errors == []
    assert not reader.thread.is_alive()
    assert tracker.max_active <= 2
    assert tracker.max_source == tracker.max_auth == 1
    assert tracker.active == 0
    assert metrics["started"] == metrics["finished"] == 300
    assert metrics["completedByStatus"] == {"succeeded": 300}
    assert service.metrics()["cleanupFailures"] == 0
    assert not service._lock_path.exists()
    assert not any(thread.is_alive() for thread in service._threads)
    assert not list(tmp_path.rglob("*.worker.lock"))
    assert not [path for path in tmp_path.rglob("*") if path.name.startswith(".playwright")]


class _DelayedFixtureHandler(BaseHTTPRequestHandler):
    html = TIMELINE.read_bytes()
    delay_seconds = 0.2

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.server.request_count += 1
        threading.Event().wait(self.delay_seconds)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.html)))
        self.end_headers()
        self.wfile.write(self.html)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _loopback_fixture():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DelayedFixtureHandler)
    server.request_count = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/timeline", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


class _LoopbackPlaywright(_FixturePlaywright):
    def __init__(self, html: str, loopback_url: str) -> None:
        super().__init__(html)
        self.loopback_url = loopback_url
        self.loopback_requests: list[str] = []

    def route(self, route):
        request = route.request
        if request.is_navigation_request() and request.url == self.destination:
            self.served.append(request.url)
            route.fulfill(status=307, headers={"Location": self.loopback_url}, body="")
            return
        parsed = urlsplit(request.url)
        expected = urlsplit(self.loopback_url)
        if (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port == expected.port
            and parsed.username is None
            and parsed.password is None
        ):
            self.loopback_requests.append(request.url)
            route.continue_()
            return
        if parsed.scheme == "data":
            route.continue_()
            return
        self.blocked.append(request.url)
        route.abort()


def _browser_settings(root: Path, name: str) -> Settings:
    state_path = root / "auth" / f"{name}.json"
    state_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    state_path.parent.chmod(0o700)
    state_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    state_path.chmod(0o600)
    settings = Settings(
        database_path=root / "unused.db",
        bearer_token_path=root / "api-token",
        storage_state_path=state_path,
        browser_headless=True,
        job_timeout_seconds=15,
        page_timeout_ms=3_000,
        no_progress_limit=1,
    )
    assert _record_status(settings, "verified_live")
    return settings


class _MeasuredBrowserProvider:
    provider_id = ProviderType.PLAYWRIGHT_BROWSER
    provider_version = 2

    def __init__(
        self,
        settings: Settings,
        loopback_url: str,
        tracker: _LifecycleTracker,
        auth_id: str,
    ) -> None:
        self.settings = settings
        self.loopback_url = loopback_url
        self.tracker = tracker
        self.auth_id = auth_id
        self._preparer = PlaywrightBrowserProvider(settings)

    def prepare(self, request, supplied_plan=None):
        return self._preparer.prepare(request, supplied_plan)

    def collect(self, request, *, execution_plan, checkpoint, on_batch, should_cancel):
        fixture = _LoopbackPlaywright(TIMELINE.read_text(encoding="utf-8"), self.loopback_url)
        fixture.destination = execution_plan["sourceUrl"]
        provider = PlaywrightBrowserProvider(
            self.settings, _playwright_factory=lambda: fixture
        )
        self.tracker.enter(request.source_value, self.auth_id)
        cleanup_started = 0.0
        try:
            def measured_batch(posts, state, metadata):
                started = time.monotonic()
                try:
                    return on_batch(posts, state, metadata)
                finally:
                    self.tracker.record_persistence(time.monotonic() - started)

            return provider.collect(
                request,
                execution_plan=execution_plan,
                checkpoint=checkpoint,
                on_batch=measured_batch,
                should_cancel=should_cancel,
            )
        finally:
            cleanup_started = time.monotonic()
            fixture.stop()
            self.tracker.cleanup_seconds += time.monotonic() - cleanup_started
            cleanup_state = {
                "closed": sorted(fixture.closed),
            }
            self.tracker.cleanup_states.append(cleanup_state)
            if set(cleanup_state["closed"]) != {"page", "context", "browser"}:
                self.tracker.cleanup_failures += 1
            self.tracker.external_requests.extend(fixture.blocked)
            self.tracker.leave(request.source_value, self.auth_id)


class _FixtureResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode()
        self.headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.payload


class _OfficialFixtureTransport:
    def __init__(self, tracker: _LifecycleTracker) -> None:
        self.tracker = tracker

    def __call__(self, request, *, timeout):
        del timeout
        parsed = urlsplit(request.full_url)
        if parsed.scheme != "https" or parsed.hostname != "api.x.com":
            self.tracker.external_requests.append(request.full_url)
            raise AssertionError("Official fixture received an unexpected destination.")
        with self.tracker._lock:
            self.tracker.official_requests += 1
        threading.Event().wait(0.45)
        return _FixtureResponse(
            {
                "data": [
                    {
                        "id": str(9_100 + index),
                        "text": "sanitized official fixture",
                        "author_id": "fixture-author",
                    }
                    for index in range(10)
                ],
                "meta": {"result_count": 10},
            }
        )


class _MeasuredOfficialProvider:
    provider_id = ProviderType.OFFICIAL_X_API
    provider_version = 2

    def __init__(self, settings: Settings, tracker: _LifecycleTracker, auth_id: str) -> None:
        self.tracker = tracker
        self.auth_id = auth_id
        self.provider = XApiProvider(settings, opener=_OfficialFixtureTransport(tracker))

    def capabilities(self):
        return self.provider.capabilities()

    def connection_status(self):
        return self.provider.connection_status()

    def prepare(self, request, supplied_plan=None):
        return self.provider.prepare(request, supplied_plan)

    def collect(self, request, *, execution_plan, checkpoint, on_batch, should_cancel):
        self.tracker.enter(request.source_value, self.auth_id)
        try:

            def measured_batch(posts, state, metadata):
                started = time.monotonic()
                try:
                    return on_batch(posts, state, metadata)
                finally:
                    self.tracker.record_persistence(time.monotonic() - started)

            return self.provider.collect(
                request,
                execution_plan=execution_plan,
                checkpoint=checkpoint,
                on_batch=measured_batch,
                should_cancel=should_cancel,
            )
        finally:
            self.tracker.leave(request.source_value, self.auth_id)


def _run_reachable_matrix_case(
    tmp_path: Path, loopback_url: str, workers: int, repetition: int
) -> dict:
    root = tmp_path / f"mixed-{workers}-{repetition}"
    root.mkdir(mode=0o700)
    token_path = root / "auth" / "token"
    token_path.parent.mkdir(parents=True, mode=0o700)
    token_path.parent.chmod(0o700)
    token_path.write_text("synthetic-benchmark-token", encoding="utf-8")
    token_path.chmod(0o600)
    settings = Settings(
        root / "queue.db",
        token_path,
        max_workers=workers,
        queue_capacity=2,
        resource_max_rss_mb=131_072,
        resource_max_cpu_percent=1_000,
    )
    tracker = _LifecycleTracker()
    tracker.official_requests = 0
    provider_index = 0

    def registry_factory(_settings: Settings) -> ProviderRegistry:
        nonlocal provider_index
        provider_index += 1
        name = f"registry-{provider_index}"
        return ProviderRegistry(
            [
                _MeasuredBrowserProvider(
                    _browser_settings(root, name),
                    loopback_url,
                    tracker,
                    f"browser-{name}",
                ),
                _MeasuredOfficialProvider(settings, tracker, f"official-{name}"),
            ]
        )

    with patch.object(api_module, "_default_registry", registry_factory):
        app = api_module.create_app(settings)
    app.config.update(TESTING=True)
    client = app.test_client()
    service = app.extensions["xworkbench_jobs"]
    storage = service.storage
    for body in (
        {
            "displayName": "Reachable Browser fixture",
            "provider": "playwright_browser",
            "surface": "profile",
            "value": "matrixbrowser",
        },
        {
            "displayName": "Reachable official fixture",
            "provider": "official_x_api",
            "surface": "profile",
            "value": "matrixofficial",
        },
    ):
        assert client.post("/api/sources", json=body).status_code == 201
    sources = {
        source["provider"]: source
        for source in client.get("/api/sources?limit=25").get_json()["sources"]
    }
    preview_response = client.post(
        "/api/batches/preview",
        json={
            "items": [
                {
                    "sourceId": sources["playwright_browser"]["sourceId"],
                    "maxPosts": 1,
                    "priority": 0,
                },
                {
                    "sourceId": sources["official_x_api"]["sourceId"],
                    "maxPosts": 10,
                    "priority": 0,
                },
            ],
            "deadlineSeconds": 600,
            "freshnessChoice": "capture_fresh",
        },
    )
    assert preview_response.status_code == 200
    preview = preview_response.get_json()
    assert preview["manifest"]["maxConcurrency"] == workers
    assert preview["manifest"]["perAuthStateConcurrency"] == 1

    wall_started = time.monotonic()
    cpu_started = _cpu_seconds()
    try:
        with _ResourceSampler() as resources:
            confirmed = client.post(
                "/api/batches/confirm",
                json={
                    "confirm": True,
                    "manifest": preview["manifest"],
                    "approvalDigest": preview["approvalDigest"],
                },
            )
            assert confirmed.status_code == 202
            job_ids = confirmed.get_json()["jobIds"]
            with service._condition:
                assert service._condition.wait_for(lambda: service._finished == 2, timeout=60)
        wall_seconds = time.monotonic() - wall_started
        cpu_seconds = _cpu_seconds() - cpu_started
        metrics = service.metrics()
        jobs = [storage.get_job(job_id) for job_id in job_ids]
        results = {
            job["provider"]: sorted(
                post["post_id"] for post in storage.get_job_posts(job["id"])
            )
            for job in jobs
        }
        stable_state = [
            {
                "provider": job["provider"],
                "sourceValue": job["request"]["sourceValue"],
                "status": job["status"],
                "completionReason": job["completion_reason"],
                "collectedCount": job["collected_count"],
                "postIds": results[job["provider"]],
            }
            for job in sorted(jobs, key=lambda value: value["provider"])
        ]
        digest = hashlib.sha256(
            json.dumps(stable_state, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        all_post_ids = [post_id for values in results.values() for post_id in values]
        assert [job["status"] for job in jobs] == ["succeeded", "succeeded"]
        assert results == {
            "official_x_api": [str(value) for value in range(9_100, 9_110)],
            "playwright_browser": ["2001"],
        }
        assert tracker.max_active == workers
        assert tracker.max_source == tracker.max_auth == 1
        assert tracker.active == 0
        assert tracker.external_requests == []
        assert tracker.cleanup_failures == 0, tracker.cleanup_states
        assert storage.queue_counts() == {
            "queued": 0,
            "running": 0,
            "waiting": 0,
            "leased": 0,
            "active": 0,
        }
        assert metrics["maxPersistenceBacklog"] <= 2
        assert len(all_post_ids) == len(set(all_post_ids)) == 11
    finally:
        service.shutdown()

    chromium_descendants = [
        command
        for command in _descendant_commands(os.getpid())
        if "chromium" in command.casefold()
    ]
    assert service.metrics()["cleanupFailures"] == 0
    assert not service._lock_path.exists()
    assert not any(thread.is_alive() for thread in service._threads)
    assert chromium_descendants == []
    sqlite_fraction = tracker.persistence_seconds / wall_seconds
    return {
        "workers": workers,
        "repetition": repetition,
        "jobs": 2,
        "wallSeconds": round(wall_seconds, 3),
        "cpuSeconds": round(cpu_seconds, 3),
        "peakProcessTreeRssBytes": resources.peak_rss_bytes,
        "queueWaitP50Ms": round(metrics["queueWaitP50Ms"] or 0, 3),
        "queueWaitP95Ms": round(metrics["queueWaitP95Ms"] or 0, 3),
        "throughputJobsPerSecond": round(metrics["throughputJobsPerSecond"], 3),
        "sqliteCallbackSeconds": round(tracker.persistence_seconds, 6),
        "sqliteCallbackFraction": round(sqlite_fraction, 6),
        "maxPersistenceBacklog": metrics["maxPersistenceBacklog"],
        "jobIds": job_ids,
        "resultIds": results,
        "duplicateCount": len(all_post_ids) - len(set(all_post_ids)),
        "remainingLeases": storage.queue_counts()["leased"],
        "remainingWorkerThreads": sum(thread.is_alive() for thread in service._threads),
        "remainingChromiumDescendants": len(chromium_descendants),
        "stateDigest": digest,
        "cleanupSeconds": round(service.metrics()["cleanupSeconds"], 6),
        "cleanupFailures": service.metrics()["cleanupFailures"],
        "externalEgressCount": len(tracker.external_requests),
        "syntheticOfficialRequests": tracker.official_requests,
        "peakCollectors": tracker.max_active,
    }


@pytest.mark.skipif(
    not RUN_BROWSER_MATRIX,
    reason="set XWORKBENCH_RUN_BROWSER_MATRIX=1 in the installed-Chromium job",
)
def test_production_playwright_mixed_provider_matrix_is_reachable(tmp_path):
    with _loopback_fixture() as (loopback_url, fixture_server):
        runs = [
            _run_reachable_matrix_case(tmp_path, loopback_url, workers, repetition)
            for workers in (1, 2)
            for repetition in range(1, 4)
        ]
        assert fixture_server.request_count == 6

    serial = [run for run in runs if run["workers"] == 1]
    concurrent = [run for run in runs if run["workers"] == 2]
    serial_wall = median(run["wallSeconds"] for run in serial)
    concurrent_wall = median(run["wallSeconds"] for run in concurrent)
    decision = {
        "speedupAtLeast15Percent": concurrent_wall <= serial_wall * 0.85,
        "cpuGrowthAtMost25Percent": median(run["cpuSeconds"] for run in concurrent)
        <= median(run["cpuSeconds"] for run in serial) * 1.25,
        "incrementalRssAtMost128MiB": (
            median(run["peakProcessTreeRssBytes"] for run in concurrent)
            - median(run["peakProcessTreeRssBytes"] for run in serial)
            <= 128 * 1024 * 1024
        ),
        "sqliteFractionBelow20Percent": median(
            run["sqliteCallbackFraction"] for run in concurrent
        )
        < 0.20,
        "backlogAtMost2": max(run["maxPersistenceBacklog"] for run in runs) <= 2,
        "correctnessCleanupZeroEgress": all(
            run["duplicateCount"] == 0
            and run["remainingLeases"] == 0
            and run["remainingWorkerThreads"] == 0
            and run["remainingChromiumDescendants"] == 0
            and run["cleanupFailures"] == 0
            and run["externalEgressCount"] == 0
            for run in runs
        ),
    }
    summary = {
        "fixture": "production-routes-loopback-browser-synthetic-official",
        "repetitionsPerCase": 3,
        "runs": runs,
        "medianSpeedup": round(serial_wall / concurrent_wall, 3),
        "stableStateDigest": len({run["stateDigest"] for run in runs}) == 1,
        "decision": decision,
        "supportedMaximum": 2 if all(decision.values()) else 1,
    }
    print("REACHABLE_MIXED_PROVIDER_MATRIX=" + json.dumps(summary, sort_keys=True))

    assert summary["stableStateDigest"] is True
    assert decision["backlogAtMost2"] is True
    assert decision["correctnessCleanupZeroEgress"] is True
    if ASSERT_SCALE_THRESHOLDS:
        assert all(decision.values()), summary


def test_recorded_benchmarks_separate_historical_and_reachable_evidence():
    benchmark_dir = Path(__file__).resolve().parents[1] / "docs" / "benchmarks"
    historical = json.loads(
        (benchmark_dir / "queue-performance-2026-08-19.json").read_text()
    )
    old_matrix = historical["realBrowserMatrix"]
    assert old_matrix["speedup"] == 2.415
    assert old_matrix["productionAdmission"] is False
    assert old_matrix["productionReachable"] is False
    assert "unique auth_state_id" in old_matrix["authStateModel"]

    reachable = json.loads(
        (benchmark_dir / "reachable-mixed-provider-2026-08-20.json").read_text()
    )
    runs = reachable["runs"]
    assert len(runs) == 6
    assert [run["workers"] for run in runs].count(1) == 3
    assert [run["workers"] for run in runs].count(2) == 3
    serial = [run for run in runs if run["workers"] == 1]
    concurrent = [run for run in runs if run["workers"] == 2]
    assert reachable["medians"] == {
        "oneWorker": {
            "wallSeconds": median(run["wallSeconds"] for run in serial),
            "cpuSeconds": median(run["cpuSeconds"] for run in serial),
            "peakProcessTreeRssBytes": median(
                run["peakProcessTreeRssBytes"] for run in serial
            ),
        },
        "twoWorkers": {
            "wallSeconds": median(run["wallSeconds"] for run in concurrent),
            "cpuSeconds": median(run["cpuSeconds"] for run in concurrent),
            "peakProcessTreeRssBytes": median(
                run["peakProcessTreeRssBytes"] for run in concurrent
            ),
            "sqliteCallbackFraction": median(
                run["sqliteCallbackFraction"] for run in concurrent
            ),
        },
        "speedup": 1.793,
        "incrementalRssBytes": -14_286_848,
    }
    assert len({run["stateDigest"] for run in runs}) == 1
    assert all(
        run["duplicateCount"] == run["remainingLeases"] == 0
        and run["remainingWorkerThreads"] == run["remainingChromiumDescendants"] == 0
        and run["cleanupFailures"] == run["externalEgressCount"] == 0
        and run["maxPersistenceBacklog"] <= 2
        for run in runs
    )
    assert reachable["decision"]["supportedMaximum"] == 2
    assert all(
        value is True
        for key, value in reachable["decision"].items()
        if key not in {"supportedMaximum", "scope"}
    )
