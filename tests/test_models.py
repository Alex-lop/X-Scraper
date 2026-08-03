import pytest

from xscraper.errors import InvalidRequestError
from xscraper.models import CollectionRequest


def test_profile_request_normalizes_url():
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "https://x.com/OpenAI/status/1", "maxTweets": 50}
    )
    assert request.source_value == "OpenAI"
    assert request.max_tweets == 50


@pytest.mark.parametrize("value", [0, 501, "not-a-number"])
def test_rejects_invalid_maximum(value):
    with pytest.raises(InvalidRequestError):
        CollectionRequest.from_dict(
            {"sourceType": "search", "sourceValue": "python", "maxTweets": value}
        )


def test_rejects_reversed_dates():
    with pytest.raises(InvalidRequestError):
        CollectionRequest.from_dict(
            {
                "sourceType": "search",
                "sourceValue": "python",
                "startDate": "2026-05-02",
                "endDate": "2026-05-01",
            }
        )


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_rejects_non_boolean_filter_values(value):
    with pytest.raises(InvalidRequestError):
        CollectionRequest.from_dict(
            {"sourceType": "search", "sourceValue": "python", "mediaOnly": value}
        )


def test_rejects_non_object_request():
    with pytest.raises(InvalidRequestError):
        CollectionRequest.from_dict(["not", "an", "object"])


def test_cursor_fingerprint_ignores_limit_and_sentiment():
    first = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "OpenAI", "maxTweets": 10}
    )
    second = CollectionRequest.from_dict(
        {
            "sourceType": "profile",
            "sourceValue": "openai",
            "maxTweets": 50,
            "analyzeSentiment": True,
        }
    )
    assert first.fingerprint() != second.fingerprint()
    assert first.fingerprint(include_limit=False, include_sentiment=False) == second.fingerprint(
        include_limit=False, include_sentiment=False
    )


def test_search_expression_only_trims_edges_and_normalizes_unicode():
    request = CollectionRequest.from_dict(
        {"sourceType": "search", "sourceValue": "  cafe\u0301   OR  Tea  "}
    )
    assert request.source_value == "café   OR  Tea"
