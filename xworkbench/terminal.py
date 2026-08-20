from __future__ import annotations

import asyncio
import json
import threading
import time
import unicodedata
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.markup import escape
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)

from .config import Settings, save_bearer_token
from .local_client import LocalJsonClient, OutcomeUnknownError
from .playwright_browser import authenticate_interactively

DISPLAY_LIMIT = 8_000
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "interrupted", "partial"}


def safe_text(value: Any, limit: int = 500) -> str:
    text = unicodedata.normalize("NFC", str(value if value is not None else "unsupported"))
    text = "".join(
        " "
        if character in "\r\n\t" or unicodedata.category(character).startswith("C")
        else character
        for character in text
    )
    return escape(" ".join(text.split())[:limit])[:limit]


def _number(value: Any, suffix: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "unsupported"
    return f"{value:.2f}{suffix}" if isinstance(value, float) else f"{value}{suffix}"


def _rss(value: Any) -> str:
    return f"{value / 1024 / 1024:.1f} MiB" if isinstance(value, int) else "unsupported"


class QueuePanel(VerticalScroll):
    def __init__(self, *, owner: bool) -> None:
        super().__init__(id="queue-panel")
        self.owner = owner

    def compose(self) -> ComposeResult:
        yield Static("Connecting to loopback server…", id="queue-connection", markup=False)
        yield Static("Queue metrics unavailable.", id="queue-metrics", markup=False)
        yield Static("No progress events.", id="queue-events", markup=False)
        yield DataTable(id="jobs-table", cursor_type="row", zebra_stripes=True)
        if self.owner:
            with Horizontal(classes="button-row"):
                yield Button("Cancel selected job", id="cancel-job", variant="warning")
                yield Button("Cancel current batch", id="cancel-batch", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#jobs-table", DataTable).add_columns(
            "Job", "Provider", "Status", "Progress", "Updated"
        )


class TerminalWorkbench(App[None]):
    TITLE = "X-Scraper terminal operations"
    CSS = """
    Screen { layout: vertical; }
    Header { dock: top; }
    Footer { dock: bottom; }
    TabbedContent, #queue-panel { height: 1fr; }
    VerticalScroll { padding: 1 2; }
    .section-title { text-style: bold; margin-top: 1; }
    .field-row { height: auto; layout: horizontal; }
    .field-row > * { width: 1fr; margin-right: 1; }
    .button-row { height: auto; margin: 1 0; }
    .button-row Button { margin-right: 1; }
    Input, Select, SelectionList { margin-bottom: 1; }
    SelectionList { height: 8; border: solid $panel; }
    #preview { min-height: 8; max-height: 18; overflow-y: auto; border: solid $panel; padding: 1; }
    #jobs-table { min-height: 10; height: 1fr; }
    #queue-metrics, #queue-events, #public-settings, #provider-readiness {
        border: solid $panel; padding: 1; margin-bottom: 1;
    }
    """
    BINDINGS = [
        Binding("q", "quit_safely", "Quit"),
        Binding("w", "open_web", "Web dashboard"),
        Binding("1", "show_tab('setup-pane')", "Setup", show=False),
        Binding("2", "show_tab('capture-pane')", "Capture", show=False),
        Binding("3", "show_tab('queue-pane')", "Queue", show=False),
    ]

    def __init__(
        self,
        base_url: str,
        *,
        owner: bool,
        settings: Settings | None = None,
        client: LocalJsonClient | Any | None = None,
        open_browser: Callable[[str], Any] = webbrowser.open,
        authenticate: Callable[..., Any] = authenticate_interactively,
    ) -> None:
        super().__init__()
        self.base_url = LocalJsonClient(base_url).base_url
        self.client = client or LocalJsonClient(self.base_url)
        self.owner = owner
        self.settings = settings
        self.open_browser = open_browser
        self.authenticate = authenticate
        self._request_lock = asyncio.Lock()
        self._event_epoch: str | None = None
        self._last_sequence = 0
        self._next_retry = 0.0
        self._jobs: list[dict[str, Any]] = []
        self._job_rows: list[str] = []
        self._sources: dict[str, dict[str, Any]] = {}
        self._single_preview: dict[str, Any] | None = None
        self._single_signature: str | None = None
        self._batch_preview: dict[str, Any] | None = None
        self._batch_signature: str | None = None
        self._current_batch_id: str | None = None
        self._auth_running = False
        self._auth_cancel = threading.Event()
        self._auth_task: asyncio.Task | None = None
        self._quit_after_auth = False

    def compose(self) -> ComposeResult:
        yield Header()
        if not self.owner:
            yield QueuePanel(owner=False)
            yield Footer()
            return
        with TabbedContent(initial="setup-pane", id="operations-tabs"):
            with TabPane("Setup", id="setup-pane"):
                with VerticalScroll():
                    yield Label("Local owner runtime", classes="section-title")
                    yield Static(
                        f"Dashboard: {safe_text(self.base_url)}\n"
                        f"Monitor: xworkbench monitor --url {safe_text(self.base_url)}",
                        id="server-details",
                        markup=False,
                    )
                    yield Static("Runtime initialized.", id="setup-status", markup=False)
                    yield Static("Loading public settings…", id="public-settings", markup=False)
                    yield Static(
                        "Loading provider readiness…", id="provider-readiness", markup=False
                    )
                    yield Input(
                        placeholder="Optional official X API Bearer Token",
                        password=True,
                        max_length=4096,
                        id="bearer-token",
                    )
                    with Horizontal(classes="button-row"):
                        yield Button("Save token", id="save-token", variant="primary")
                        yield Button("Open headed Browser auth", id="browser-auth")
                        yield Button("Refresh readiness", id="initialize-runtime")
            with TabPane("Capture", id="capture-pane"):
                with VerticalScroll():
                    yield Label("One exact approved capture", classes="section-title")
                    with Horizontal(classes="field-row"):
                        yield Select(
                            [("Browser", "playwright_browser"), ("Official API", "official_x_api")],
                            value="playwright_browser",
                            allow_blank=False,
                            id="provider",
                        )
                        yield Select(
                            [("Home", "home"), ("Profile", "profile"), ("Latest search", "search")],
                            value="home",
                            allow_blank=False,
                            id="surface",
                        )
                        yield Input(value="5", type="integer", id="max-posts")
                    yield Input(
                        value="home",
                        placeholder="Profile handle or search query",
                        id="source-value",
                    )
                    with Horizontal(classes="field-row"):
                        yield Select(
                            [("Recent", "recent"), ("Full archive", "fullArchive")],
                            value="recent",
                            allow_blank=False,
                            id="search-mode",
                            disabled=True,
                        )
                        yield Input(placeholder="Start YYYY-MM-DD", id="start-date", disabled=True)
                        yield Input(placeholder="End YYYY-MM-DD", id="end-date", disabled=True)
                    with Horizontal(classes="field-row"):
                        yield Checkbox("Include replies", id="include-replies", disabled=True)
                        yield Checkbox("Media only", id="media-only", disabled=True)
                        yield Input(placeholder="Saved source display name", id="source-name")
                    with Horizontal(classes="button-row"):
                        yield Button("Save source", id="save-source")
                        yield Button(
                            "Preview exact request", id="preview-single", variant="primary"
                        )
                        yield Button("Confirm capture", id="confirm-single", disabled=True)

                    yield Label("Saved-source batch", classes="section-title")
                    yield SelectionList[str](id="saved-sources")
                    with Horizontal(classes="field-row"):
                        yield Input(value="5", type="integer", id="batch-browser-posts")
                        yield Input(value="25", type="integer", id="batch-official-posts")
                        yield Input(value="0", type="integer", id="batch-priority")
                        yield Input(value="600", type="integer", id="batch-deadline")
                    yield Static("Freshness: capture_fresh", markup=False)
                    with Horizontal(classes="button-row"):
                        yield Button(
                            "Preview selected batch", id="preview-batch", variant="primary"
                        )
                        yield Button("Confirm selected batch", id="confirm-batch", disabled=True)
                    yield Static("No approved preview.", id="preview", markup=False)
            with TabPane("Queue", id="queue-pane"):
                yield QueuePanel(owner=True)
        yield Footer()

    async def on_mount(self) -> None:
        self.set_interval(1.5, self._poll_queue)
        self.set_interval(10, self._poll_connection)
        if self.owner and self.settings is not None:
            public = self.settings.public_dict()
            lines = [
                f"{safe_text(key)}: {safe_text(value)}" for key, value in sorted(public.items())
            ]
            self.query_one("#public-settings", Static).update("\n".join(lines))
        await self._poll_connection()
        await self._poll_queue(force=True)
        if self.owner:
            await self._refresh_sources()

    async def _call(self, function, *args):
        async with self._request_lock:
            return await asyncio.to_thread(function, *args)

    def _set_connection(self, message: str) -> None:
        self.query_one("#queue-connection", Static).update(message)

    async def _poll_connection(self) -> None:
        if time.monotonic() < self._next_retry:
            return
        try:
            payload = await self._call(self.client.get, "/api/connection")
        except Exception:
            self._next_retry = time.monotonic() + 3
            self._set_connection("Disconnected — showing stale durable state; retrying in 3s.")
            return
        self._next_retry = 0
        self._set_connection(f"Connected: {safe_text(self.base_url)}")
        if self.owner:
            providers = payload.get("providers") if isinstance(payload, dict) else {}
            lines = []
            for provider in ("playwright_browser", "official_x_api"):
                item = providers.get(provider, {}) if isinstance(providers, dict) else {}
                connection = item.get("connection", {}) if isinstance(item, dict) else {}
                lines.append(
                    f"{provider}: status={safe_text(connection.get('status'))}; "
                    f"ready={safe_text(connection.get('ready', connection.get('valid')))}"
                )
            self.query_one("#provider-readiness", Static).update("\n".join(lines))

    async def _poll_queue(self, *, force: bool = False) -> None:
        if not force and time.monotonic() < self._next_retry:
            return
        requested_sequence = self._last_sequence
        event_stream_reset = False
        try:
            progress = await self._call(
                self.client.get,
                "/api/progress",
                {"after": requested_sequence, "limit": 100},
            )
            event_epoch = progress.get("eventEpoch")
            last_sequence = progress.get("lastSequence")
            event_stream_reset = (
                self._event_epoch is not None
                and isinstance(event_epoch, str)
                and bool(event_epoch)
                and event_epoch != self._event_epoch
            ) or (
                isinstance(last_sequence, int)
                and not isinstance(last_sequence, bool)
                and last_sequence < requested_sequence
            )
            if event_stream_reset:
                progress = await self._call(
                    self.client.get,
                    "/api/progress",
                    {"after": 0, "limit": 100},
                )
            metrics = await self._call(self.client.get, "/api/queue/metrics")
        except Exception:
            self._next_retry = time.monotonic() + 3
            self._set_connection("Disconnected — showing stale durable state; retrying in 3s.")
            return
        self._next_retry = 0
        self._set_connection(f"Connected: {safe_text(self.base_url)}")
        event_epoch = progress.get("eventEpoch")
        if isinstance(event_epoch, str) and event_epoch:
            self._event_epoch = event_epoch
        last_sequence = progress.get("lastSequence")
        if isinstance(last_sequence, int) and not isinstance(last_sequence, bool):
            self._last_sequence = (
                last_sequence
                if event_stream_reset
                else max(self._last_sequence, last_sequence)
            )
        elif event_stream_reset:
            self._last_sequence = 0
        jobs = progress.get("jobs")
        if isinstance(jobs, list):
            self._jobs = [job for job in jobs[:100] if isinstance(job, dict)]
        self._render_jobs()
        self._render_metrics(metrics, gap=event_stream_reset or progress.get("gap") is True)
        self._render_events(progress)

    def _render_jobs(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        table.clear()
        self._job_rows = []
        for job in self._jobs:
            job_id = job.get("id")
            if not isinstance(job_id, str):
                continue
            self._job_rows.append(job_id)
            table.add_row(
                safe_text(job_id, 36),
                safe_text(job.get("provider"), 32),
                safe_text(job.get("status"), 20),
                f"{_number(job.get('collectedCount'))}/{_number(job.get('targetCount'))}",
                safe_text(job.get("updatedAt"), 32),
            )

    def _render_metrics(self, metrics: Any, *, gap: bool) -> None:
        metrics = metrics if isinstance(metrics, dict) else {}
        reasons = metrics.get("resourcePauseReasons")
        reason_text = (
            ", ".join(safe_text(reason, 40) for reason in reasons)
            if isinstance(reasons, list)
            else "none"
        )
        signals = metrics.get("resourceSignalStatus")
        chromium_status = (
            signals.get("chromiumProcessCount", "unsupported")
            if isinstance(signals, dict)
            else "unsupported"
        )
        lines = [
            f"Queue {_number(metrics.get('queueDepth'))}/{_number(metrics.get('queueCapacity'))}; "
            f"workers {_number(metrics.get('activeWorkers'))}/{_number(metrics.get('maxWorkers'))}",
            f"Active sources/auth keys: {_number(metrics.get('activeSources'))}/"
            f"{_number(metrics.get('activeAuthStates'))}",
            f"Pause: {safe_text(metrics.get('resourcePaused'))}; reasons: {reason_text}",
            f"Wait p50/p95: {_number(metrics.get('queueWaitP50Ms'), 'ms')}/"
            f"{_number(metrics.get('queueWaitP95Ms'), 'ms')}; throughput: "
            f"{_number(metrics.get('throughputJobsPerSecond'), '/s')}",
            f"Persistence active/waiting/backlog: {_number(metrics.get('persistenceActive'))}/"
            f"{_number(metrics.get('persistenceWaiting'))}/"
            f"{_number(metrics.get('maxPersistenceBacklog'))}",
            f"Events dropped/coalesced: {_number(metrics.get('eventDropped'))}/"
            f"{_number(metrics.get('eventCoalesced'))}; gap: {gap}",
            f"Cleanup failures: {_number(metrics.get('cleanupFailures'))}",
            f"Coordinator RSS: {_rss(metrics.get('rssBytes'))}; CPU: "
            f"{_number(metrics.get('cpuPercent'), '%')}",
            f"Chromium process count signal: {safe_text(chromium_status)}; "
            "Chromium process-tree RSS: unsupported",
        ]
        self.query_one("#queue-metrics", Static).update("\n".join(lines))

    def _render_events(self, progress: Any) -> None:
        events = progress.get("events") if isinstance(progress, dict) else None
        lines = []
        for event in events[-10:] if isinstance(events, list) else []:
            if not isinstance(event, dict):
                continue
            lines.append(
                f"#{_number(event.get('sequence'))} {safe_text(event.get('type'), 20)} "
                f"job={safe_text(event.get('jobId'), 36)} "
                f"status={safe_text(event.get('status'), 20)} count={_number(event.get('count'))}"
            )
        self.query_one("#queue-events", Static).update(
            "\n".join(lines) if lines else "No new bounded progress events."
        )

    async def _refresh_sources(self) -> None:
        try:
            payload = await self._call(self.client.get, "/api/sources", {"limit": 25})
        except Exception:
            return
        rows = payload.get("sources") if isinstance(payload, dict) else []
        self._sources = {
            row["sourceId"]: row
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("sourceId"), str)
        }
        selection = self.query_one("#saved-sources", SelectionList)
        selection.clear_options()
        selection.add_options(
            [
                (
                    f"{safe_text(row.get('displayName'), 80)} — "
                    f"{safe_text(row.get('provider'), 24)}",
                    source_id,
                    False,
                )
                for source_id, row in self._sources.items()
            ]
        )

    def _capture_values(self) -> dict[str, Any]:
        return {
            "provider": self.query_one("#provider", Select).value,
            "surface": self.query_one("#surface", Select).value,
            "source": self.query_one("#source-value", Input).value,
            "maximum": self.query_one("#max-posts", Input).value,
            "mode": self.query_one("#search-mode", Select).value,
            "start": self.query_one("#start-date", Input).value,
            "end": self.query_one("#end-date", Input).value,
            "replies": self.query_one("#include-replies", Checkbox).value,
            "media": self.query_one("#media-only", Checkbox).value,
        }

    def _capture_signature_value(self) -> str:
        return json.dumps(self._capture_values(), sort_keys=True, default=str)

    def _batch_signature_value(self) -> str:
        return json.dumps(
            {
                "selected": sorted(self.query_one("#saved-sources", SelectionList).selected),
                "browser": self.query_one("#batch-browser-posts", Input).value,
                "official": self.query_one("#batch-official-posts", Input).value,
                "priority": self.query_one("#batch-priority", Input).value,
                "deadline": self.query_one("#batch-deadline", Input).value,
            },
            sort_keys=True,
        )

    def _discard_previews(self) -> None:
        self._single_preview = None
        self._single_signature = None
        self._batch_preview = None
        self._batch_signature = None
        self.query_one("#confirm-single", Button).disabled = True
        self.query_one("#confirm-batch", Button).disabled = True
        self.query_one("#preview", Static).update("Approval discarded; preview again.")

    def _request_body(self) -> dict[str, Any]:
        values = self._capture_values()
        provider = str(values["provider"])
        body: dict[str, Any] = {
            "provider": provider,
            "sourceType": str(values["surface"]),
            "sourceValue": str(values["source"]),
            "maxPosts": int(str(values["maximum"])),
        }
        if provider == "official_x_api":
            body.update(
                searchMode=str(values["mode"]),
                includeReplies=bool(values["replies"]),
                mediaOnly=bool(values["media"]),
            )
            if values["start"]:
                body["startDate"] = str(values["start"])
            if values["end"]:
                body["endDate"] = str(values["end"])
        return body

    @staticmethod
    def _not_expired(value: Any) -> bool:
        if value is None:
            return True
        try:
            expires = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return expires.utcoffset() is not None and expires.astimezone(UTC) > datetime.now(UTC)
        except ValueError:
            return False

    def _show_single_preview(self, preview: dict[str, Any]) -> None:
        request = preview.get("request") if isinstance(preview.get("request"), dict) else {}
        plan = (
            preview.get("executionPlan") if isinstance(preview.get("executionPlan"), dict) else {}
        )
        cost = preview.get("costEstimate") if isinstance(preview.get("costEstimate"), dict) else {}
        confirmation = (
            preview.get("confirmation") if isinstance(preview.get("confirmation"), dict) else {}
        )
        intent = (
            preview.get("compiledIntent") if isinstance(preview.get("compiledIntent"), dict) else {}
        )
        lines = [
            "EXACT SERVER PREVIEW",
            f"Provider: {safe_text(preview.get('provider'))}",
            f"Source: {safe_text(request.get('sourceType'))} — "
            f"{safe_text(request.get('sourceValue'))}",
            f"Post limit: {_number(request.get('maxPosts'))}",
            "Destination: "
            f"{safe_text(plan.get('sourceUrl') or plan.get('endpoint') or plan.get('query'))}",
            f"Prepared/expires: {safe_text(plan.get('preparedAt') or plan.get('compiledAt'))} / "
            f"{safe_text(plan.get('expiresAt'))}",
            f"Confirmation: {safe_text(confirmation.get('kind'))}",
        ]
        if intent:
            lines.extend(
                [
                    f"Compiled endpoint/query: {safe_text(intent.get('endpoint'))} — "
                    f"{safe_text(intent.get('query'), 1_024)}",
                    f"Mode/window: {safe_text(intent.get('searchMode'))}; "
                    f"{safe_text(intent.get('startTime'))} to "
                    f"{safe_text(intent.get('endTime'))}",
                    f"Sort/query length: {safe_text(intent.get('sortOrder'))}/"
                    f"{_number(intent.get('compiledLength'))}",
                ]
            )
        if cost:
            lines.extend(
                [
                    f"Cost basis: {safe_text(cost.get('basis'))}",
                    f"Maximum Post resources: {_number(cost.get('maximumPostResources'))}",
                    f"Maximum Post list price USD: {_number(cost.get('maximumPostListPriceUsd'))}",
                    f"Variable resources: {safe_text(cost.get('variableResources'))}",
                    f"Pricing as of: {safe_text(cost.get('pricingAsOf'))}",
                    f"Cost note: {safe_text(cost.get('note'), 1_000)}",
                ]
            )
        self.query_one("#preview", Static).update("\n".join(lines)[:DISPLAY_LIMIT])

    def _show_batch_preview(self, preview: dict[str, Any]) -> None:
        manifest = preview.get("manifest") if isinstance(preview.get("manifest"), dict) else {}
        lines = [
            "EXACT SERVER BATCH MANIFEST",
            f"Batch: {safe_text(manifest.get('batchId'))}",
            f"Expires: {safe_text(manifest.get('expiresAt'))}",
            f"Freshness/route: {safe_text(manifest.get('freshnessChoice'))}/"
            f"{safe_text(manifest.get('routeAlias'))}",
            f"Concurrency global/source/auth: {_number(manifest.get('maxConcurrency'))}/"
            f"{_number(manifest.get('perSourceConcurrency'))}/"
            f"{_number(manifest.get('perAuthStateConcurrency'))}",
            f"Queue capacity: {_number(manifest.get('queueCapacity'))}",
            f"Ordering: {safe_text(manifest.get('expectedQueueOrder'), 500)}",
            f"Ordering basis: {safe_text(manifest.get('queueOrderBasis'), 1_000)}",
            f"Approval digest: {safe_text(preview.get('approvalDigest'), 128)}",
        ]
        paid = False
        for item in manifest.get("items", []) if isinstance(manifest.get("items"), list) else []:
            if isinstance(item, dict):
                paid = paid or item.get("provider") == "official_x_api"
                plan = (
                    item.get("executionPlan") if isinstance(item.get("executionPlan"), dict) else {}
                )
                lines.append(
                    f"#{_number(item.get('expectedQueueOrder'))} "
                    f"{safe_text(item.get('provider'))} destination="
                    f"{safe_text(item.get('visibleDestination'))} posts="
                    f"{_number(item.get('maxPosts'))} priority={_number(item.get('priority'))} "
                    f"deadline={safe_text(item.get('deadlineAt'))} "
                    f"route/freshness={safe_text(item.get('routeAlias'))}/"
                    f"{safe_text(item.get('freshnessChoice'))} max-post-cost="
                    f"{_number(plan.get('maximumPostListPriceUsd'))}"
                )
        lines.append(f"Includes paid official reads: {'yes' if paid else 'no'}")
        self.query_one("#preview", Static).update("\n".join(lines)[:DISPLAY_LIMIT])

    async def _mutation(self, path: str, body: dict[str, Any], success: str):
        try:
            result = await self._call(self.client.post, path, body)
        except OutcomeUnknownError:
            self._discard_previews()
            self.query_one("#preview", Static).update(
                "Mutation outcome unknown. Durable queue state refreshed; nothing was resubmitted."
            )
            await self._poll_queue(force=True)
            if path == "/api/sources":
                await self._refresh_sources()
            return None
        except Exception:
            self.query_one("#preview", Static).update(
                "Local server rejected the mutation; review the fields and preview again."
            )
            return None
        self.query_one("#preview", Static).update(success)
        await self._poll_queue(force=True)
        return result

    @on(Select.Changed)
    def select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "provider":
            official = event.value == "official_x_api"
            surface = self.query_one("#surface", Select)
            surface.set_options(
                [("Profile", "profile"), ("Search", "search")]
                if official
                else [("Home", "home"), ("Profile", "profile"), ("Latest search", "search")]
            )
            surface.value = "profile" if official else "home"
            self.query_one("#max-posts", Input).value = "25" if official else "5"
            for selector in (
                "#search-mode",
                "#start-date",
                "#end-date",
                "#include-replies",
                "#media-only",
            ):
                self.query_one(selector).disabled = not official
        if event.select.id == "surface":
            field = self.query_one("#source-value", Input)
            field.disabled = event.value == "home"
            if event.value == "home":
                field.value = "home"
            elif field.value == "home":
                field.value = ""
        self._discard_previews()

    @on(Input.Changed)
    def input_changed(self, event: Input.Changed) -> None:
        if event.input.id not in {"bearer-token", "source-name"}:
            self._discard_previews()

    @on(Checkbox.Changed)
    def checkbox_changed(self, _event: Checkbox.Changed) -> None:
        self._discard_previews()

    @on(SelectionList.SelectedChanged)
    def selection_changed(self, _event: SelectionList.SelectedChanged) -> None:
        self._discard_previews()

    @on(Button.Pressed, "#initialize-runtime")
    async def initialize_pressed(self) -> None:
        await self._poll_connection()
        self.query_one("#setup-status", Static).update(
            "Runtime is already initialized; provider readiness refreshed."
        )

    @on(Button.Pressed, "#save-token")
    async def save_token_pressed(self) -> None:
        field = self.query_one("#bearer-token", Input)
        token = field.value
        field.value = ""
        if self.settings is None:
            return
        try:
            await asyncio.to_thread(save_bearer_token, self.settings, token)
        except Exception:
            self.query_one("#setup-status", Static).update("Token was not saved.")
        else:
            self.query_one("#setup-status", Static).update("Protected token saved; value cleared.")
            await self._poll_connection()

    @on(Button.Pressed, "#browser-auth")
    def browser_auth_pressed(self) -> None:
        if self.settings is None or self._auth_running:
            return
        self._auth_running = True
        self._auth_cancel.clear()
        self.query_one("#setup-status", Static).update(
            "Headed Browser authentication is open; close it before quitting."
        )
        self._auth_task = asyncio.create_task(self._run_browser_auth())

    async def _run_browser_auth(self) -> None:
        try:
            await asyncio.to_thread(
                self.authenticate,
                self.settings,
                should_cancel=self._auth_cancel.is_set,
            )
        except Exception:
            self.query_one("#setup-status", Static).update(
                "Browser authentication did not complete."
            )
        else:
            self.query_one("#setup-status", Static).update("Browser authentication state saved.")
            await self._poll_connection()
        finally:
            self._auth_running = False
            self._auth_task = None
            if self._quit_after_auth:
                self.exit()

    @on(Button.Pressed, "#save-source")
    async def save_source_pressed(self) -> None:
        try:
            request = self._request_body()
        except (TypeError, ValueError):
            self.query_one("#preview", Static).update("Source fields are invalid.")
            return
        name = self.query_one("#source-name", Input).value.strip()
        body = {
            "displayName": name or f"{request['sourceType']}: {request['sourceValue']}",
            "provider": request["provider"],
            "surface": request["sourceType"],
            "value": request["sourceValue"],
        }
        if await self._mutation("/api/sources", body, "Saved source created.") is not None:
            await self._refresh_sources()

    @on(Button.Pressed, "#preview-single")
    async def preview_single_pressed(self) -> None:
        try:
            body = self._request_body()
            preview = await self._call(self.client.post, "/api/collections/preview", body)
        except Exception:
            self.query_one("#preview", Static).update(
                "Preview rejected; review the capture fields."
            )
            return
        self._single_preview = preview
        self._single_signature = self._capture_signature_value()
        self._batch_preview = None
        self._batch_signature = None
        button = self.query_one("#confirm-single", Button)
        button.disabled = False
        button.label = (
            "Confirm paid official read"
            if preview.get("provider") == "official_x_api"
            else "Confirm Browser capture"
        )
        self.query_one("#confirm-batch", Button).disabled = True
        self._show_single_preview(preview)

    @on(Button.Pressed, "#confirm-single")
    async def confirm_single_pressed(self) -> None:
        preview = self._single_preview
        if preview is None or self._single_signature != self._capture_signature_value():
            self._discard_previews()
            return
        plan = (
            preview.get("executionPlan") if isinstance(preview.get("executionPlan"), dict) else {}
        )
        if not self._not_expired(plan.get("expiresAt")):
            self._discard_previews()
            self.query_one("#preview", Static).update("Preview expired; preview again.")
            return
        provider = preview.get("provider")
        confirmation = (
            "confirmPaidRead" if provider == "official_x_api" else "confirmBrowserCapture"
        )
        body = {**preview["request"], "executionPlan": plan, confirmation: True}
        result = await self._mutation(
            "/api/jobs", body, "Approved job admitted to the durable queue."
        )
        if result is not None:
            self._single_preview = None
            self._single_signature = None
            self.query_one("#confirm-single", Button).disabled = True

    @on(Button.Pressed, "#preview-batch")
    async def preview_batch_pressed(self) -> None:
        selected = list(self.query_one("#saved-sources", SelectionList).selected)
        if not 2 <= len(selected) <= 25:
            self.query_one("#preview", Static).update("Select 2–25 saved sources.")
            return
        try:
            priority = int(self.query_one("#batch-priority", Input).value)
            deadline = int(self.query_one("#batch-deadline", Input).value)
            browser_posts = int(self.query_one("#batch-browser-posts", Input).value)
            official_posts = int(self.query_one("#batch-official-posts", Input).value)
            items = [
                {
                    "sourceId": source_id,
                    "maxPosts": (
                        browser_posts
                        if self._sources[source_id].get("provider") == "playwright_browser"
                        else official_posts
                    ),
                    "priority": priority,
                }
                for source_id in selected
            ]
            preview = await self._call(
                self.client.post,
                "/api/batches/preview",
                {
                    "items": items,
                    "deadlineSeconds": deadline,
                    "freshnessChoice": "capture_fresh",
                },
            )
        except Exception:
            self.query_one("#preview", Static).update("Batch preview rejected; review the fields.")
            return
        self._batch_preview = preview
        self._batch_signature = self._batch_signature_value()
        self._single_preview = None
        self._single_signature = None
        button = self.query_one("#confirm-batch", Button)
        button.disabled = False
        manifest = preview.get("manifest") if isinstance(preview.get("manifest"), dict) else {}
        items = manifest.get("items", [])
        includes_paid = any(
            isinstance(item, dict) and item.get("provider") == "official_x_api" for item in items
        )
        button.label = (
            "Confirm batch including paid official reads"
            if includes_paid
            else "Confirm Browser batch"
        )
        self.query_one("#confirm-single", Button).disabled = True
        self._show_batch_preview(preview)

    @on(Button.Pressed, "#confirm-batch")
    async def confirm_batch_pressed(self) -> None:
        preview = self._batch_preview
        if preview is None or self._batch_signature != self._batch_signature_value():
            self._discard_previews()
            return
        manifest = preview.get("manifest") if isinstance(preview.get("manifest"), dict) else {}
        if not self._not_expired(manifest.get("expiresAt")):
            self._discard_previews()
            self.query_one("#preview", Static).update("Batch preview expired; preview again.")
            return
        result = await self._mutation(
            "/api/batches/confirm",
            {
                "confirm": True,
                "manifest": manifest,
                "approvalDigest": preview.get("approvalDigest"),
            },
            "Approved batch admitted atomically to the durable queue.",
        )
        if result is not None:
            batch_id = result.get("batchId")
            self._current_batch_id = batch_id if isinstance(batch_id, str) else None
            self._batch_preview = None
            self._batch_signature = None
            self.query_one("#confirm-batch", Button).disabled = True

    @on(Button.Pressed, "#cancel-job")
    async def cancel_job_pressed(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        row = table.cursor_row
        if not 0 <= row < len(self._job_rows):
            return
        await self._mutation(
            f"/api/jobs/{self._job_rows[row]}/cancel",
            {},
            "Cancellation requested; durable terminal state will win.",
        )

    @on(Button.Pressed, "#cancel-batch")
    async def cancel_batch_pressed(self) -> None:
        if self._current_batch_id is None:
            self.query_one("#preview", Static).update("No current-session batch is available.")
            return
        await self._mutation(
            f"/api/batches/{self._current_batch_id}/cancel",
            {"confirm": True},
            "Remaining batch jobs received an explicit cancellation request.",
        )

    def action_show_tab(self, pane: str) -> None:
        if not self.owner:
            return
        self.query_one("#operations-tabs", TabbedContent).active = pane

    def action_open_web(self) -> None:
        self.open_browser(self.base_url)

    def action_quit_safely(self) -> None:
        if self._auth_running:
            self._quit_after_auth = True
            self._auth_cancel.set()
            self.query_one("#setup-status", Static).update(
                "Closing headed Browser authentication before quitting."
            )
            return
        self.exit()


def run_owner(
    base_url: str,
    settings: Settings,
) -> int:
    TerminalWorkbench(
        base_url,
        owner=True,
        settings=settings,
    ).run()
    return 0


def run_monitor(base_url: str) -> int:
    TerminalWorkbench(base_url, owner=False).run()
    return 0
