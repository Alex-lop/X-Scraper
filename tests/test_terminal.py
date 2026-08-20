from __future__ import annotations

import asyncio
import copy
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import Button, DataTable, Input, Select, SelectionList, Static, TabbedContent

from xworkbench.config import Settings
from xworkbench.local_client import OutcomeUnknownError
from xworkbench.terminal import QueuePanel, TerminalWorkbench, safe_text

SECRET = "secret-canary-must-not-render"


class _LocalFixtureClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.disconnected = False
        self.unknown_path: str | None = None
        self.delay = 0.0
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()
        self.progress = {
            "eventEpoch": "a" * 32,
            "events": [
                {
                    "sequence": 7,
                    "type": "started",
                    "jobId": "job-1",
                    "status": "running",
                    "count": 1,
                }
            ],
            "jobs": [
                {
                    "id": "job-1",
                    "provider": "[bold]playwright_browser[/bold]\n",
                    "status": "succeeded",
                    "collectedCount": 1,
                    "targetCount": 2,
                    "updatedAt": "2026-08-20T12:00:00+00:00",
                    "responseBody": SECRET,
                }
            ],
            "lastSequence": 7,
            "gap": True,
        }
        self.metrics = {
            "queueDepth": 1,
            "queueCapacity": 25,
            "activeWorkers": 1,
            "maxWorkers": 2,
            "activeSources": 1,
            "activeAuthStates": 1,
            "resourcePaused": False,
            "resourcePauseReasons": [],
            "queueWaitP50Ms": 1.25,
            "queueWaitP95Ms": 2.5,
            "throughputJobsPerSecond": 3.0,
            "persistenceActive": 0,
            "persistenceWaiting": 0,
            "maxPersistenceBacklog": 1,
            "eventDropped": 2,
            "eventCoalesced": 1,
            "cleanupFailures": 0,
            "rssBytes": 64 * 1024 * 1024,
            "cpuPercent": 4.5,
            "resourceSignalStatus": {"chromiumProcessCount": "supported"},
        }
        self.sources = [
            {
                "sourceId": "source-a",
                "displayName": "[bold]Fixture A[/bold]",
                "provider": "playwright_browser",
            },
            {
                "sourceId": "source-b",
                "displayName": "Fixture B",
                "provider": "official_x_api",
            },
        ]

    def _enter(self) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        if self.delay:
            time.sleep(self.delay)

    def _leave(self) -> None:
        with self._lock:
            self.active -= 1

    def get(self, path: str, query: dict | None = None) -> dict:
        self._enter()
        try:
            self.calls.append(("GET", path, copy.deepcopy(query)))
            if self.disconnected:
                raise RuntimeError("offline with raw details that must not render")
            if path == "/api/connection":
                return {
                    "providers": {
                        "playwright_browser": {
                            "connection": {
                                "status": "verified_live",
                                "ready": True,
                                "message": SECRET,
                            }
                        },
                        "official_x_api": {
                            "connection": {
                                "status": "not_configured",
                                "ready": False,
                                "message": SECRET,
                            }
                        },
                    }
                }
            if path == "/api/progress":
                progress = copy.deepcopy(self.progress)
                after = query.get("after", 0) if isinstance(query, dict) else 0
                progress["events"] = [
                    event
                    for event in progress.get("events", [])
                    if isinstance(event.get("sequence"), int) and event["sequence"] > after
                ]
                return progress
            if path == "/api/queue/metrics":
                return copy.deepcopy(self.metrics)
            if path == "/api/sources":
                return {"sources": copy.deepcopy(self.sources)}
            raise AssertionError(path)
        finally:
            self._leave()

    def post(self, path: str, body: dict) -> dict:
        self._enter()
        try:
            self.calls.append(("POST", path, copy.deepcopy(body)))
            if path == self.unknown_path:
                raise OutcomeUnknownError("unknown")
            if path == "/api/collections/preview":
                expires = datetime.now(UTC) + timedelta(minutes=5)
                response = {
                    "provider": body["provider"],
                    "request": copy.deepcopy(body),
                    "executionPlan": {
                        "sourceUrl": "https://x.com/home",
                        "preparedAt": datetime.now(UTC).isoformat(),
                        "expiresAt": expires.isoformat(),
                    },
                    "confirmation": {"kind": "explicit"},
                }
                if body["provider"] == "official_x_api":
                    response["compiledIntent"] = {
                        "searchMode": body["searchMode"],
                        "endpoint": "/2/tweets/search/recent",
                        "query": body["sourceValue"],
                        "compiledLength": len(body["sourceValue"]),
                        "startTime": "2026-08-13T12:00:00Z",
                        "endTime": "2026-08-20T11:59:50Z",
                        "sortOrder": "recency",
                    }
                    response["costEstimate"] = {
                        "basis": "list_price_pre_dedup",
                        "maximumPostResources": body["maxPosts"],
                        "maximumPostListPriceUsd": 0.1,
                        "variableResources": ["users", "media"],
                        "pricingAsOf": "2026-08-20",
                        "note": "Not an invoice total.",
                    }
                return response
            if path == "/api/batches/preview":
                expires = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
                return {
                    "approvalDigest": "digest-1",
                    "manifest": {
                        "batchId": "batch-1",
                        "expiresAt": expires,
                        "freshnessChoice": "capture_fresh",
                        "routeAlias": "direct",
                        "maxConcurrency": 2,
                        "perSourceConcurrency": 1,
                        "perAuthStateConcurrency": 1,
                        "queueCapacity": 25,
                        "expectedQueueOrder": ["source-a", "source-b"],
                        "queueOrderBasis": "Priority then preview order.",
                        "items": [
                            {
                                **item,
                                "provider": self.sources[index]["provider"],
                                "visibleDestination": f"Fixture {index + 1}",
                                "expectedQueueOrder": index + 1,
                                "deadlineAt": expires,
                                "routeAlias": "direct",
                                "freshnessChoice": "capture_fresh",
                                "executionPlan": {
                                    "maximumPostListPriceUsd": 0.25 if index == 1 else None
                                },
                            }
                            for index, item in enumerate(body["items"])
                        ],
                    },
                }
            if path == "/api/batches/confirm":
                return {"batchId": "batch-1", "jobs": []}
            if path == "/api/jobs":
                return {"id": "job-new"}
            if path == "/api/sources":
                return {"sourceId": "source-new"}
            if path.endswith("/cancel"):
                return {"status": "cancellation_requested"}
            raise AssertionError(path)
        finally:
            self._leave()


