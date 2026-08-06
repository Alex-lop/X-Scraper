import stat
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from xworkbench.config import Settings
from xworkbench.errors import CollectionCancelled, InvalidRequestError
from xworkbench.models import CollectionRequest
from xworkbench.playwright_browser import (
    BrowserManualActionRequired,
    BrowserSchemaError,
    BrowserSessionExpiredError,
    BrowserTimeoutError,
    PlaywrightBrowserProvider,
    _record_status,
    authenticate,
    parse_projected_article,
)

FIXTURE = Path(__file__).parent / "fixtures" / "home_feed.html"


def settings(tmp_path, **changes):
    values = {
        "database_path": tmp_path / "workbench.db",
        "bearer_token_path": tmp_path / "api-token",
        "storage_state_path": tmp_path / "auth" / "playwright.json",
        "browser_headless": True,
        "job_timeout_seconds": 10,
        "page_timeout_ms": 100,
        "no_progress_limit": 2,
    }
    values.update(changes)
    return Settings(**values)


def request(max_posts=2):
    return CollectionRequest.from_dict(
        {
            "provider": "playwright_browser",
            "sourceType": "home",
            "maxPosts": max_posts,
        }
    )


def article(post_id, *, text="visible", nested_id=None, position=0):
    candidates = [
        {
            "href": f"https://x.com/tester/status/{post_id}",
            "depth": 2,
            "order": 1,
            "hasTime": True,
            "timestamp": "2026-08-05T12:00:00Z",
            "nested": False,
        }
    ]
    if nested_id:
        candidates.insert(
            0,
            {
                "href": f"https://x.com/quoted/status/{nested_id}",
                "depth": 5,
                "order": 0,
                "hasTime": True,
                "timestamp": "2026-08-05T11:59:00Z",
                "nested": True,
            },
        )
    return {
        "identityCandidates": candidates,
        "text": text,
        "userText": "Tester @tester",
        "metrics": {},
        "media": [],
        "sourcePosition": position,
    }


def fixture_projections():
    root = ElementTree.fromstring(FIXTURE.read_text())
    parents = {child: parent for parent in root.iter() for child in parent}

    def nested(element, outer):
        node = element
        while node is not outer:
            if node.attrib.get("data-testid") in {"quoteTweet", "card.wrapper"}:
                return True
            node = parents[node]
        return False

    def outer_element(article_element, test_id):
        return next(
            (
                item
                for item in article_element.iter()
                if item.attrib.get("data-testid") == test_id
                and not nested(item, article_element)
            ),
            None,
        )

    result = []
    for source_position, article_element in enumerate(root.findall(".//article")):
        candidates = []
        for order, anchor in enumerate(article_element.findall(".//a")):
            if "/status/" not in anchor.attrib.get("href", ""):
                continue
            depth = 0
            node = anchor
            while node is not article_element:
                depth += 1
                node = parents[node]
            timestamp = anchor.find(".//time")
            candidates.append(
                {
                    "href": anchor.attrib["href"],
                    "depth": depth,
                    "order": order,
                    "hasTime": timestamp is not None,
                    "timestamp": (
                        timestamp.attrib.get("datetime") if timestamp is not None else None
                    ),
                    "nested": nested(anchor, article_element),
                }
            )
        text = outer_element(article_element, "tweetText")
        user = outer_element(article_element, "User-Name")
        media = []
        for image in article_element.findall(".//img"):
            if nested(image, article_element):
                continue
            media.append(
                {
                    "type": "photo",
                    "url": image.attrib.get("src"),
                    "altText": image.attrib.get("alt"),
                }
            )
        result.append(
            {
                "identityCandidates": candidates,
                "text": "".join(text.itertext()) if text is not None else None,
                "userText": "".join(user.itertext()) if user is not None else None,
                "articleText": "".join(article_element.itertext()),
                "metrics": {
                    key: (
                        outer_element(article_element, test_id).attrib.get("aria-label")
                        if outer_element(article_element, test_id) is not None
                        else None
                    )
                    for key, test_id in {
                        "reply": "reply",
                        "repost": "retweet",
                        "like": "like",
                        "bookmark": "bookmark",
                    }.items()
                },
                "media": media,
                "sourcePosition": source_position,
            }
        )
    return result


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def wait_for(self, **_kwargs):
        if self.page.wait_error:
            raise self.page.wait_error

    def inner_text(self, **_kwargs):
        return self.page.body

    def evaluate(self, _script):
        return self.page.body

    def count(self):
        if "AppTabBar_Home_Link" in self.selector:
            return int("/home" in self.page.url)
        if "autocomplete" in self.selector or "input[name" in self.selector:
            return int(self.page.login_wall)
        return 0

    def evaluate_all(self, _script):
        value = self.page.scans[min(self.page.scan_index, len(self.page.scans) - 1)]
        self.page.scan_index += 1
        if isinstance(value, Exception):
            raise value
        return value


