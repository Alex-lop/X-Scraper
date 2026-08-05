from __future__ import annotations

import hashlib
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
from .models import CollectionRequest, CollectionSummary, Post, SourceType

PROVIDER_ID = "x_api_recent_search"
PROVIDER_VERSION = 1
COMPILER_VERSION = 1
ENDPOINT = "https://api.x.com/2/tweets/search/recent"
POST_PRICE_USD = 0.005
PRICING_AS_OF = "August 2026"
RECENT_WINDOW = timedelta(days=7)
MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 30

TWEET_FIELDS = (
    "id,text,author_id,created_at,lang,conversation_id,attachments,referenced_tweets,public_metrics"
)
EXPANSIONS = "author_id,attachments.media_keys"
USER_FIELDS = "id,username,name"
MEDIA_FIELDS = (
    "media_key,type,url,preview_image_url,alt_text,duration_ms,height,width,public_metrics,variants"
)


def account_scope(token: str) -> str:
    return hashlib.sha256(f"{PROVIDER_ID}\0{token}".encode()).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def maximum_billable_reads(max_posts: int) -> int:
    remainder = max_posts % 100
    return max_posts + (10 - remainder if 0 < remainder < 10 else 0)


def _fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def compile_request(
    request: CollectionRequest,
    token: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    # A stable 15-minute bucket makes exact requests reusable for the advertised cache window.
    bucket = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    latest_end = bucket - timedelta(seconds=10)
    # Keep the full bucket valid until its end instead of crossing X's rolling cutoff.
    earliest = bucket - RECENT_WINDOW + timedelta(minutes=15)
    if request.start_date:
        start = datetime.combine(date.fromisoformat(request.start_date), datetime_time(), UTC)
        if start < earliest:
            raise InvalidRequestError(
                "startDate must be within the recent-search seven-day window."
            )
    else:
        start = earliest
    if request.end_date:
        end = datetime.combine(
            date.fromisoformat(request.end_date) + timedelta(days=1), datetime_time(), UTC
        )
        end = min(end, latest_end)
    else:
        end = latest_end
    if end <= earliest or start >= end:
        raise InvalidRequestError("The selected dates do not overlap the recent-search window.")

    query = (
        f"from:{request.source_value}"
        if request.source_type is SourceType.PROFILE
        else request.source_value
    )
    if not request.include_replies:
        query += " -is:reply"
    if request.media_only:
        query += " has:media"
    max_reads = maximum_billable_reads(request.max_posts)
    intent = {
        "provider": PROVIDER_ID,
        "providerVersion": PROVIDER_VERSION,
        "compilerVersion": COMPILER_VERSION,
        "endpoint": ENDPOINT,
        "query": query,
        "startTime": _iso(start),
        "endTime": _iso(end),
        "sortOrder": "recency",
        "maxPosts": request.max_posts,
    }
    return {
        **intent,
        "requestFingerprint": _fingerprint(intent),
        "accountScope": account_scope(token),
        "maxBillableReads": max_reads,
        "estimatedPostReadUsd": round(max_reads * POST_PRICE_USD, 3),
        "pricePerPostUsd": POST_PRICE_USD,
        "pricingAsOf": PRICING_AS_OF,
        "compiledAt": _iso(now),
    }


def validate_compiled_request(
    request: CollectionRequest,
    compiled: dict[str, Any],
    token: str,
    *,
    now: datetime | None = None,
    require_fresh: bool = True,
) -> None:
    if not isinstance(compiled, dict):
        raise InvalidRequestError("A collection preview is required.")
    try:
        compiled_at = datetime.fromisoformat(str(compiled["compiledAt"]).replace("Z", "+00:00"))
        start = datetime.fromisoformat(str(compiled["startTime"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(compiled["endTime"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidRequestError("Collection preview is invalid.") from exc
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if require_fresh and (
        compiled_at > current + timedelta(minutes=1)
        or current - compiled_at > timedelta(minutes=15)
    ):
        raise InvalidRequestError("Collection preview expired; preview it again.")
    if require_fresh and (
        start >= end or end > current - timedelta(seconds=10) or start < current - RECENT_WINDOW
    ):
        raise InvalidRequestError("Collection preview is outside the recent-search window.")
    expected_query = (
        f"from:{request.source_value}"
        if request.source_type is SourceType.PROFILE
        else request.source_value
    )
    if not request.include_replies:
        expected_query += " -is:reply"
    if request.media_only:
        expected_query += " has:media"
    max_reads = maximum_billable_reads(request.max_posts)
    intent = {
        "provider": PROVIDER_ID,
        "providerVersion": PROVIDER_VERSION,
        "compilerVersion": COMPILER_VERSION,
        "endpoint": ENDPOINT,
        "query": expected_query,
        "startTime": _iso(start),
        "endTime": _iso(end),
        "sortOrder": "recency",
        "maxPosts": request.max_posts,
    }
    expected = {
        **intent,
        "requestFingerprint": _fingerprint(intent),
        "accountScope": account_scope(token),
        "maxBillableReads": max_reads,
        "estimatedPostReadUsd": round(max_reads * POST_PRICE_USD, 3),
        "pricePerPostUsd": POST_PRICE_USD,
        "pricingAsOf": PRICING_AS_OF,
    }
    if any(compiled.get(key) != value for key, value in expected.items()):
        raise InvalidRequestError("Collection preview does not match this request or account.")


def _integer(value: Any) -> int:
    return int(value) if isinstance(value, int) and value >= 0 else 0


def map_response(payload: Any) -> tuple[list[Post], str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        raise SchemaDriftError("X API response did not match the recent-search schema.")
    data = payload.get("data", [])
    if data is None:
        data = []
    if not isinstance(data, list):
        raise SchemaDriftError("X API response contained invalid post data.")
    includes = payload.get("includes") or {}
    if not isinstance(includes, dict):
        raise SchemaDriftError("X API response contained invalid expansions.")
    users = {
        str(user.get("id")): user
        for user in includes.get("users", [])
        if isinstance(user, dict) and user.get("id") and user.get("username")
    }
    media = {
        str(item.get("media_key")): item
        for item in includes.get("media", [])
        if isinstance(item, dict) and item.get("media_key")
    }
    posts: list[Post] = []
    for item in data:
        if (
            not isinstance(item, dict)
            or not item.get("id")
            or not isinstance(item.get("text"), str)
        ):
            raise SchemaDriftError("X API returned a post with missing required fields.")
        author = users.get(str(item.get("author_id")))
        if not author:
            raise SchemaDriftError("X API omitted a requested author expansion.")
        references = item.get("referenced_tweets") or []
        if not isinstance(references, list):
            raise SchemaDriftError("X API returned invalid post references.")
        reference_types = {
            str(reference.get("type")): str(reference.get("id"))
            for reference in references
            if isinstance(reference, dict) and reference.get("type") and reference.get("id")
        }
        media_items = []
        attachments = item.get("attachments") or {}
        for key in attachments.get("media_keys", []) if isinstance(attachments, dict) else []:
            source = media.get(str(key))
            if not source:
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
        metrics = item.get("public_metrics") or {}
        if not isinstance(metrics, dict):
            raise SchemaDriftError("X API returned invalid public metrics.")
        post_id = str(item["id"])
        username = str(author["username"])
        reply_id = reference_types.get("replied_to")
        posts.append(
            Post(
                post_id=post_id,
                text=item["text"].strip(),
                author_username=username,
                url=f"https://x.com/{username}/status/{post_id}",
                created_at=item.get("created_at"),
                language=item.get("lang"),
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
                has_media=bool(media_items),
                media=media_items,
            )
        )
    token = payload["meta"].get("next_token")
    return posts, str(token) if token else None


def _header_int(headers: Any, name: str) -> int | None:
    try:
        value = headers.get(name)
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class XApiProvider:
    def __init__(self, settings: Settings, *, opener=urlopen, sleeper=time.sleep):
        self.settings = settings
        self._opener = opener
        self._sleep = sleeper

    def _request(self, params: dict[str, str]) -> tuple[dict[str, Any], dict[str, int | None]]:
        token = self.settings.bearer_token()
        if not token:
            raise CredentialError("No X API Bearer Token. Run: xscraper configure")
        request = Request(
            f"{ENDPOINT}?{urlencode(params)}",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "xscraper/0.2"},
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
                        "X API rejected the Bearer Token or project access. Run xscraper "
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
        compiled_request: dict[str, Any],
        cursor: str | None,
        collected_count: int,
        on_batch,
        should_cancel,
    ) -> CollectionSummary:
        token = self.settings.bearer_token()
        if not token:
            raise CredentialError("No X API Bearer Token. Run: xscraper configure")
        try:
            validate_compiled_request(request, compiled_request, token, require_fresh=False)
        except InvalidRequestError as exc:
            raise ResumeIncompatibleError(str(exc)) from exc
        remaining = request.max_posts - collected_count
        current_token = cursor
        seen_tokens = {cursor} if cursor else set()
        warnings: list[str] = []
        while remaining > 0:
            if should_cancel():
                raise CollectionCancelled("Collection cancelled by the user.")
            page_size = min(100, max(10, remaining))
            params = {
                "query": compiled_request["query"],
                "start_time": compiled_request["startTime"],
                "end_time": compiled_request["endTime"],
                "sort_order": compiled_request["sortOrder"],
                "max_results": str(page_size),
                "tweet.fields": TWEET_FIELDS,
                "expansions": EXPANSIONS,
                "user.fields": USER_FIELDS,
                "media.fields": MEDIA_FIELDS,
            }
            if current_token:
                params["next_token"] = current_token
            payload, rate = self._request(params)
            posts, next_token = map_response(payload)
            batch = posts[:remaining]
            added = on_batch(batch, next_token, {**rate, "billableReads": len(posts)})
            remaining -= added
            repeated_token = bool(next_token and next_token in seen_tokens)
            current_token = next_token
            if remaining <= 0:
                return CollectionSummary(warnings=warnings, completion_reason="target_reached")
            if not next_token:
                if remaining == request.max_posts:
                    warnings.append("The collection completed without matching posts.")
                return CollectionSummary(
                    warnings=warnings, completion_reason="recent_search_exhausted"
                )
            if repeated_token:
                return CollectionSummary(
                    warnings=["Stopped because X returned a repeated pagination token."],
                    completion_reason="cursor_stalled",
                    partial=True,
                )
            seen_tokens.add(next_token)
        return CollectionSummary(completion_reason="target_reached")
