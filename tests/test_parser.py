import json
from pathlib import Path

from xscraper.models import CollectionRequest
from xscraper.providers.playwright import (
    _build_search_query,
    _matches_request,
    _replace_cursor,
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