class FakePage:
    def __init__(
        self,
        scans,
        *,
        url="https://x.com/home",
        body="",
        wait_error=None,
        preserve_url=False,
        login_wall=False,
    ):
        self.scans = scans
        self.url = url
        self.body = body
        self.wait_error = wait_error
        self.preserve_url = preserve_url
        self.login_wall = login_wall
        self.scan_index = 0
        self.scrolls = 0
        self.closed = False

    def locator(self, selector):
        return FakeLocator(self, selector)

    def set_default_timeout(self, _timeout):
        pass

    def goto(self, url, **_kwargs):
        if not self.preserve_url and "/login" not in url:
            self.url = url

    def evaluate(self, _script):
        self.scrolls += 1

    def wait_for_timeout(self, _timeout):
        pass

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def storage_state(self, *, path):
        Path(path).write_text('{"cookies":[{"name":"sensitive"}]}')

    def close(self):
        self.closed = True


class FakeBrowser:
    version = "synthetic-chromium"

    def __init__(self, context):
        self.context = context
        self.closed = False

    def new_context(self, **_kwargs):
        return self.context

    def close(self):
        self.closed = True


class FakeLifecycle:
    def __init__(self, scans, **page_options):
        page_options.setdefault("preserve_url", "url" in page_options)
        self.page = FakePage(scans, **page_options)
        self.context = FakeContext(self.page)
        self.browser = FakeBrowser(self.context)
        self.headless = None
        self.chromium = SimpleNamespace(launch=self.launch)

    def launch(self, *, headless):
        self.headless = headless
        return self.browser

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def ready_provider(tmp_path, lifecycle, **setting_changes):
    configured = settings(tmp_path, **setting_changes)
    configured.storage_state_path.parent.mkdir(parents=True)
    configured.storage_state_path.write_text("{}")
    return (
        PlaywrightBrowserProvider(configured, _playwright_factory=lambda: lifecycle),
        configured,
    )


def checkpoint(stored=0, state=None):
    return {"providerState": state, "storedCount": stored, "metadata": {}}


def test_fixture_parser_prefers_outer_timestamp_identity_and_preserves_nullable_fields():
    first, missing, invalid, nested_only = [
        parse_projected_article(item) for item in fixture_projections()
    ]

    assert first.post_id == "111" and first.author_username == "alice"
    assert first.created_at == "2026-08-05T12:00:00Z" and first.is_quote is True
    assert first.text.startswith("  exact") and first.like_count == 1234
    assert first.reply_count == 2 and first.repost_count == 3
    assert first.bookmark_count is None and first.media[0]["altText"] == "Synthetic landscape"
    assert missing.post_id == "222" and missing.author_username is None
    assert missing.text is missing.created_at is missing.like_count is missing.media is None
    assert invalid is None
    assert nested_only is None


def test_prepare_and_passive_session_states(tmp_path):
    lifecycle = FakeLifecycle([[article("1")]])
    provider, configured = ready_provider(tmp_path, lifecycle)
    plan = provider.prepare(request())

    assert plan["sourceUrl"] == "https://x.com/home"
    assert "maximumPostResources" not in plan
    assert provider.prepare(request(), plan) is plan
    assert provider.capabilities()["limits"]["maximum"] == 25
    assert provider.connection_status()["status"] == "ready"
    _record_status(configured, "expired")
    assert provider.connection_status()["status"] == "expired"
    _record_status(configured, "manual_action_required")
    assert provider.connection_status()["status"] == "manual_action_required"
    configured.storage_state_path.unlink()
    assert provider.connection_status()["status"] == "missing"
    with pytest.raises(InvalidRequestError, match="does not match"):
        provider.prepare(request(), {**plan, "sourceUrl": "https://example.invalid"})


def test_virtualized_scans_deduplicate_and_persist_each_batch(tmp_path):
    lifecycle = FakeLifecycle(
        [[article("1"), article("1")], [article("1"), article("2", nested_id="9")]]
    )
    provider, _ = ready_provider(tmp_path, lifecycle)
    batches = []

    summary = provider.collect(
        request(),
        execution_plan=provider.prepare(request()),
        checkpoint=checkpoint(),
        on_batch=lambda posts, state, metadata: (
            batches.append((posts, state, metadata)) or len(posts)
        ),
        should_cancel=lambda: False,
    )

    assert [[post.post_id for post in batch[0]] for batch in batches] == [["1"], ["2"]]
    assert batches[-1][1]["seenPostIds"] == ["1", "2"]
    assert batches[-1][2]["scanIterations"] == 2
    assert lifecycle.page.scrolls == 1 and summary.completion_reason == "target_reached"


