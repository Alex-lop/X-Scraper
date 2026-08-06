import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from xworkbench.config import Settings
from xworkbench.errors import SchemaDriftError
from xworkbench.models import CollectionRequest
from xworkbench.x_api import ARCHIVE_ENDPOINT, XApiProvider, compile_request, map_response


def payload(*, next_token="next"):
    return {
        "data": [
            {
                "id": "42",
                "text": "  exact text\n",
                "author_id": "7",
                "created_at": "2026-08-03T12:00:00Z",
                "lang": "en",
                "conversation_id": "40",
                "referenced_tweets": [
                    {"type": "replied_to", "id": "41"},
                    {"type": "quoted", "id": "39"},
                ],
                "attachments": {"media_keys": ["3_1"]},
                "public_metrics": {
                    "like_count": 4,
                    "reply_count": 2,
                    "retweet_count": 3,
                    "quote_count": 1,
                    "bookmark_count": 5,
                },
            }
        ],
        "includes": {
            "users": [{"id": "7", "username": "tester"}],
            "media": [
                {"media_key": "3_1", "type": "photo", "url": "https://img.invalid/a.jpg"}
            ],
        },
        "meta": {"result_count": 1, "next_token": next_token},
    }


def settings(tmp_path):
    token = tmp_path / "auth" / "token"
    token.parent.mkdir()
    token.write_text("secret")
    return Settings(tmp_path / "db.sqlite", token)


class Response:
    def __init__(self, body, headers=None):
        self.body = json.dumps(body).encode()
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.body


def test_maps_exact_text_metrics_references_media_and_resource_counts():
    posts, cursor, warnings, resources = map_response(payload())
    post = posts[0]

    assert cursor == "next" and warnings == []
    assert resources == {"posts": 1, "users": 1, "media": 1}
    assert post.post_id == "42" and post.author_id == "7"
    assert post.text == "  exact text\n" and post.author_username == "tester"
    assert post.in_reply_to_post_id == "41"
    assert post.is_reply and post.is_quote and post.has_media
    assert post.repost_count == 3 and post.bookmark_count == 5
    assert post.media[0]["url"].endswith("a.jpg")


def test_note_tweet_replaces_truncated_text_without_stripping():
    response = payload(next_token=None)
    response["data"][0]["text"] = "Truncated…"
    response["data"][0]["note_tweet"] = {"text": "  complete long-form evidence\n"}

    posts, _, warnings, _ = map_response(response)

    assert posts[0].text == "  complete long-form evidence\n"
    assert warnings == []


def test_partial_errors_and_missing_author_preserve_valid_post():
    response = payload(next_token=None)
    response["includes"].pop("users")
    response["errors"] = [
        {"title": "Resource unavailable", "detail": "  Author expansion failed.  "}
    ]

    posts, _, warnings, resources = map_response(response)

    assert len(posts) == 1 and posts[0].author_id == "7"
    assert posts[0].author_username is None
    assert posts[0].url == "https://x.com/i/web/status/42"
    assert resources == {"posts": 1, "users": 0, "media": 1}
    assert any("Resource unavailable: Author expansion failed." in warning for warning in warnings)
    assert any("author" in warning.casefold() for warning in warnings)


def test_profile_fallback_does_not_require_author_expansion():
    response = payload(next_token=None)
    response["includes"].pop("users")

    posts, _, warnings, _ = map_response(response, fallback_username="tester")

    assert posts[0].author_username == "tester"
    assert posts[0].url == "https://x.com/tester/status/42"
    assert not any("omitted" in warning for warning in warnings)


def test_malformed_post_does_not_discard_valid_sibling():
    response = payload(next_token=None)
    response["data"].insert(0, {"id": "broken"})

    posts, _, warnings, _ = map_response(response)

    assert [post.post_id for post in posts] == ["42"]
    assert any(
        "malformed" in warning and ("broken" in warning or "index 0" in warning)
        for warning in warnings
    )


def test_invalid_top_level_schema_fails_safely():
    with pytest.raises(SchemaDriftError):
        map_response({"data": []})


def test_profile_collection_uses_fallback_and_reports_page_resources(tmp_path):
    response = payload(next_token=None)
    response["includes"].pop("users")
    requested = []
    page_stats = []

    def opener(request, timeout):
        requested.append(request.full_url)
        return Response(response, {"x-rate-limit-remaining": "44"})

    collection = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    stored = []
    summary = XApiProvider(settings(tmp_path), opener=opener).collect(
        collection,
        compiled_request=compile_request(collection, now=datetime.now(UTC)),
        cursor=None,
        collected_count=0,
        on_batch=lambda batch, cursor, stats: (
            stored.extend(batch) or page_stats.append(stats) or len(batch)
        ),
        should_cancel=lambda: False,
    )

    params = parse_qs(urlsplit(requested[0]).query)
    assert params["expansions"] == ["attachments.media_keys"]
    assert "user.fields" not in params
    assert stored[0].author_username == "tester"
    assert page_stats[0]["resourcesReturned"] == {"posts": 1, "users": 0, "media": 1}
    assert page_stats[0]["rateLimitRemaining"] == 44
    assert summary.completion_reason == "recent_search_exhausted"


def test_full_archive_uses_archive_endpoint_and_single_large_page(tmp_path):
    requested = []

    def opener(request, timeout):
        requested.append(request.full_url)
        return Response(payload(next_token=None))

    collection = CollectionRequest.from_dict(
        {
            "sourceType": "search",
            "sourceValue": "python",
            "searchMode": "fullArchive",
            "maxPosts": 105,
            "startDate": "2020-01-01",
            "endDate": "2020-01-02",
        }
    )
    XApiProvider(settings(tmp_path), opener=opener).collect(
        collection,
        compiled_request=compile_request(collection),
        cursor=None,
        collected_count=0,
        on_batch=lambda batch, *_args: len(batch),
        should_cancel=lambda: False,
    )

    parsed = urlsplit(requested[0])
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == ARCHIVE_ENDPOINT
    assert parse_qs(parsed.query)["max_results"] == ["105"]