def _widget_text(app: TerminalWorkbench) -> str:
    static = "\n".join(str(widget.content) for widget in app.query(Static))
    table = app.query_one("#jobs-table", DataTable)
    rows = "\n".join(
        " ".join(map(str, table.get_row_at(index))) for index in range(table.row_count)
    )
    return f"{static}\n{rows}"


def test_safe_text_bounds_and_escapes_markup_and_controls():
    rendered = safe_text("[bold]hello[/bold]\n\x1b[31m" + "[" * 200, 40)
    assert len(rendered) <= 40
    assert rendered.startswith("\\[bold]")
    assert "\n" not in rendered and "\x1b" not in rendered


@pytest.mark.parametrize("size", [(100, 36), (140, 45), (48, 24)])
def test_monitor_is_responsive_read_only_stale_safe_and_serial(size):
    async def scenario() -> None:
        client = _LocalFixtureClient()
        opened: list[str] = []
        app = TerminalWorkbench(
            "http://localhost:5000",
            owner=False,
            client=client,
            open_browser=opened.append,
        )
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert app.screen.size == size
            assert len(app.query(QueuePanel)) == 1
            assert len(app.query(TabbedContent)) == 0
            assert len(app.query(Button)) == 0

            metrics = str(app.query_one("#queue-metrics", Static).content)
            assert "Coordinator RSS: 64.0 MiB" in metrics
            assert "Chromium process-tree RSS: unsupported" in metrics
            assert "gap: True" in metrics
            row = app.query_one("#jobs-table", DataTable).get_row_at(0)
            assert str(row[2]) == "succeeded"
            assert SECRET not in _widget_text(app)

            client.disconnected = True
            app._next_retry = 0
            await app._poll_queue(force=True)
            assert "showing stale durable state" in str(
                app.query_one("#queue-connection", Static).content
            )
            assert app.query_one("#jobs-table", DataTable).row_count == 1

            client.disconnected = False
            client.delay = 0.01
            client.peak = 0
            app._next_retry = 0
            await asyncio.gather(app._poll_connection(), app._poll_queue(force=True))
            assert client.peak == 1

            app.action_open_web()
            assert opened == ["http://127.0.0.1:5000"]

    asyncio.run(scenario())


