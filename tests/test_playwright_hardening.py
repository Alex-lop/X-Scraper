import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from xworkbench.config import Settings
from xworkbench.errors import CollectionCancelled
from xworkbench.models import CollectionRequest
from xworkbench.playwright_browser import (
    BrowserSessionInvalidError,
    PlaywrightBrowserProvider,
    _atomic_text,
    _record_status,
    _save_storage_state,
)


def _settings(tmp_path, **changes):
    values = {
        "database_path": tmp_path / "workbench.db",
        "bearer_token_path": tmp_path / "token",
        "storage_state_path": tmp_path / "auth" / "playwright.json",
        "browser_headless": True,
        "job_timeout_seconds": 5,
        "page_timeout_ms": 300,
        "no_progress_limit": 2,
    }
    values.update(changes)
    return Settings(**values)


def _write_state(settings, value, *, mode=0o600):
    path = settings.storage_state_path
    path.parent.mkdir(parents=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(mode)


def _provider(settings, lifecycle=None):
    return PlaywrightBrowserProvider(
        settings, _playwright_factory=lambda: lifecycle or SimpleNamespace()
    )


def _request(target=1):
    return CollectionRequest.from_dict(
        {"provider": "playwright_browser", "sourceType": "home", "maxPosts": target}
    )


@pytest.mark.parametrize(
    ("state", "mode"),
    [
        ("not-json", 0o600),
        ({}, 0o600),
        ({"cookies": [], "origins": "wrong"}, 0o600),
        (
            {
                "cookies": [{"name": "foreign", "value": "secret", "domain": "evilx.com"}],
                "origins": [],
            },
            0o600,
        ),
        (
            {
                "cookies": [],
                "origins": [{"origin": "http://x.com", "localStorage": []}],
            },
            0o600,
        ),
        ({"cookies": [], "origins": []}, 0o644),
    ],
)
def test_invalid_or_nonprivate_local_state_never_becomes_ready(tmp_path, state, mode):
    settings = _settings(tmp_path)
    path = settings.storage_state_path
    path.parent.mkdir(parents=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(state if isinstance(state, str) else json.dumps(state), encoding="utf-8")
    path.chmod(mode)

    status = _provider(settings).connection_status()

    assert status["status"] == "invalid_local_state"
    assert status["localStateValid"] is status["valid"] is status["ready"] is False


def test_state_marker_is_digest_bound_and_unverified_state_is_rejected(tmp_path):
    settings = _settings(tmp_path)
    _write_state(settings, {"cookies": [], "origins": []})
    provider = _provider(settings)

    status = provider.connection_status()
    assert {key: status[key] for key in ("status", "valid", "ready")} == {
        "status": "present_unverified",
        "valid": True,
        "ready": False,
    }
    with pytest.raises(BrowserSessionInvalidError):
        provider.prepare(_request())

    verified_at = "2026-08-19T12:00:00+00:00"
    assert _record_status(settings, "verified_live", verified_at=verified_at)
    marker = json.loads(
        settings.storage_state_path.with_name(".playwright.json.auth-status").read_text()
    )
    status = provider.connection_status()
    assert marker["stateSha256"] == hashlib.sha256(
        settings.storage_state_path.read_bytes()
    ).hexdigest()
    assert status["status"] == "verified_live" and status["ready"] is True
    assert status["verifiedAt"] == verified_at

    for bound_status in ("expired", "manual_action_required", "unavailable"):
        assert _record_status(settings, bound_status)
        status = provider.connection_status()
        assert status["status"] == bound_status and status["ready"] is False
        assert status["verifiedAt"] == verified_at

    settings.storage_state_path.write_text(
        settings.storage_state_path.read_text() + "\n", encoding="utf-8"
    )
    assert provider.connection_status()["status"] == "present_unverified"
    assert provider.connection_status()["ready"] is False


class _MixedStateContext:
    def storage_state(self, *, path):
        Path(path).write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "kept", "value": "x", "domain": ".x.com"},
                        {"name": "dropped", "value": "secret", "domain": "example.com"},
                    ],
                    "origins": [
                        {"origin": "https://x.com", "localStorage": []},
                        {
                            "origin": "https://example.com",
                            "localStorage": [{"name": "secret", "value": "foreign"}],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )


def test_refresh_filters_foreign_state_and_refuses_stale_overwrite(tmp_path):
    settings = _settings(tmp_path)
    _write_state(settings, {"cookies": [], "origins": []})
    original_digest = hashlib.sha256(settings.storage_state_path.read_bytes()).hexdigest()

    assert _save_storage_state(
        _MixedStateContext(), settings.storage_state_path, expected_digest=original_digest
    )
    saved = json.loads(settings.storage_state_path.read_text())
    assert [cookie["name"] for cookie in saved["cookies"]] == ["kept"]
    assert [origin["origin"] for origin in saved["origins"]] == ["https://x.com"]
    assert settings.storage_state_path.stat().st_mode & 0o777 == 0o600

    current = settings.storage_state_path.read_bytes()
    assert not _save_storage_state(
        _MixedStateContext(), settings.storage_state_path, expected_digest=original_digest
    )
    assert settings.storage_state_path.read_bytes() == current


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode regression")
def test_auth_writes_refuse_unsafe_parent_before_any_secret_is_written(tmp_path):
    parent = tmp_path / "auth"
    parent.mkdir(mode=0o700)
    parent.chmod(0o755)
    target = parent / "playwright.json"

    class RecordingContext:
        called = False

        def storage_state(self, *, path):
            self.called = True
            Path(path).write_text("secret", encoding="utf-8")

    context = RecordingContext()
    with pytest.raises(OSError, match="0700"):
        _save_storage_state(context, target)
    with pytest.raises(OSError, match="0700"):
        _atomic_text(target, "secret")

    assert context.called is False
    assert parent.stat().st_mode & 0o777 == 0o755
    assert list(parent.iterdir()) == []


def _article():
    return {
        "identityCandidates": [
            {
                "href": "https://x.com/fixture/status/42",
                "depth": 1,
                "order": 0,
                "hasTime": True,
                "timestamp": "2026-08-19T12:00:00Z",
                "nested": False,
            }
        ],
        "text": "Synthetic hardening fixture",
        "userText": "Fixture @fixture",
        "articleText": "Synthetic hardening fixture",
        "socialContext": None,
        "metrics": {"like": "1.2K Likes", "quote": "2.5K Quotes", "view": "3K Views"},
        "media": [],
        "sourcePosition": 0,
    }


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        return 0

    def wait_for(self, **_kwargs):
        return None

    def evaluate(self, _script):
        return ""

    def evaluate_all(self, _script):
        return [_article()]


class _Page:
    def __init__(self, *, cancel_on_wait=False):
        self.url = "https://x.com/home"
        self.cancel_on_wait = cancel_on_wait
        self.cancelled = False
        self.waits = []
        self.goto_timeout = None
        self.default_timeout = None
        self.closed = False

    def locator(self, selector):
        return _Locator(self, selector)

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def goto(self, _url, **kwargs):
        self.goto_timeout = kwargs["timeout"]

    def evaluate(self, _script):
        return None

    def wait_for_timeout(self, timeout):
        self.waits.append(timeout)
        self.cancelled = self.cancel_on_wait

    def close(self):
        self.closed = True


class _Lifecycle:
    def __init__(self, *, cancel_on_wait=False):
        self.page = _Page(cancel_on_wait=cancel_on_wait)
        self.context = SimpleNamespace(closed=False)
        self.browser = SimpleNamespace(version="fixture", closed=False)
        self.launch_timeout = None
        self.context.new_page = lambda: self.page
        self.context.storage_state = lambda *, path: Path(path).write_text(
            '{"cookies":[],"origins":[]}', encoding="utf-8"
        )
        self.context.close = lambda: setattr(self.context, "closed", True)
        self.browser.new_context = lambda **_kwargs: self.context
        self.browser.close = lambda: setattr(self.browser, "closed", True)
        self.chromium = SimpleNamespace(launch=self.launch)

    def launch(self, **kwargs):
        self.launch_timeout = kwargs["timeout"]
        return self.browser

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _ready_provider(tmp_path, lifecycle):
    settings = _settings(tmp_path)
    _write_state(settings, {"cookies": [], "origins": []})
    _record_status(settings, "verified_live")
    return _provider(settings, lifecycle), settings


def test_callback_failure_is_not_reclassified_or_allowed_to_invalidate_session(tmp_path):
    lifecycle = _Lifecycle()
    provider, _ = _ready_provider(tmp_path, lifecycle)
    request = _request()

    def fail(*_args):
        raise RuntimeError("synthetic persistence failure")

    with pytest.raises(RuntimeError, match="synthetic persistence failure"):
        provider.collect(
            request,
            execution_plan=provider.prepare(request),
            checkpoint={"providerState": None, "storedCount": 0},
            on_batch=fail,
            should_cancel=lambda: False,
        )

    assert provider.connection_status()["status"] == "verified_live"
    assert lifecycle.page.closed and lifecycle.context.closed and lifecycle.browser.closed


def test_progress_wait_is_cancel_aware_and_uses_bounded_polling(tmp_path):
    lifecycle = _Lifecycle(cancel_on_wait=True)
    provider, _ = _ready_provider(tmp_path, lifecycle)
    request = _request(2)

    with pytest.raises(CollectionCancelled):
        provider.collect(
            request,
            execution_plan=provider.prepare(request),
            checkpoint={"providerState": None, "storedCount": 0},
            on_batch=lambda posts, *_args: len(posts),
            should_cancel=lambda: lifecycle.page.cancelled,
        )

    assert lifecycle.page.waits == [100]
    assert lifecycle.page.closed and lifecycle.context.closed and lifecycle.browser.closed


def test_collection_emits_segment_coverage_and_deadline_metadata(tmp_path):
    lifecycle = _Lifecycle()
    provider, _ = _ready_provider(tmp_path, lifecycle)
    request = _request()
    captured = []

    plan = provider.prepare(request)
    summary = provider.collect(
        request,
        execution_plan=plan,
        checkpoint={"providerState": {"captureSegment": 3}, "storedCount": 0},
        on_batch=lambda posts, state, metadata: (
            captured.append((posts, state, metadata)) or len(posts)
        ),
        should_cancel=lambda: False,
    )

    posts, state, metadata = captured[0]
    assert summary.completion_reason == "target_reached"
    assert posts[0].like_count == 1_200 and posts[0].quote_count == 2_500
    assert posts[0].view_count == 3_000
    assert state["captureSegment"] == metadata["captureSegment"] == 4
    assert plan["providerVersion"] == plan["parserVersion"] == 2
    assert metadata["providerVersion"] == metadata["parserVersion"] == 2
    assert metadata["fieldCoverage"]["viewCount"] == {
        "present": 1,
        "total": 1,
        "ratio": 1.0,
    }
    assert metadata["visibleCards"] == metadata["parsedCards"] == 1
    assert 0 < lifecycle.launch_timeout <= 5_000
    assert 0 < lifecycle.page.goto_timeout <= lifecycle.page.default_timeout <= 300


def test_selector_drift_report_is_sanitized_and_never_promoted(tmp_path):
    provider = _provider(_settings(tmp_path))
    page = SimpleNamespace()
    page.locator = lambda _selector: SimpleNamespace(
        evaluate_all=lambda _script: [
            {
                "tag": "article",
                "testId": 'tweet\"><script>',
                "role": "article",
                "statusLinks": 1,
                "times": 1,
                "textNodes": 1,
                "userNodes": 1,
            }
        ]
    )

    report = provider._selector_drift_report(page)

    assert report["autoPromoted"] is False
    assert report["action"] == "maintainer_review_required"
    assert report["candidates"][0]["candidate"]["testId"] is None
