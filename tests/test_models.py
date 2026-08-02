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
