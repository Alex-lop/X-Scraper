from datetime import UTC, datetime

import pytest

from xscraper.errors import InvalidRequestError
from xscraper.models import CollectionRequest
from xscraper.x_api import compile_request, maximum_billable_reads


def test_request_normalization_and_official_query_compilation():
    request = CollectionRequest.from_dict(
        {
            "sourceType": "profile",
            "sourceValue": "https://x.com/OpenAI/status/1",
            "maxPosts": 105,
            "mediaOnly": True,
        }
    )
    compiled = compile_request(request, "token", now=datetime(2026, 8, 3, 16, 7, tzinfo=UTC))
    assert request.source_value == "OpenAI"
    assert compiled["query"] == "from:OpenAI -is:reply has:media"
    assert compiled["maxBillableReads"] == 110
    assert compiled["estimatedPostReadUsd"] == 0.55


def test_search_is_only_trimmed_and_nfc_normalized():
    request = CollectionRequest.from_dict(
        {"sourceType": "search", "sourceValue": "  cafe\u0301   OR  Tea  ", "maxPosts": 10}
    )
    assert request.source_value == "café   OR  Tea"
    assert (
        compile_request(request, "token", now=datetime(2026, 8, 3, 16, 7, tzinfo=UTC))["query"]
        == "café   OR  Tea -is:reply"
    )


@pytest.mark.parametrize("value", [9, 501, "10", True])
def test_max_posts_is_10_to_500(value):
    with pytest.raises(InvalidRequestError):
        CollectionRequest.from_dict(
            {"sourceType": "search", "sourceValue": "python", "maxPosts": value}
        )


def test_recent_window_and_overfetch_math():
    request = CollectionRequest.from_dict(
        {
            "sourceType": "search",
            "sourceValue": "python",
            "maxPosts": 10,
            "startDate": "2026-07-01",
        }
    )
    with pytest.raises(InvalidRequestError, match="seven-day"):
        compile_request(request, "token", now=datetime(2026, 8, 3, 16, tzinfo=UTC))
    assert [maximum_billable_reads(value) for value in (10, 100, 101, 109, 110)] == [
        10,
        100,
        110,
        110,
        110,
    ]
