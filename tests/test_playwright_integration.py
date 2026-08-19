import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from xworkbench.config import Settings
from xworkbench.models import CollectionRequest
from xworkbench.playwright_browser import (
    ARTICLE_SELECTOR,
    DOM_PROJECTION,
    HOME_URL,
    PlaywrightBrowserProvider,
    _record_status,
    parse_projected_article,
)

FIXTURES = Path(__file__).parent / "fixtures"
CARDS = FIXTURES / "playwright_cards.html"
TIMELINE = FIXTURES / "playwright_virtualized_timeline.html"


def test_production_dom_projection_in_real_chromium_covers_sanitized_cards():
    requested = []
    blocked = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()

        def local_files_only(route):
            requested.append(route.request.url)
            if route.request.url.startswith(("file:", "data:")):
                route.continue_()
            else:
                blocked.append(route.request.url)
                route.abort()

        context.route("**/*", local_files_only)
        page = context.new_page()
        page.goto(CARDS.resolve().as_uri(), wait_until="domcontentloaded")
        projected = page.locator(ARTICLE_SELECTOR).evaluate_all(DOM_PROJECTION)
        context.close()
        browser.close()

    parsed = [parse_projected_article(item) for item in projected]
    by_id = {
        post.post_id: (item, post)
        for item, post in zip(projected, parsed, strict=True)
        if post is not None
    }

    assert blocked == []
    assert requested and all(url.startswith(("file:", "data:")) for url in requested)
    assert len(projected) == 8
    assert set(by_id) == {"1001", "1002", "1003", "1004", "1005", "1006", "1007"}

    compact, original = by_id["1001"]
    assert original.text == "Synthetic original card."
    assert compact["metrics"] == {
        "reply": "1.2K Replies",
        "repost": "2.5M reposts",
        "quote": "4.5K Quotes",
        "like": "3B Likes",
        "bookmark": None,
        "view": "6.7K Views",
    }
    assert original.quote_count == 4_500
    assert original.view_count == 6_700

    assert by_id["1002"][1].is_reply is True
    assert by_id["1003"][1].is_repost is True
    quote = by_id["1004"][1]
    assert quote.is_quote is True and quote.text == "Synthetic outer quote text."
    assert "1904" not in by_id

    media = by_id["1005"][1]
    assert media.text is None and media.has_media is True
    assert media.media[0]["url"].startswith("data:image/svg+xml")
    promoted = by_id["1006"][1]
    assert promoted.text == "Synthetic promoted-like card." and promoted.is_repost is None

    missing = by_id["1007"][1]
    assert missing.text is missing.author_username is missing.created_at is None
    assert missing.like_count is missing.reply_count is missing.repost_count is None
    drift = next(item for item in projected if "deliberately drifted" in item["articleText"])
    assert drift["identityCandidates"] == [] and parse_projected_article(drift) is None


class _FixturePlaywright:
    def __init__(self, html):
        self.html = html
        self.destination = HOME_URL
        self.served = []
        self.blocked = []
        self.closed = set()
        self.metadata = []

    def __enter__(self):
        self._manager = sync_playwright()
        playwright = self._manager.__enter__()
        self.chromium = _FixtureChromium(playwright.chromium, self)
        return self

    def __exit__(self, *args):
        self._exit_args = args
        return False

    def stop(self):
        self._manager.__exit__(*self._exit_args)

    def route(self, route):
        request = route.request
        if request.is_navigation_request() and request.url == self.destination:
            self.served.append(request.url)
            route.fulfill(status=200, content_type="text/html", body=self.html)
        else:
            self.blocked.append(request.url)
            route.abort()


class _FixtureChromium:
    def __init__(self, chromium, fixture):
        self._chromium = chromium
        self._fixture = fixture

    def launch(self, **kwargs):
        browser = self._chromium.launch(**kwargs)
        return _FixtureBrowser(browser, self._fixture)


class _FixtureBrowser:
    def __init__(self, browser, fixture):
        self._browser = browser
        self._fixture = fixture

    @property
    def version(self):
        return self._browser.version

    def new_context(self, **kwargs):
        context = self._browser.new_context(**kwargs)
        context.route("**/*", self._fixture.route)
        return _FixtureContext(context, self._fixture)

    def close(self):
        self._browser.close()
        if not self._browser.is_connected():
            self._fixture.closed.add("browser")


class _FixtureContext:
    def __init__(self, context, fixture):
        self._context = context
        self._fixture = fixture

    def new_page(self):
        page = self._context.new_page()
        return _FixturePage(page, self._fixture)

    def __getattr__(self, name):
        return getattr(self._context, name)

    def close(self):
        self._context.close()
        if not self._context.pages:
            self._fixture.closed.add("context")


