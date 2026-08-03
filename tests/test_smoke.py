import json
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from xscraper.cli import main
from xscraper.config import Settings
from xscraper.errors import RateLimitedError, SchemaDriftError
from xscraper.models import CollectionSummary
from xscraper.providers.playwright import (
    TimelinePageObservation,
    _cursor_context,
    _expected_operation,
    _matches_request,
    parse_timeline,
)
from xscraper.smoke import (
    EXIT_PRECONDITION,
    EXIT_RATE_LIMIT,
    EXIT_SEMANTIC,
    EXIT_SESSION,
    REPORT_VERSION,
    SmokePreconditionError,
    SmokeRunError,
    run_graphql_smoke,
    semantic_fixture,
    structural_hash,
    structure_tree,
    validate_projection,
    validate_saved_state,
)


def result(tweet_id, *, username="fixture", reply=False, quote=False, media=False):
    legacy = {
        "full_text": f"original content {tweet_id}",
        "created_at": format_datetime(datetime.now(UTC)),
        "favorite_count": 1,
        "reply_count": 0,
        "retweet_count": 0,
        "quote_count": 0,
        "bookmark_count": 0,
        "is_quote_status": quote,
    }
    if reply:
        legacy["in_reply_to_status_id_str"] = "parent"
    if media:
        legacy["extended_entities"] = {
            "media": [
                {
                    "id_str": "media-original",
                    "type": "photo",
                    "media_url_https": "https://pbs.twimg.com/original.jpg",
                }
            ]
        }
    return {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {"user_results": {"result": {"legacy": {"screen_name": username}}}},
        "legacy": legacy,
    }


def timeline(*results, cursor="next-cursor"):
    entries = [
        {
            "entryId": f"tweet-{index}",
            "content": {"itemContent": {"tweet_results": {"result": item}}},
        }
        for index, item in enumerate(results)
    ]
    if cursor:
        entries.append(
            {
                "entryId": "cursor-bottom",
                "content": {"cursorType": "Bottom", "value": cursor},
            }
        )
    return {
        "data": {
            "timeline": {
                "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
            }
        }
    }


def test_cli_refuses_live_smoke_without_confirmation(capsys):
    assert main(["smoke", "graphql", "--profile", "fixture"]) == EXIT_PRECONDITION
    assert "--confirm-live-x" in capsys.readouterr().err


def test_saved_state_preflight_rejects_missing_and_invalid_files(tmp_path):
    with pytest.raises(SmokePreconditionError):
        validate_saved_state(tmp_path / "missing.json")
    invalid = tmp_path / "state.json"
    invalid.write_text("not json")
    invalid.chmod(0o600)
    with pytest.raises(SmokePreconditionError):
        validate_saved_state(invalid)


def settings_with_state(tmp_path):
    runtime = tmp_path / "var"
    state = runtime / "auth" / "storage_state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"cookies": [], "origins": []}')
    state.chmod(0o600)
    return Settings(
        runtime_dir=runtime,
        database_path=runtime / "history.db",
        storage_state_path=state,
        artifacts_dir=runtime / "artifacts",
    )


def test_fixture_projection_removes_secrets_and_preserves_parser_semantics():
    payload = timeline(result("original-tweet-id", username="original_handle"))
    payload["authorization"] = "Bearer original-secret"
    payload["data"]["irrelevant"] = {"tracking": "discard me"}
    projected = semantic_fixture(payload)
    original_posts, original_cursor = parse_timeline(payload)
    validate_projection(payload, projected, len(original_posts), bool(original_cursor))
    serialized = json.dumps(projected)
    assert "original content" not in serialized
    assert "original_handle" not in serialized
    assert "original-secret" not in serialized
    assert "authorization" not in projected
    assert "irrelevant" not in projected["data"]


def test_structural_hash_is_order_independent_and_structure_has_no_scalars():
    left = {"b": "private", "a": {"count": 7}}
    right = {"a": {"count": 99}, "b": "different"}
    assert structural_hash(left) == structural_hash(right)
    assert structure_tree(left) == {"a": {"count": "integer"}, "b": "string"}