def test_no_progress_stops_after_immediate_empty_batches(tmp_path):
    lifecycle = FakeLifecycle([[article("1")]])
    provider, _ = ready_provider(tmp_path, lifecycle, no_progress_limit=2)
    sizes = []
    summary = provider.collect(
        request(3),
        execution_plan=provider.prepare(request(3)),
        checkpoint=checkpoint(),
        on_batch=lambda posts, *_args: sizes.append(len(posts)) or len(posts),
        should_cancel=lambda: False,
    )

    assert sizes == [1, 0, 0]
    assert summary.partial and summary.completion_reason == "no_progress"


def test_later_schema_failure_keeps_first_batch_and_always_closes(tmp_path):
    lifecycle = FakeLifecycle([[article("1")], RuntimeError("DOM changed")])
    provider, _ = ready_provider(tmp_path, lifecycle)
    persisted = []
    with pytest.raises(BrowserSchemaError):
        provider.collect(
            request(),
            execution_plan=provider.prepare(request()),
            checkpoint=checkpoint(),
            on_batch=lambda posts, *_args: persisted.extend(posts) or len(posts),
            should_cancel=lambda: False,
        )

    assert [post.post_id for post in persisted] == ["1"]
    assert lifecycle.page.closed and lifecycle.context.closed and lifecycle.browser.closed


def test_cancellation_and_timeout_close_every_browser_object(tmp_path):
    cancelled_lifecycle = FakeLifecycle([[article("1")]])
    provider, _ = ready_provider(tmp_path, cancelled_lifecycle)
    with pytest.raises(CollectionCancelled):
        provider.collect(
            request(),
            execution_plan=provider.prepare(request()),
            checkpoint=checkpoint(),
            on_batch=lambda *_args: 0,
            should_cancel=lambda: True,
        )
    assert cancelled_lifecycle.page.closed and cancelled_lifecycle.context.closed
    assert cancelled_lifecycle.browser.closed

    moments = iter((0.0, 2.0))
    timeout_lifecycle = FakeLifecycle([[article("1")]])
    configured = settings(tmp_path, job_timeout_seconds=1)
    configured.storage_state_path.write_text("{}")
    timed = PlaywrightBrowserProvider(
        configured,
        _playwright_factory=lambda: timeout_lifecycle,
        _monotonic=lambda: next(moments),
    )
    with pytest.raises(BrowserTimeoutError):
        timed.collect(
            request(),
            execution_plan=timed.prepare(request()),
            checkpoint=checkpoint(),
            on_batch=lambda *_args: 0,
            should_cancel=lambda: False,
        )
    assert timeout_lifecycle.page.closed and timeout_lifecycle.browser.closed


@pytest.mark.parametrize(
    ("page_options", "error", "status"),
    [
        ({"url": "https://x.com/i/flow/login"}, BrowserSessionExpiredError, "expired"),
        ({"login_wall": True}, BrowserSessionExpiredError, "expired"),
        (
            {"body": "Please verify you are human"},
            BrowserManualActionRequired,
            "manual_action_required",
        ),
    ],
)
def test_session_failures_are_truthful_and_close(tmp_path, page_options, error, status):
    lifecycle = FakeLifecycle([[article("1")]], **page_options)
    provider, _ = ready_provider(tmp_path, lifecycle)
    with pytest.raises(error):
        provider.collect(
            request(),
            execution_plan=provider.prepare(request()),
            checkpoint=checkpoint(),
            on_batch=lambda *_args: 0,
            should_cancel=lambda: False,
        )
    assert provider.connection_status()["status"] == status
    assert lifecycle.page.closed and lifecycle.context.closed and lifecycle.browser.closed


def test_headed_auth_saves_state_atomically_with_owner_only_permissions(tmp_path):
    lifecycle = FakeLifecycle([[]])
    configured = settings(tmp_path)

    result = authenticate(configured, _playwright_factory=lambda: lifecycle)

    assert result["status"] == "ready" and lifecycle.headless is False
    assert stat.S_IMODE(configured.storage_state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(
        configured.storage_state_path.with_name(".playwright.json.auth-status").stat().st_mode
    ) == 0o600
    assert lifecycle.page.closed and lifecycle.context.closed and lifecycle.browser.closed