@pytest.mark.parametrize("restart_sequence", [1, 7, 9])
def test_monitor_replays_progress_after_server_event_stream_reset(restart_sequence):
    async def scenario() -> None:
        client = _LocalFixtureClient()
        app = TerminalWorkbench(
            "http://127.0.0.1:5000",
            owner=False,
            client=client,
        )
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            assert app._last_sequence == 7
            assert app._event_epoch == "a" * 32
            initial_progress_queries = [
                call[2]
                for call in client.calls
                if call[:2] == ("GET", "/api/progress")
            ]
            assert initial_progress_queries == [{"after": 0, "limit": 100}]

            restarted = copy.deepcopy(client.progress)
            restarted["eventEpoch"] = "b" * 32
            restarted["events"] = [
                {
                    "sequence": restart_sequence,
                    "type": "started",
                    "jobId": "job-1",
                    "status": "running",
                    "count": 1,
                }
            ]
            restarted["lastSequence"] = restart_sequence
            restarted["gap"] = False
            client.progress = restarted
            call_start = len(client.calls)

            await app._poll_queue(force=True)

            progress_queries = [
                call[2]
                for call in client.calls[call_start:]
                if call[:2] == ("GET", "/api/progress")
            ]
            assert progress_queries == [
                {"after": 7, "limit": 100},
                {"after": 0, "limit": 100},
            ]
            assert app._event_epoch == "b" * 32
            assert app._last_sequence == restart_sequence
            assert f"#{restart_sequence} started" in str(
                app.query_one("#queue-events", Static).content
            )
            assert "gap: True" in str(app.query_one("#queue-metrics", Static).content)
            assert str(app.query_one("#jobs-table", DataTable).get_row_at(0)[2]) == "succeeded"
            assert client.peak == 1

    asyncio.run(scenario())


