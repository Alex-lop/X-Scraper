from datetime import UTC, datetime, timedelta

import pytest

from xworkbench.errors import InvalidRequestError
from xworkbench.models import CollectionRequest, SearchMode
from xworkbench.x_api import (
    ARCHIVE_ENDPOINT,
    RECENT_ENDPOINT,
    compile_request,
    maximum_billable_reads,
    validate_compiled_request,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def collection(**changes):
    body = {"sourceType": "search", "sourceValue": "python", "maxPosts": 10}
    body.update(changes)
    return CollectionRequest.from_dict(body)


def test_request_normalization_and_recent_profile_compilation():
    request = CollectionRequest.from_dict(
        {
            "sourceType": "profile",
            "sourceValue": "https://x.com/OpenAI/status/1",
            "maxPosts": 105,
            "mediaOnly": True,
        }
    )
    compiled = compile_request(request, now=NOW)

    assert request.source_value == "OpenAI"
    assert compiled["endpoint"] == RECENT_ENDPOINT
    assert compiled["query"] == "from:OpenAI -is:reply has:media"
    assert compiled["startTime"] == "2026-07-29T12:05:00Z"
    assert compiled["endTime"] == "2026-08-05T11:59:50Z"
    assert compiled["expiresAt"] == "2026-08-05T12:05:00Z"
    assert compiled["maximumPostResources"] == 110
    assert compiled["expansions"] == "attachments.media_keys"
    assert compiled["userFields"] is None


def test_search_is_nfc_normalized_and_request_fields_are_allowlisted():
    request = collection(sourceValue="  cafe\u0301   OR  Tea  ")
    assert request.source_value == "café   OR  Tea"

    with pytest.raises(InvalidRequestError, match="Unknown request field"):
        collection(forceRefresh=True)
    for value in (9, 501, "10", True):
        with pytest.raises(InvalidRequestError, match="maxPosts"):
            collection(maxPosts=value)


def test_or_queries_are_grouped_before_application_filters():
    assert (
        compile_request(collection(sourceValue="cats OR dogs", mediaOnly=True), now=NOW)["query"]
        == "(cats OR dogs) -is:reply has:media"
    )
    assert (
        compile_request(
            collection(sourceValue="cats OR dogs", includeReplies=True), now=NOW
        )["query"]
        == "cats OR dogs"
    )


def test_recent_boundary_is_clamped_and_older_date_is_rejected():
    assert (
        compile_request(collection(startDate="2026-07-29"), now=NOW)["startTime"]
        == "2026-07-29T12:05:00Z"
    )
    with pytest.raises(InvalidRequestError, match="seven-day"):
        compile_request(collection(startDate="2026-07-28"), now=NOW)


def test_full_archive_requires_dates_and_uses_inclusive_end():
    with pytest.raises(InvalidRequestError, match="requires startDate and endDate"):
        collection(searchMode="fullArchive")

    archive = collection(
        searchMode="fullArchive",
        startDate="2020-01-01",
        endDate="2020-01-02",
        maxPosts=105,
    )
    compiled = compile_request(archive, now=NOW)
    assert compiled["endpoint"] == ARCHIVE_ENDPOINT
    assert compiled["startTime"] == "2020-01-01T00:00:00Z"
    assert compiled["endTime"] == "2020-01-03T00:00:00Z"
    assert compiled["maximumPostResources"] == 105


def test_final_compiled_query_limits_include_automatic_filters():
    assert compile_request(collection(sourceValue="x" * 500), now=NOW)["queryLength"] <= 512
    with pytest.raises(InvalidRequestError, match="512"):
        compile_request(collection(sourceValue="x" * 503), now=NOW)

    archive = collection(
        sourceValue="x" * 1_010,
        searchMode="fullArchive",
        startDate="2020-01-01",
        endDate="2020-01-02",
    )
    assert compile_request(archive, now=NOW)["queryLength"] <= 1_024
    with pytest.raises(InvalidRequestError, match="1,024"):
        compile_request(
            collection(
                sourceValue="x" * 1_024,
                searchMode="fullArchive",
                startDate="2020-01-01",
                endDate="2020-01-02",
            ),
            now=NOW,
        )


def test_preview_is_valid_for_five_minutes_and_rejects_tampering():
    request = collection()
    compiled = compile_request(request, now=NOW)
    validate_compiled_request(request, compiled, now=NOW + timedelta(minutes=5))

    with pytest.raises(InvalidRequestError, match="expired"):
        validate_compiled_request(request, compiled, now=NOW + timedelta(minutes=5, seconds=1))
    with pytest.raises(InvalidRequestError, match="does not match"):
        validate_compiled_request(request, {**compiled, "query": "different"}, now=NOW)


def test_recent_overfetch_does_not_apply_to_full_archive():
    assert [maximum_billable_reads(value) for value in (10, 100, 101, 109, 110)] == [
        10,
        100,
        110,
        110,
        110,
    ]
    assert maximum_billable_reads(105, SearchMode.FULL_ARCHIVE) == 105
