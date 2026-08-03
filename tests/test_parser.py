import json
from copy import deepcopy
from pathlib import Path

import pytest

from xscraper.errors import RateLimitedError, ResumeIncompatibleError, SchemaDriftError
from xscraper.models import CollectionRequest
from xscraper.providers.playwright import (
    _build_search_query,
    _cursor_context,
    _matches_request,
    _operation_from_url,
    _replace_cursor,
    _validate_cursor_context,
    parse_timeline,
)

FIXTURE = Path(__file__).parent / "fixtures" / "timeline.json"


def test_parses_structured_timeline_and_cursor():
    tweets, cursor = parse_timeline(json.loads(FIXTURE.read_text()))
    assert cursor == "cursor-next"
    assert [tweet.tweet_id for tweet in tweets] == ["123", "456"]
    assert tweets[0].author_username == "example"
    assert tweets[0].like_count == 11
    assert tweets[0].has_media is True
    assert tweets[1].text == "A long reply from a note tweet"
    assert tweets[1].is_reply is True


def test_filters_and_search_query_are_consistent():
    tweets, _ = parse_timeline(json.loads(FIXTURE.read_text()))
    request = CollectionRequest.from_dict(
        {
            "sourceType": "search",
            "sourceValue": "python",
            "startDate": "2026-06-02",
            "endDate": "2026-06-02",
            "mediaOnly": True,
        }
    )
    assert _matches_request(tweets[0], request)
    assert not _matches_request(tweets[1], request)
    query = _build_search_query(request)
    assert "since:2026-06-02" in query
    assert "until:2026-06-03" in query
    assert "filter:media" in query
    assert "-filter:replies" in query


def test_replaces_graphql_cursor_without_losing_variables():
    url = "https://x.com/i/api/graphql/id/SearchTimeline?variables=%7B%22count%22%3A20%7D&features=%7B%7D"
    changed = _replace_cursor(url, "next cursor")
    assert "%22cursor%22" in changed
    assert "next+cursor" in changed or "next%20cursor" in changed
    assert "features=" in changed


def test_rejects_graphql_errors_and_unknown_structures():
    with pytest.raises(RateLimitedError):
        parse_timeline({"errors": [{"code": 88, "message": "Rate limit exceeded"}]})
    with pytest.raises(SchemaDriftError):
        parse_timeline({"data": {"unexpected": []}})


def test_skips_promoted_timeline_entries():
    payload = json.loads(FIXTURE.read_text())
    promoted = deepcopy(
        payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"][0][
            "entries"
        ][0]
    )
    promoted["entryId"] = "promoted-tweet-999"
    promoted["content"]["promotedMetadata"] = {"advertiser": "example"}
    entries = payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"][0][
        "entries"
    ]
    entries.insert(0, promoted)
    tweets, cursor = parse_timeline(payload)
    assert [tweet.tweet_id for tweet in tweets] == ["123", "456"]
    assert cursor == "cursor-next"


def test_operation_detection_requires_exact_path_component():
    assert (
        _operation_from_url("https://x.com/i/api/graphql/id/SearchTimeline?variables=%7B%7D")
        == "SearchTimeline"
    )
    assert _operation_from_url("https://x.com/example/SearchTimeline-preview") is None


def test_resume_cursor_is_bound_to_request_and_operation():
    request = CollectionRequest.from_dict(
        {"sourceType": "search", "sourceValue": "python", "maxTweets": 10}
    )
    context = _cursor_context(request, "SearchTimeline")
    assert _validate_cursor_context(request, "SearchTimeline", "cursor", context) == context

    changed = CollectionRequest.from_dict(
        {"sourceType": "search", "sourceValue": "rust", "maxTweets": 10}
    )
    with pytest.raises(ResumeIncompatibleError):
        _validate_cursor_context(changed, "SearchTimeline", "cursor", context)
    with pytest.raises(ResumeIncompatibleError):
        _validate_cursor_context(request, "SearchTimeline", "legacy-cursor", None)


def test_quote_payload_is_not_emitted_as_a_second_timeline_post():
    payload = json.loads(FIXTURE.read_text())
    outer = payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"][0][
        "entries"
    ][0]["content"]["itemContent"]["tweet_results"]["result"]
    quoted = deepcopy(outer)
    quoted["rest_id"] = "nested-quote"
    outer["legacy"]["is_quote_status"] = True
    outer["quoted_status_result"] = {"result": quoted}

    tweets, _ = parse_timeline(payload)

    assert [tweet.tweet_id for tweet in tweets] == ["123", "456"]
    assert tweets[0].is_quote is True