class _FixturePage:
    def __init__(self, page, fixture):
        self._page = page
        self._fixture = fixture

    def __getattr__(self, name):
        return getattr(self._page, name)

    def close(self):
        self._page.close()
        if self._page.is_closed():
            self._fixture.closed.add("page")


def _collect_timeline(tmp_path, target, source_type="home", source_value=None):
    state_path = tmp_path / "auth" / "playwright.json"
    state_path.parent.mkdir(parents=True, mode=0o700)
    state_path.parent.chmod(0o700)
    state_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    state_path.chmod(0o600)
    configured = Settings(
        database_path=tmp_path / "workbench.db",
        bearer_token_path=tmp_path / "api-token",
        storage_state_path=state_path,
        browser_headless=True,
        job_timeout_seconds=10,
        page_timeout_ms=2_000,
        no_progress_limit=2,
    )
    _record_status(configured, "verified_live")
    fixture = _FixturePlaywright(TIMELINE.read_text(encoding="utf-8"))
    provider = PlaywrightBrowserProvider(configured, _playwright_factory=lambda: fixture)
    request_body = {
        "provider": "playwright_browser",
        "sourceType": source_type,
        "maxPosts": target,
    }
    if source_value is not None:
        request_body["sourceValue"] = source_value
    request = CollectionRequest.from_dict(request_body)
    plan = provider.prepare(request)
    fixture.destination = plan["sourceUrl"]
    batches = []
    states = []

    def save(posts, state, metadata):
        batches.append([post.post_id for post in posts])
        states.append(state)
        fixture.metadata.append(metadata)
        return len(posts)

    started = time.monotonic()
    try:
        summary = provider.collect(
            request,
            execution_plan=plan,
            checkpoint={
                "providerState": None,
                "storedCount": 0,
                "metadata": {"captureSegment": 0},
            },
            on_batch=save,
            should_cancel=lambda: False,
        )
    finally:
        fixture.stop()
    return summary, batches, states[-1], fixture, time.monotonic() - started


def test_dynamic_real_chromium_reaches_exact_target_without_duplicates_and_cleans_up(tmp_path):
    summary, batches, state, fixture, elapsed = _collect_timeline(tmp_path, 4)

    assert batches == [["2001"], ["2002"], ["2003"], ["2004"], []]
    assert state == {
        "seenPostIds": ["2001", "2002", "2003", "2004"],
        "scanIterations": 4,
        "scrollIterations": 3,
        "captureSegment": 0,
        "segmentScanIterations": 4,
    }
    assert summary.completion_reason == "target_reached" and summary.partial is False
    assert fixture.metadata[-1]["stopReason"] == "target_reached"
    assert elapsed < 10
    assert fixture.served == [HOME_URL] and fixture.blocked == []
    assert fixture.closed == {"page", "context", "browser"}


def test_dynamic_real_chromium_stall_is_bounded_and_cleans_up(tmp_path):
    summary, batches, state, fixture, elapsed = _collect_timeline(tmp_path, 6)

    assert batches == [["2001"], ["2002"], ["2003"], ["2004"], [], [], []]
    assert state == {
        "seenPostIds": ["2001", "2002", "2003", "2004"],
        "scanIterations": 6,
        "scrollIterations": 5,
        "captureSegment": 0,
        "segmentScanIterations": 6,
    }
    assert summary.completion_reason == "no_progress" and summary.partial is True
    assert fixture.metadata[-1]["stopReason"] == "no_progress"
    assert elapsed < 10
    assert fixture.served == [HOME_URL] and fixture.blocked == []
    assert fixture.closed == {"page", "context", "browser"}


@pytest.mark.parametrize(
    ("source_type", "source_value", "normalized", "destination"),
    [
        ("profile", "https://x.com/OpenAI", "openai", "https://x.com/openai"),
        (
            "search",
            "  cafe\u0301\n OR\tTea  ",
            "café OR Tea",
            "https://x.com/search?q=caf%C3%A9+OR+Tea&src=typed_query&f=live",
        ),
    ],
)
def test_real_chromium_uses_only_derived_profile_and_latest_search_destinations(
    tmp_path, source_type, source_value, normalized, destination
):
    summary, batches, _state, fixture, elapsed = _collect_timeline(
        tmp_path, 1, source_type, source_value
    )

    assert batches == [["2001"], []]
    assert summary.completion_reason == "target_reached"
    assert elapsed < 10
    assert fixture.served == [destination] and fixture.blocked == []
    assert fixture.closed == {"page", "context", "browser"}
    assert {
        key: fixture.metadata[-1][key]
        for key in ("sourceKind", "sourceValue", "sourceUrl", "stopReason")
    } == {
        "sourceKind": source_type,
        "sourceValue": normalized,
        "sourceUrl": destination,
        "stopReason": "target_reached",
    }
