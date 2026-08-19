from __future__ import annotations

import gc
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
from urllib.parse import urlsplit

import pytest
from test_playwright_integration import TIMELINE, _FixturePlaywright

from xworkbench.config import Settings
from xworkbench.jobs import JobService
from xworkbench.models import CollectionRequest, CollectionSummary, Post, ProviderType
from xworkbench.playwright_browser import PlaywrightBrowserProvider, _record_status
from xworkbench.storage import Storage

RUN_BROWSER_MATRIX = os.environ.get("XWORKBENCH_RUN_BROWSER_MATRIX") == "1"
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
    }
    print("QUEUE_STRESS_SUMMARY=" + json.dumps(summary, sort_keys=True))

    assert rss_growth <= RSS_GROWTH_LIMIT_BYTES
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


def _run_browser_matrix_case(tmp_path: Path, loopback_url: str, workers: int) -> dict:
    root = tmp_path / f"browser-{workers}"
    storage = Storage(root / "queue.db")
    storage.initialize()
    tracker = _LifecycleTracker()
    provider_index = 0

    def provider(name: str) -> _MeasuredBrowserProvider:
        return _MeasuredBrowserProvider(
            _browser_settings(root, name), loopback_url, tracker, name
        )

    admission = provider("admission")

    def provider_factory() -> _MeasuredBrowserProvider:
        nonlocal provider_index
        provider_index += 1
        return provider(f"worker-{provider_index}")

    service = JobService(
        storage,
        admission,
        start_worker=False,
        max_workers=workers,
        max_queue=4,
        provider_factory=provider_factory if workers > 1 else None,
    )
    job_ids = []
    for index in range(4):
        request = _browser_request(f"matrix{index}")
        job_ids.append(
            service.submit(
                request,
                admission.prepare(request),
                auth_state_id=f"approved-auth-{index}",
                idempotency_key=f"browser-{workers}-{index}",
            )
        )
    wall_started = time.monotonic()
    cpu_started = _cpu_seconds()
    with _ResourceSampler() as resources:
        service.start()
        _wait_for_jobs(service, 4, timeout=60)
    wall_seconds = time.monotonic() - wall_started
    cpu_seconds = _cpu_seconds() - cpu_started
    metrics = service.metrics()
    service.shutdown()
    jobs = [storage.get_job(job_id) for job_id in job_ids]
    post_ids = [storage.get_job_posts(job_id)[0]["post_id"] for job_id in job_ids]

    assert all(job["status"] == "succeeded" for job in jobs)
    assert post_ids == ["2001"] * 4
    assert tracker.max_active <= workers
    assert tracker.max_source == tracker.max_auth == 1
    assert tracker.active == 0
    assert tracker.external_requests == []
    assert tracker.cleanup_failures == 0, tracker.cleanup_states
    assert storage.queue_counts()["leased"] == 0
    assert metrics["queueDepth"] == metrics["activeWorkers"] == 0
    assert service.metrics()["cleanupFailures"] == 0
    assert not service._lock_path.exists()
    assert not any(thread.is_alive() for thread in service._threads)
    assert not any(
        "chromium" in command.casefold()
        for command in _descendant_commands(os.getpid())
    )
    assert not [
        path
        for path in (root / "auth").iterdir()
        if path.name.startswith(".") and not path.name.endswith(".auth-status")
    ]

    return {
        "workers": workers,
        "jobs": 4,
        "wallSeconds": round(wall_seconds, 3),
        "peakProcessTreeRssBytes": resources.peak_rss_bytes,
        "cpuSeconds": round(cpu_seconds, 3),
        "sqliteCallbackSeconds": round(tracker.persistence_seconds, 3),
        "maxPersistenceBacklog": metrics["maxPersistenceBacklog"],
        "cleanupSeconds": round(tracker.cleanup_seconds, 3),
        "cleanupFailures": tracker.cleanup_failures,
        "correctResults": len(post_ids),
    }


@pytest.mark.skipif(
    not RUN_BROWSER_MATRIX,
    reason="set XWORKBENCH_RUN_BROWSER_MATRIX=1 in the installed-Chromium job",
)
def test_production_playwright_matrix_uses_loopback_only_and_cleans_up(tmp_path):
    with _loopback_fixture() as (loopback_url, fixture_server):
        serial = _run_browser_matrix_case(tmp_path, loopback_url, 1)
        concurrent = _run_browser_matrix_case(tmp_path, loopback_url, 2)
        assert fixture_server.request_count == 8

    summary = {
        "fixture": "numeric-loopback-dynamic-html",
        "externalEgress": False,
        "matrix": [serial, concurrent],
        "speedup": round(serial["wallSeconds"] / concurrent["wallSeconds"], 3),
    }
    print("REAL_BROWSER_MATRIX=" + json.dumps(summary, sort_keys=True))
    assert concurrent["wallSeconds"] <= serial["wallSeconds"] * 0.85