class FakeProvider:
    calls = 0

    def __init__(self, settings, *, _page_observer=None, _page_limit=None):
        self.observer = _page_observer
        self._browser_version = "test-browser"

    def session_status(self):
        return {"status": "valid", "valid": True, "message": "ready"}

    def collect(
        self, request, *, cursor, cursor_context, on_batch, should_cancel
    ):
        FakeProvider.calls += 1
        operation = _expected_operation(request)
        if operation == "UserTweets" and cursor is None:
            payload = timeline(result("profile-one"), result("profile-two"), cursor="cursor-one")
        elif operation == "UserTweets":
            payload = timeline(result("profile-two"), result("profile-three"), cursor="cursor-two")
        elif operation == "UserTweetsAndReplies":
            payload = timeline(
                result("reply-one", reply=True), result("quote-one", quote=True), cursor="replies"
            )
        elif request.media_only:
            payload = timeline(result("photo-one", media=True), cursor="media")
        else:
            payload = timeline(result("search-one"), cursor="search")
        posts, next_cursor = parse_timeline(payload)
        accepted = [post for post in posts if _matches_request(post, request)]
        if self.observer:
            self.observer(
                TimelinePageObservation(
                    operation=operation,
                    page_number=1,
                    posts=posts,
                    cursor=next_cursor,
                    duration_ms=1,
                    raw_payload=payload,
                )
            )
        on_batch(accepted, next_cursor, _cursor_context(request, operation), len(posts))
        return CollectionSummary(
            last_cursor=next_cursor,
            completion_reason="page_limit_reached",
            partial=True,
        )


class FailingProvider:
    mode = "expired"
    collect_calls = 0

    def __init__(self, settings, *, _page_observer=None, _page_limit=None):
        self.observer = _page_observer

    def session_status(self):
        valid = self.mode != "expired"
        return {"status": "valid" if valid else "expired", "valid": valid}

    def collect(self, *args, **kwargs):
        FailingProvider.collect_calls += 1
        if self.mode == "rate":
            raise RateLimitedError("rate limited")
        payload = {"authorization": "secret", "data": {"unknown": "private-value"}}
        self.observer(
            TimelinePageObservation(
                operation="UserTweets",
                page_number=1,
                posts=[],
                cursor=None,
                duration_ms=1,
                raw_payload=payload,
                parse_error="unknown structure",
            )
        )
        raise SchemaDriftError("private upstream detail")


@pytest.mark.parametrize(
    ("mode", "exit_code"), [("expired", EXIT_SESSION), ("rate", EXIT_RATE_LIMIT)]
)
def test_session_and_rate_limit_exit_distinctly_without_retry(
    tmp_path, monkeypatch, mode, exit_code
):
    settings = settings_with_state(tmp_path)
    FailingProvider.mode = mode
    FailingProvider.collect_calls = 0
    monkeypatch.setattr("xscraper.smoke.PlaywrightProvider", FailingProvider)
    with pytest.raises(SmokeRunError) as raised:
        run_graphql_smoke(settings, "fixture")
    assert raised.value.exit_code == exit_code
    assert FailingProvider.collect_calls == (0 if mode == "expired" else 1)


def test_schema_drift_writes_only_a_structure_tree(tmp_path, monkeypatch):
    settings = settings_with_state(tmp_path)
    FailingProvider.mode = "schema"
    FailingProvider.collect_calls = 0
    monkeypatch.setattr("xscraper.smoke.PlaywrightProvider", FailingProvider)
    with pytest.raises(SmokeRunError) as raised:
        run_graphql_smoke(settings, "fixture")
    assert raised.value.exit_code == EXIT_SEMANTIC
    structure_path = next((raised.value.report_path.parent / "structures").glob("*.json"))
    serialized = structure_path.read_text()
    assert "secret" not in serialized
    assert "private-value" not in serialized


def test_mocked_five_payload_smoke_run_is_sanitized_and_versioned(tmp_path, monkeypatch):
    settings = settings_with_state(tmp_path)
    runtime = settings.runtime_dir
    FakeProvider.calls = 0
    monkeypatch.setattr("xscraper.smoke.PlaywrightProvider", FakeProvider)

    report_path = run_graphql_smoke(settings, "fixture")
    report = json.loads(report_path.read_text())

    assert FakeProvider.calls == 5
    assert report["schema"] == REPORT_VERSION
    assert report["status"] == "passed"
    assert [scenario["operation"] for scenario in report["scenarios"]] == [
        "UserTweets",
        "UserTweetsAndReplies",
        "SearchTimeline",
        "SearchTimeline",
    ]
    assert len(list((report_path.parent / "candidate-fixtures").glob("*.json"))) == 5
    assert not (runtime / "history.db").exists()
