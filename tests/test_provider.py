import io
import json
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError

import pytest

from xscraper.config import Settings
from xscraper.errors import (
    BillingError,
    CredentialError,
    NetworkError,
    RateLimitWaiting,
    SchemaDriftError,
)
from xscraper.models import CollectionRequest
from xscraper.x_api import XApiProvider, compile_request, map_response


def payload(next_token="next"):
    return {
        "data": [
            {
                "id": "42",
                "text": "hello",
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
            "media": [{"media_key": "3_1", "type": "photo", "url": "https://img.invalid/a.jpg"}],
        },
        "meta": {"result_count": 1, "next_token": next_token},
    }


def settings(tmp_path):
    token = tmp_path / "auth" / "token"
    token.parent.mkdir()
    token.write_text("secret")
    return Settings(tmp_path / "db.sqlite", token)


def test_maps_metrics_references_author_and_media():
    posts, cursor = map_response(payload())
    post = posts[0]
    assert cursor == "next"
    assert post.post_id == "42" and post.repost_count == 3
    assert post.author_username == "tester"
    assert post.in_reply_to_post_id == "41"
    assert post.is_reply and post.is_quote and post.has_media
    assert post.like_count == 4 and post.bookmark_count == 5
    assert post.media[0]["url"].endswith("a.jpg")


def test_schema_mismatch_fails_safely():
    with pytest.raises(SchemaDriftError):
        map_response({"data": []})


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


@pytest.mark.parametrize(
    ("status", "detail", "error"),
    [
        (401, "unauthorized", CredentialError),
        (403, "credits exhausted", BillingError),
        (402, "pay", BillingError),
        (429, "usage cap exceeded", BillingError),
    ],
)
def test_terminal_http_classification(tmp_path, status, detail, error):
    def opener(request, timeout):
        raise HTTPError(request.full_url, status, detail, {}, io.BytesIO(detail.encode()))

    provider = XApiProvider(settings(tmp_path), opener=opener, sleeper=lambda _: None)
    with pytest.raises(error):
        provider._request({"query": "python"})


def test_429_persists_reset_instead_of_sleeping(tmp_path):
    sleeps = []

    def opener(request, timeout):
        raise HTTPError(
            request.full_url,
            429,
            "limited",
            {"x-rate-limit-reset": "1785772860", "x-rate-limit-remaining": "0"},
            io.BytesIO(b"{}"),
        )

    provider = XApiProvider(settings(tmp_path), opener=opener, sleeper=sleeps.append)
    with pytest.raises(RateLimitWaiting) as raised:
        provider._request({"query": "python"})
    assert raised.value.reset == 1785772860
    assert raised.value.remaining == 0
    assert sleeps == []


def test_network_failure_gets_three_short_retries(tmp_path):
    calls = []

    def opener(request, timeout):
        calls.append(1)
        raise URLError("offline")

    provider = XApiProvider(settings(tmp_path), opener=opener, sleeper=lambda _: None)
    with pytest.raises(NetworkError):
        provider._request({"query": "python"})
    assert len(calls) == 4


def test_pagination_uses_unmodified_token_and_final_minimum_ten(tmp_path):
    requests = []
    bodies = [
        {**payload("opaque-token"), "data": payload()["data"] * 100},
        {**payload(None), "data": payload(None)["data"] * 5},
    ]

    def opener(request, timeout):
        requests.append(request.full_url)
        return Response(bodies.pop(0))

    request = CollectionRequest.from_dict(
        {"sourceType": "search", "sourceValue": "python", "maxPosts": 105}
    )
    compiled = compile_request(request, "secret", now=datetime.now(UTC))
    stored = []
    XApiProvider(settings(tmp_path), opener=opener).collect(
        request,
        compiled_request=compiled,
        cursor=None,
        collected_count=0,
        on_batch=lambda batch, cursor, stats: stored.extend(batch) or len(batch),
        should_cancel=lambda: False,
    )
    assert "max_results=100" in requests[0]
    assert "next_token=opaque-token" in requests[1] and "max_results=10" in requests[1]
    assert len(stored) == 105