def test_owner_exact_approvals_unknown_outcome_and_cancellation(tmp_path):
    async def scenario() -> None:
        client = _LocalFixtureClient()
        settings = Settings(tmp_path / "workbench.db", tmp_path / "auth" / "token")
        app = TerminalWorkbench(
            "http://127.0.0.1:5000",
            owner=True,
            settings=settings,
            client=client,
        )
        async with app.run_test(size=(110, 42)) as pilot:
            await pilot.pause()
            assert len(app.query(TabbedContent)) == 1
            assert len(app.query("#cancel-job")) == 1
            assert "max_workers" in str(app.query_one("#public-settings", Static).content)
            assert SECRET not in _widget_text(app)

            app.query_one("#bearer-token", Input).value = "stored-token-never-rendered"
            await app.save_token_pressed()
            assert settings.bearer_token_path.read_text(encoding="utf-8").strip() == (
                "stored-token-never-rendered"
            )
            assert "Protected token saved" in str(app.query_one("#setup-status", Static).content)
            assert "stored-token-never-rendered" not in _widget_text(app)

            await app.preview_single_pressed()
            preview = app._single_preview
            assert preview is not None
            assert "EXACT SERVER PREVIEW" in str(app.query_one("#preview", Static).content)
            assert not app.query_one("#confirm-single", Button).disabled

            app.query_one("#max-posts", Input).value = "6"
            await pilot.pause()
            assert app._single_preview is None
            assert app.query_one("#confirm-single", Button).disabled

            app.query_one("#max-posts", Input).value = "5"
            await pilot.pause()
            await app.preview_single_pressed()
            approved = copy.deepcopy(app._single_preview)
            await app.confirm_single_pressed()
            job_posts = [call for call in client.calls if call[:2] == ("POST", "/api/jobs")]
            assert job_posts[-1][2] == {
                **approved["request"],
                "executionPlan": approved["executionPlan"],
                "confirmBrowserCapture": True,
            }

            app.query_one("#provider", Select).value = "official_x_api"
            await pilot.pause()
            app.query_one("#surface", Select).value = "search"
            app.query_one("#source-value", Input).value = "fixture query"
            app.query_one("#max-posts", Input).value = "10"
            await pilot.pause()
            await app.preview_single_pressed()
            assert "paid official" in str(app.query_one("#confirm-single", Button).label)
            assert "Maximum Post resources: 10" in str(app.query_one("#preview", Static).content)
            assert "Compiled endpoint/query" in str(app.query_one("#preview", Static).content)

            client.unknown_path = "/api/jobs"
            before = len([call for call in client.calls if call[:2] == ("POST", "/api/jobs")])
            await app.confirm_single_pressed()
            after = len([call for call in client.calls if call[:2] == ("POST", "/api/jobs")])
            assert after == before + 1
            assert "outcome unknown" in str(app.query_one("#preview", Static).content)
            assert app._single_preview is None
            assert app.query_one("#confirm-single", Button).disabled
            await app.confirm_single_pressed()
            assert (
                len([call for call in client.calls if call[:2] == ("POST", "/api/jobs")]) == after
            )

            client.unknown_path = "/api/sources"
            source_refreshes = len(
                [call for call in client.calls if call[:2] == ("GET", "/api/sources")]
            )
            await app.save_source_pressed()
            assert (
                len([call for call in client.calls if call[:2] == ("GET", "/api/sources")])
                == source_refreshes + 1
            )
            client.unknown_path = None

            selection = app.query_one("#saved-sources", SelectionList)
            selection.select("source-a")
            selection.select("source-b")
            await pilot.pause()
            await app.preview_batch_pressed()
            batch_preview = copy.deepcopy(app._batch_preview)
            assert "Concurrency global/source/auth: 2/1/1" in str(
                app.query_one("#preview", Static).content
            )
            assert "paid official reads: yes" in str(app.query_one("#preview", Static).content)
            assert "paid official" in str(app.query_one("#confirm-batch", Button).label)
            await app.confirm_batch_pressed()
            confirm = [
                call for call in client.calls if call[:2] == ("POST", "/api/batches/confirm")
            ][-1]
            assert confirm[2] == {
                "confirm": True,
                "manifest": batch_preview["manifest"],
                "approvalDigest": batch_preview["approvalDigest"],
            }

            await app.cancel_job_pressed()
            await app.cancel_batch_pressed()
            assert ("POST", "/api/jobs/job-1/cancel", {}) in client.calls
            assert ("POST", "/api/batches/batch-1/cancel", {"confirm": True}) in client.calls

            app._auth_running = True
            app.action_quit_safely()
            assert app._auth_cancel.is_set()
            assert "Closing headed Browser authentication" in str(
                app.query_one("#setup-status", Static).content
            )

    asyncio.run(scenario())


def test_expired_preview_is_discarded_without_submission():
    async def scenario() -> None:
        client = _LocalFixtureClient()
        app = TerminalWorkbench("http://127.0.0.1:5000", owner=True, client=client)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await app.preview_single_pressed()
            assert app._single_preview is not None
            app._single_preview["executionPlan"]["expiresAt"] = "2000-01-01T00:00:00+00:00"
            await app.confirm_single_pressed()
            assert not any(call[:2] == ("POST", "/api/jobs") for call in client.calls)
            assert "Preview expired" in str(app.query_one("#preview", Static).content)

    asyncio.run(scenario())
