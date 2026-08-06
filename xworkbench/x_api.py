from __future__ import annotations

import json
import random
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings
from .errors import (
    BillingError,
    CollectionCancelled,
    CredentialError,
    InvalidRequestError,
    NetworkError,
    RateLimitWaiting,
    ResumeIncompatibleError,
    SchemaDriftError,
)
from .models import (
    CollectionRequest,
    CollectionSummary,
    Post,
    ProviderType,
    SearchMode,
    SourceType,
)

PROVIDER_ID = ProviderType.OFFICIAL_X_API.value
LEGACY_PROVIDER_ID = "x_api_search"
PROVIDER_VERSION = 2
COMPILER_VERSION = 2
RECENT_ENDPOINT = "https://api.x.com/2/tweets/search/recent"
ARCHIVE_ENDPOINT = "https://api.x.com/2/tweets/search/all"
POST_PRICE_USD = 0.005
USER_PRICE_USD = 0.010
MEDIA_PRICE_USD = 0.005
UNIT_PRICES_USD = {
    "post": POST_PRICE_USD,
    "user": USER_PRICE_USD,
    "media": MEDIA_PRICE_USD,
}
PRICING_AS_OF = "August 2026"
RECENT_WINDOW = timedelta(days=7)
PREVIEW_TTL = timedelta(minutes=5)
END_TIME_SAFETY = timedelta(seconds=10)
MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 30

