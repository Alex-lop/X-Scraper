from dataclasses import FrozenInstanceError

import pytest

from xworkbench.errors import InvalidRequestError
from xworkbench.models import (
    BROWSER_QUERY_MAX_LENGTH,
    CollectionRequest,
    SourceDefinition,
    request_fingerprint,
    source_fingerprint,
)


def browser(surface: str, value: str, max_posts: int = 5) -> CollectionRequest:
    return CollectionRequest.from_dict(
        {
            "provider": "playwright_browser",
            "sourceType": surface,
            "sourceValue": value,
            "maxPosts": max_posts,
        }
    )


def test_browser_profile_and_search_values_are_normalized_and_bounded():
    profile = browser("profile", " https://WWW.X.COM/OpenAI/ ")
    search = browser("search", "  cafe\u0301\n   OR\tTea  ")

    assert profile.source_value == "openai"
    assert search.source_value == "café OR Tea"
    assert profile.max_posts == search.max_posts == 5
    assert browser("profile", "@OpenAI", 25).max_posts == 25

    for value in (0, 26, True):
        with pytest.raises(InvalidRequestError, match="maxPosts"):
            CollectionRequest.from_dict(
                {
                    "provider": "playwright_browser",
                    "sourceType": "profile",
                    "sourceValue": "openai",
                    "maxPosts": value,
                }
            )
    with pytest.raises(InvalidRequestError, match="cannot exceed"):
        browser("search", "x" * (BROWSER_QUERY_MAX_LENGTH + 1))


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/OpenAI",
        "http://x.com/OpenAI",
        "https://user@x.com/OpenAI",
        "https://x.com:443/OpenAI",
        "https://x.com/OpenAI/status/1",
        "https://x.com/OpenAI?ref=home",
        "https://x.com/search",
        "@home",
    ],
)
def test_browser_profile_rejects_non_profile_destinations(value):
    with pytest.raises(InvalidRequestError, match="profile"):
        browser("profile", value)


@pytest.mark.parametrize(
    "value",
    ["https://x.com/search?q=python", "https://example.com/topic", "//x.com/search?q=python"],
)
def test_browser_search_rejects_urls(value):
    with pytest.raises(InvalidRequestError, match="query, not a URL"):
        browser("search", value)


def test_browser_rejects_api_controls_but_official_api_semantics_are_unchanged():
    with pytest.raises(InvalidRequestError, match="does not support"):
        CollectionRequest.from_dict(
            {
                "provider": "playwright_browser",
                "sourceType": "search",
                "sourceValue": "python",
                "startDate": "2026-01-01",
            }
        )

    official = CollectionRequest.from_dict(
        {
            "provider": "official_x_api",
            "sourceType": "profile",
            "sourceValue": "https://x.com/OpenAI/status/1",
            "maxPosts": 10,
            "startDate": "2026-01-01",
        }
    )
    assert official.source_value == "OpenAI"
    assert official.start_date == "2026-01-01"


def test_versioned_canonical_fingerprints_use_normalized_values_and_exact_budgets():
    url = browser("profile", "https://x.com/OpenAI")
    handle = browser("profile", " @openai ")
    bigger = browser("profile", "openai", 6)
    search = browser("search", "openai")
    official = CollectionRequest.from_dict(
        {
            "provider": "official_x_api",
            "sourceType": "profile",
            "sourceValue": "OpenAI",
            "maxPosts": 10,
        }
    )

    assert request_fingerprint(url) == request_fingerprint(handle)
    assert source_fingerprint(url) == source_fingerprint(handle)
    assert source_fingerprint(url) == source_fingerprint(bigger)
    assert request_fingerprint(url) != request_fingerprint(bigger)
    assert source_fingerprint(url) != source_fingerprint(search)
    assert source_fingerprint(url) != source_fingerprint(official)
    assert request_fingerprint(url).startswith("v1:")
    assert len(request_fingerprint(url).removeprefix("v1:")) == 64

    composed = browser("search", "café topic")
    decomposed = browser("search", "  cafe\u0301\t topic  ")
    assert request_fingerprint(composed) == request_fingerprint(decomposed)


def test_source_definition_is_normalized_credential_free_and_immutable():
    source = SourceDefinition.from_dict(
        {
            "id": "research.openai",
            "displayName": "  OpenAI\nPosts  ",
            "provider": "playwright_browser",
            "surface": "profile",
            "value": "https://x.com/OpenAI",
            "createdAt": "2026-08-19T08:00:00-04:00",
            "lastStatus": "succeeded",
        }
    )

    assert source.to_dict() == {
        "id": "research.openai",
        "displayName": "OpenAI Posts",
        "provider": "playwright_browser",
        "surface": "profile",
        "value": "openai",
        "createdAt": "2026-08-19T12:00:00+00:00",
        "lastStatus": "succeeded",
    }
    assert source_fingerprint(source) == source_fingerprint(browser("profile", "openai"))
    with pytest.raises(FrozenInstanceError):
        source.display_name = "changed"

    for extra in ({"authToken": "secret"}, {"unknown": True}):
        with pytest.raises(InvalidRequestError, match="Unknown source field"):
            SourceDefinition.from_dict({**source.to_dict(), **extra})