TWEET_FIELDS = (
    "id,text,note_tweet,author_id,created_at,lang,conversation_id,attachments,"
    "referenced_tweets,public_metrics"
)
SEARCH_EXPANSIONS = "author_id,attachments.media_keys"
PROFILE_EXPANSIONS = "attachments.media_keys"
USER_FIELDS = "id,username,name"
MEDIA_FIELDS = (
    "media_key,type,url,preview_image_url,alt_text,duration_ms,height,width,public_metrics,variants"
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def maximum_billable_reads(max_posts: int, search_mode: SearchMode = SearchMode.RECENT) -> int:
    if search_mode is SearchMode.FULL_ARCHIVE:
        return max_posts
    remainder = max_posts % 100
    return max_posts + (10 - remainder if 0 < remainder < 10 else 0)


def _query(request: CollectionRequest) -> str:
    source = (
        f"from:{request.source_value}"
        if request.source_type is SourceType.PROFILE
        else request.source_value
    )
    filters = []
    if not request.include_replies:
        filters.append("-is:reply")
    if request.media_only:
        filters.append("has:media")
    if not filters:
        return source
    # User-authored operators must remain one expression before app filters are applied.
    if request.source_type is SourceType.SEARCH:
        source = f"({source})"
    return " ".join((source, *filters))


def returned_list_price(
    resources: dict[str, int], unit_prices: dict[str, float] | None = None
) -> float:
    prices = unit_prices or UNIT_PRICES_USD
    return round(
        resources.get("posts", 0) * prices["post"]
        + resources.get("users", 0) * prices["user"]
        + resources.get("media", 0) * prices["media"],
        3,
    )


def compile_request(
    request: CollectionRequest,
    _token: str | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if (
        request.provider is not ProviderType.OFFICIAL_X_API
        or request.source_type not in {SourceType.PROFILE, SourceType.SEARCH}
        or request.search_mode is None
    ):
        raise InvalidRequestError("official_x_api supports only profile or search requests.")
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    latest_end = now - END_TIME_SAFETY
    if request.search_mode is SearchMode.FULL_ARCHIVE:
        if not request.start_date or not request.end_date:
            raise InvalidRequestError("Full-archive search requires startDate and endDate.")
        start = datetime.combine(date.fromisoformat(request.start_date), datetime_time(), UTC)
        end = min(
            datetime.combine(
                date.fromisoformat(request.end_date) + timedelta(days=1), datetime_time(), UTC
            ),
            latest_end,
        )
    else:
        # The five-minute margin keeps the exact preview valid for its advertised lifetime.
        earliest = now - RECENT_WINDOW + PREVIEW_TTL
        if request.start_date:
            requested_start = datetime.combine(
                date.fromisoformat(request.start_date), datetime_time(), UTC
            )
            if requested_start < earliest:
                if requested_start.date() != earliest.date():
                    raise InvalidRequestError(
                        "startDate must be within the recent-search seven-day window."
                    )
                start = earliest
            else:
                start = requested_start
        else:
            start = earliest
        if request.end_date:
            end = min(
                datetime.combine(
                    date.fromisoformat(request.end_date) + timedelta(days=1),
                    datetime_time(),
                    UTC,
                ),
                latest_end,
            )
        else:
            end = latest_end
    if start >= end:
        raise InvalidRequestError("The selected dates do not overlap the searchable window.")

    query = _query(request)
    query_limit = 512 if request.search_mode is SearchMode.RECENT else 1_024
    if len(query) > query_limit:
        raise InvalidRequestError(
            f"Compiled query cannot exceed {query_limit:,} characters in "
            f"{request.search_mode.value} mode."
        )
    max_reads = maximum_billable_reads(request.max_posts, request.search_mode)
    endpoint = RECENT_ENDPOINT if request.search_mode is SearchMode.RECENT else ARCHIVE_ENDPOINT
    expansions = (
        PROFILE_EXPANSIONS if request.source_type is SourceType.PROFILE else SEARCH_EXPANSIONS
    )
    intent = {
        "provider": PROVIDER_ID,
        "providerVersion": PROVIDER_VERSION,
        "compilerVersion": COMPILER_VERSION,
        "searchMode": request.search_mode.value,
        "endpoint": endpoint,
        "query": query,
        "queryLength": len(query),
        "startTime": _iso(start),
        "endTime": _iso(end),
        "sortOrder": "recency",
        "maxPosts": request.max_posts,
        "tweetFields": TWEET_FIELDS,
        "expansions": expansions,
        "userFields": USER_FIELDS if request.source_type is SourceType.SEARCH else None,
        "mediaFields": MEDIA_FIELDS,
    }
    return {
        **intent,
        "maximumPostResources": max_reads,
        "maximumPostListPriceUsd": round(max_reads * POST_PRICE_USD, 3),
        "unitPricesUsd": UNIT_PRICES_USD,
        "pricingAsOf": PRICING_AS_OF,
        "compiledAt": _iso(now),
        "expiresAt": _iso(now + PREVIEW_TTL),
    }


def validate_compiled_request(
    request: CollectionRequest,
    compiled: dict[str, Any],
    _token: str | None = None,
    *,
    now: datetime | None = None,
    require_fresh: bool = True,
) -> None:
    if not isinstance(compiled, dict):
        raise InvalidRequestError("A collection preview is required.")
    try:
        compiled_at = datetime.fromisoformat(str(compiled["compiledAt"]).replace("Z", "+00:00"))
        expires_at = datetime.fromisoformat(str(compiled["expiresAt"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidRequestError("Collection preview is invalid.") from exc
    if compiled_at.tzinfo is None or expires_at.tzinfo is None:
        raise InvalidRequestError("Collection preview is invalid.")
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    if require_fresh and (
        compiled_at > current or current > expires_at or expires_at != compiled_at + PREVIEW_TTL
    ):
        raise InvalidRequestError("Collection preview expired; preview it again.")
    try:
        expected = compile_request(request, now=compiled_at)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError("Collection preview is invalid.") from exc
    plan_provider = compiled.get("provider")
    if plan_provider == LEGACY_PROVIDER_ID:
        expected["provider"] = LEGACY_PROVIDER_ID
    elif plan_provider is None:
        expected.pop("provider")
    if compiled != expected:
        raise InvalidRequestError("Collection preview does not match this request.")


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _warning_text(error: Any) -> str:
    if not isinstance(error, dict):
        return "X API reported an unspecified partial-response error."
    title = " ".join(str(error.get("title") or "").split())
    detail = " ".join(str(error.get("detail") or "").split())
    message = ": ".join(part for part in (title, detail) if part)
    return (
        f"X API partial response: {message[:400]}"
        if message
        else ("X API reported an unspecified partial-response error.")
    )


def map_response(
    payload: Any, *, fallback_username: str | None = None
) -> tuple[list[Post], str | None, list[str], dict[str, int]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        raise SchemaDriftError("X API response did not match the search schema.")
    data = payload.get("data", [])
    if data is None:
        data = []
    if not isinstance(data, list):
        raise SchemaDriftError("X API response contained invalid post data.")
    includes = payload.get("includes") or {}
    if not isinstance(includes, dict):
        raise SchemaDriftError("X API response contained invalid expansions.")
    user_resources = includes.get("users", [])
    media_resources = includes.get("media", [])
    errors = payload.get("errors", [])
    warnings: list[str] = []
    if not isinstance(user_resources, list):
        warnings.append("X API returned an invalid user expansion; usernames are unavailable.")
        user_resources = []
    if not isinstance(media_resources, list):
        warnings.append("X API returned an invalid media expansion; media details are unavailable.")
        media_resources = []
    if not isinstance(errors, list):
        warnings.append("X API returned an invalid partial-error list.")
        errors = []
    users = {
        str(user.get("id")): user
        for user in user_resources
        if isinstance(user, dict) and user.get("id")
    }
    media = {
        str(item.get("media_key")): item
        for item in media_resources
        if isinstance(item, dict) and item.get("media_key")
    }
    warnings.extend(_warning_text(error) for error in errors)
    posts: list[Post] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict) or not item.get("id"):
            warnings.append(f"Skipped malformed Post resource at response index {index}.")
            continue
        post_id = str(item["id"])
        note_tweet = item.get("note_tweet")
        note_text = note_tweet.get("text") if isinstance(note_tweet, dict) else None
        text = note_text if isinstance(note_text, str) else item.get("text")
        if not isinstance(text, str):
            warnings.append(f"Skipped Post {post_id} because it was malformed: text was missing.")
            continue
        if note_tweet is not None and not isinstance(note_tweet, dict):
            warnings.append(f"Post {post_id} had malformed long-form text; used its fallback text.")
        author_id = str(item.get("author_id") or "") or None
        author = users.get(author_id or "")
        username_value = author.get("username") if author else None
        username = (
            str(username_value) if isinstance(username_value, str) and username_value else None
        ) or fallback_username
        if author_id and not author and fallback_username is None:
            warnings.append(
                f"X API omitted the author expansion for Post {item['id']}; "
                "the username is unavailable."
            )
        references = item.get("referenced_tweets") or []
        if not isinstance(references, list):
            warnings.append(f"Post {post_id} had malformed references; they were omitted.")
            references = []
        reference_types = {
            str(reference.get("type")): str(reference.get("id"))
            for reference in references
            if isinstance(reference, dict) and reference.get("type") and reference.get("id")
        }
        media_items = []
        attachments = item.get("attachments") or {}
        if not isinstance(attachments, dict):
            warnings.append(f"Post {post_id} had malformed attachments; they were omitted.")
            attachments = {}
        media_keys = attachments.get("media_keys", [])
        if not isinstance(media_keys, list):
            warnings.append(f"Post {post_id} had malformed media keys; they were omitted.")
            media_keys = []
        for key in media_keys:
            source = media.get(str(key))
            if not source:
                warnings.append(f"X API omitted media expansion {key} for Post {item['id']}.")
                continue
            media_items.append(
                {
                    "id": str(key),
                    "type": source.get("type"),
                    "url": source.get("url") or source.get("preview_image_url"),
                    "previewImageUrl": source.get("preview_image_url"),
                    "altText": source.get("alt_text"),
                    "width": source.get("width"),
                    "height": source.get("height"),
                    "durationMs": source.get("duration_ms"),
                    "variants": source.get("variants") or [],
                }
            )
        metrics = item.get("public_metrics")
        if metrics is None:
            metrics = {}
        elif not isinstance(metrics, dict):
            warnings.append(f"Post {post_id} had malformed public metrics; they are unavailable.")
            metrics = {}
        reply_id = reference_types.get("replied_to")
        posts.append(
            Post(
                post_id=post_id,
                text=text,
                author_username=username,
                author_id=author_id,
                url=(
                    f"https://x.com/{username}/status/{post_id}"
                    if username
                    else f"https://x.com/i/web/status/{post_id}"
                ),
                created_at=(
                    item.get("created_at") if isinstance(item.get("created_at"), str) else None
                ),
                language=item.get("lang") if isinstance(item.get("lang"), str) else None,
                conversation_id=str(item.get("conversation_id") or "") or None,
                in_reply_to_post_id=reply_id,
                like_count=_integer(metrics.get("like_count")),
                reply_count=_integer(metrics.get("reply_count")),
                repost_count=_integer(metrics.get("retweet_count")),
                quote_count=_integer(metrics.get("quote_count")),
                bookmark_count=_integer(metrics.get("bookmark_count")),
                is_reply=bool(reply_id),
                is_repost=bool({"retweeted", "reposted"}.intersection(reference_types)),
                is_quote="quoted" in reference_types,
                has_media=bool(media_keys),
                media=media_items,
            )
        )
    token = payload["meta"].get("next_token")
    resources = {
        "posts": len(data),
        "users": len(user_resources),
        "media": len(media_resources),
    }
    return posts, str(token) if token else None, list(dict.fromkeys(warnings)), resources


def _header_int(headers: Any, name: str) -> int | None:
    try:
        value = headers.get(name)
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class XApiProvider:
    provider_id = ProviderType.OFFICIAL_X_API
    provider_version = PROVIDER_VERSION

    def __init__(self, settings: Settings, *, opener=urlopen, sleeper=time.sleep):
        self.settings = settings
        self._opener = opener
        self._sleep = sleeper

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            "sources": [SourceType.PROFILE.value, SourceType.SEARCH.value],
            "limits": {"minimum": 10, "default": 25, "maximum": 500},
            "searchModes": [SearchMode.RECENT.value, SearchMode.FULL_ARCHIVE.value],
            "paidReads": True,
            "confirmation": {"field": "confirmPaidRead", "kind": "paid_read"},
        }

    def connection_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            **self.settings.connection_status(),
        }

    def prepare(
        self,
        request: CollectionRequest,
        supplied_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self.settings.bearer_token()
        if not token:
            raise CredentialError("No X API Bearer Token. Run: xworkbench configure")
        if supplied_plan is None:
            return compile_request(request, token)
        validate_compiled_request(request, supplied_plan, token)
        return supplied_plan

    def _request(
        self, params: dict[str, str], *, endpoint: str = RECENT_ENDPOINT
    ) -> tuple[dict[str, Any], dict[str, int | None]]:
        token = self.settings.bearer_token()
        if not token:
            raise CredentialError("No X API Bearer Token. Run: xworkbench configure")
        request = Request(
            f"{endpoint}?{urlencode(params)}",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "xworkbench/0.2"},
        )
        for attempt in range(MAX_RETRIES + 1):
            try:
                with self._opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    headers = response.headers
                return payload, {
                    "rateLimitRemaining": _header_int(headers, "x-rate-limit-remaining"),
                    "rateLimitReset": _header_int(headers, "x-rate-limit-reset"),
                }
            except HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")[:2_000]
                except OSError:
                    detail = ""
                lowered = detail.casefold()
                remaining = _header_int(exc.headers, "x-rate-limit-remaining")
                reset = _header_int(exc.headers, "x-rate-limit-reset")
                credit_failure = any(
                    word in lowered for word in ("credit", "billing", "usage cap", "quota")
                )
                if exc.code in {402, 403, 429} and credit_failure:
                    raise BillingError(
                        "X API credits are exhausted. Check the X Developer Console billing page."
                    ) from exc
                if exc.code == 429:
                    reset = reset or int(datetime.now(UTC).timestamp()) + 60
                    retry_at = datetime.fromtimestamp(reset, UTC) + timedelta(
                        seconds=random.uniform(0.5, 2.0)
                    )
                    raise RateLimitWaiting(
                        "X API rate limit reached; collection will resume automatically.",
                        _iso(retry_at),
                        remaining,
                        reset,
                    ) from exc
                if exc.code == 402:
                    raise BillingError(
                        "X API credits are exhausted. Check the X Developer Console billing page."
                    ) from exc
                if exc.code in {401, 403}:
                    raise CredentialError(
                        "X API rejected the Bearer Token or project access. Run xworkbench "
                        "configure and check the X Developer Console."
                    ) from exc
                if exc.code < 500 or attempt >= MAX_RETRIES:
                    raise NetworkError(
                        f"X API request failed with HTTP {exc.code}.", retryable=exc.code >= 500
                    ) from exc
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= MAX_RETRIES:
                    raise NetworkError("X API request failed after three retries.") from exc
            self._sleep(min(0.25 * (2**attempt), 1.0))
        raise NetworkError("X API request failed after three retries.")

    def collect(
        self,
        request: CollectionRequest,
        *,
        execution_plan: dict[str, Any],
        checkpoint: dict[str, Any],
        on_batch,
        should_cancel,
    ) -> CollectionSummary:
        token = self.settings.bearer_token()
        if not token:
            raise CredentialError("No X API Bearer Token. Run: xworkbench configure")
        try:
            validate_compiled_request(request, execution_plan, token, require_fresh=False)
        except InvalidRequestError as exc:
            raise ResumeIncompatibleError(str(exc)) from exc
        try:
            collected_count = int(checkpoint["storedCount"])
            current_token = checkpoint.get("providerState")
            metadata = checkpoint.get("metadata") or {}
            resources_returned = metadata.get("resourcesReturned") or {}
            returned_post_count = int(resources_returned.get("posts") or 0)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ResumeIncompatibleError("Saved official X API checkpoint is invalid.") from exc
        if current_token is not None and not isinstance(current_token, str):
            raise ResumeIncompatibleError("Saved official X API checkpoint is invalid.")
        stored_remaining = request.max_posts - collected_count
        resource_remaining = int(execution_plan["maximumPostResources"]) - returned_post_count
        if stored_remaining <= 0:
            return CollectionSummary(completion_reason="target_reached")
        if resource_remaining <= 0:
            return CollectionSummary(
                warnings=["Stopped at the confirmed Post-resource limit."],
                completion_reason="post_resource_limit_reached",
                partial=True,
            )
        seen_tokens = {current_token} if current_token else set()
        warnings: list[str] = []
        exhausted_reason = (
            "recent_search_exhausted"
            if request.search_mode is SearchMode.RECENT
            else "full_archive_exhausted"
        )
        while stored_remaining > 0 and resource_remaining > 0:
            if should_cancel():
                raise CollectionCancelled("Collection cancelled by the user.")
            page_limit = 100 if request.search_mode is SearchMode.RECENT else 500
            page_size = min(page_limit, resource_remaining, max(10, stored_remaining))
            if page_size < 10:
                warnings.append("Stopped before a request that would exceed the confirmed limit.")
                return CollectionSummary(
                    warnings=warnings,
                    completion_reason="post_resource_limit_reached",
                    partial=True,
                )
            params = {
                "query": execution_plan["query"],
                "start_time": execution_plan["startTime"],
                "end_time": execution_plan["endTime"],
                "sort_order": execution_plan["sortOrder"],
                "max_results": str(page_size),
                "tweet.fields": execution_plan["tweetFields"],
                "expansions": execution_plan["expansions"],
                "media.fields": execution_plan["mediaFields"],
            }
            if execution_plan["userFields"]:
                params["user.fields"] = execution_plan["userFields"]
            if current_token:
                params["next_token"] = current_token
            payload, rate = self._request(params, endpoint=execution_plan["endpoint"])
            posts, next_token, page_warnings, resources = map_response(
                payload,
                fallback_username=(
                    request.source_value if request.source_type is SourceType.PROFILE else None
                ),
            )
            warnings.extend(page_warnings)
            returned_posts = int(resources.get("posts") or 0)
            if returned_posts > page_size:
                warnings.append(
                    "X returned more Post resources than requested; collection stopped."
                )
            batch = posts[:stored_remaining]
            added = on_batch(
                batch,
                next_token,
                {**rate, "resourcesReturned": resources, "warnings": page_warnings},
            )
            stored_remaining -= added
            resource_remaining -= returned_posts
            repeated_token = bool(next_token and next_token in seen_tokens)
            current_token = next_token
            if stored_remaining <= 0:
                return CollectionSummary(warnings=warnings, completion_reason="target_reached")
            if resource_remaining <= 0:
                warnings.append("Stopped at the confirmed Post-resource limit.")
                return CollectionSummary(
                    warnings=list(dict.fromkeys(warnings)),
                    completion_reason="post_resource_limit_reached",
                    partial=True,
                )
            if not next_token:
                if collected_count == 0 and stored_remaining == request.max_posts:
                    warnings.append("The collection completed without matching posts.")
                return CollectionSummary(warnings=warnings, completion_reason=exhausted_reason)
            if repeated_token:
                return CollectionSummary(
                    warnings=list(
                        dict.fromkeys(
                            [*warnings, "Stopped because X returned a repeated pagination token."]
                        )
                    ),
                    completion_reason="cursor_stalled",
                    partial=True,
                )
            seen_tokens.add(next_token)
        return CollectionSummary(
            warnings=list(dict.fromkeys(warnings)),
            completion_reason="post_resource_limit_reached",
            partial=True,
        )
