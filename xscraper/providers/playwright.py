from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

from playwright.sync_api import BrowserContext, Page, Response, sync_playwright

from ..config import Settings
from ..errors import (
    CollectionCancelled,
    CollectionTimeoutError,
    ProfileUnavailableError,
    RateLimitedError,
    ResumeIncompatibleError,
    SchemaDriftError,
    ScraperError,
    SessionExpiredError,
    SessionMissingError,
)
from ..models import CollectionRequest, CollectionSummary, SourceType, Tweet
from .base import BatchCallback, CancelCallback

TIMELINE_OPERATIONS = ("SearchTimeline", "UserTweets", "UserTweetsAndReplies")
PROVIDER_ID = "x_web_playwright"
CURSOR_CONTEXT_VERSION = 1
QUERY_COMPILER_VERSION = 1
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TimelinePageObservation:
    operation: str
    page_number: int
    posts: list[Tweet]
    cursor: str | None
    duration_ms: int
    raw_payload: Any
    parse_error: str | None = None


def _walk(value: Any) -> Iterator[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield None, child
            yield from _walk(child)


def _unwrap_tweet_result(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("tweet"), dict):
        result = result["tweet"]
    if result.get("__typename") in {"TweetTombstone", "TweetUnavailable"}:
        return None
    return result if isinstance(result.get("legacy"), dict) else None


def _get_nested(value: dict[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _parse_created_at(raw: Any) -> str | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _parse_media(legacy: dict[str, Any]) -> list[dict[str, Any]]:
    media_items = _get_nested(legacy, "extended_entities", "media")
    if not isinstance(media_items, list):
        media_items = _get_nested(legacy, "entities", "media")
    if not isinstance(media_items, list):
        return []
    result = []
    for media in media_items:
        if not isinstance(media, dict):
            continue
        result.append(
            {
                "id": str(media.get("id_str") or media.get("id") or ""),
                "type": media.get("type"),
                "url": media.get("media_url_https") or media.get("media_url"),
                "expandedUrl": media.get("expanded_url"),
            }
        )
    return result


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_tweet_result(result: Any) -> Tweet | None:
    result = _unwrap_tweet_result(result)
    if result is None:
        return None
    legacy = result["legacy"]
    tweet_id = str(result.get("rest_id") or legacy.get("id_str") or "")
    if not tweet_id:
        return None

    text = legacy.get("full_text")
    note_text = _get_nested(result, "note_tweet", "note_tweet_results", "result", "text")
    if note_text:
        text = note_text
    if not isinstance(text, str) or not text.strip():
        return None

    user = _get_nested(result, "core", "user_results", "result") or {}
    username = _get_nested(user, "legacy", "screen_name") or _get_nested(
        user, "core", "screen_name"
    )
    if not username:
        username = legacy.get("screen_name")
    if not username:
        return None
    username = str(username)
    media = _parse_media(legacy)
    in_reply_to = legacy.get("in_reply_to_status_id_str")

    return Tweet(
        tweet_id=tweet_id,
        text=text.strip(),
        author_username=username,
        url=f"https://x.com/{username}/status/{tweet_id}",
        created_at=_parse_created_at(legacy.get("created_at")),
        language=legacy.get("lang"),
        conversation_id=str(legacy.get("conversation_id_str") or "") or None,
        in_reply_to_tweet_id=str(in_reply_to) if in_reply_to else None,
        like_count=_integer(legacy.get("favorite_count")),
        reply_count=_integer(legacy.get("reply_count")),
        retweet_count=_integer(legacy.get("retweet_count")),
        quote_count=_integer(legacy.get("quote_count")),
        bookmark_count=_integer(legacy.get("bookmark_count")),
        is_reply=bool(in_reply_to),
        is_retweet=bool(
            result.get("retweeted_status_result")
            or legacy.get("retweeted_status_result")
            or text.startswith("RT @")
        ),
        is_quote=bool(legacy.get("is_quote_status") or result.get("quoted_status_result")),
        has_media=bool(media),
        media=media,
        raw=result,
    )


def parse_timeline(payload: Any) -> tuple[list[Tweet], str | None]:
    if not isinstance(payload, dict):
        raise SchemaDriftError("Timeline response was not a JSON object.")

    graphql_errors = payload.get("errors")
    if isinstance(graphql_errors, list) and graphql_errors:
        messages = []
        codes: set[str] = set()
        for error in graphql_errors:
            if not isinstance(error, dict):
                continue
            message = str(error.get("message") or "Unknown GraphQL error")
            messages.append(message[:200])
            code = error.get("code") or _get_nested(error, "extensions", "code")
            if code is not None:
                codes.add(str(code).lower())
        combined = " ".join(messages).lower()
        if codes.intersection({"88", "429", "rate_limited"}) or "rate limit" in combined:
            raise RateLimitedError("X rate-limited the timeline request.")
        if codes.intersection({"32", "89", "215", "326", "unauthorized"}):
            raise SessionExpiredError("X rejected the saved browser session.")
        detail = "; ".join(messages) or "Unknown GraphQL error"
        raise SchemaDriftError(f"X returned GraphQL errors: {detail}")

    instruction_lists = [
        value
        for key, value in _walk(payload)
        if key == "instructions" and isinstance(value, list)
    ]
    if not instruction_lists:
        raise SchemaDriftError("Timeline response did not contain timeline instructions.")

    tweets: list[Tweet] = []
    seen: set[str] = set()
    bottom_cursors: dict[str, str] = {}
    entry_index = 0
    recognized_instruction = False
    known_instruction_types = {
        "TimelineAddEntries",
        "TimelineReplaceEntry",
        "TimelinePinEntry",
        "TimelineTerminateTimeline",
        "TimelineClearCache",
    }
    for instructions in instruction_lists:
        for instruction in instructions:
            if not isinstance(instruction, dict):
                continue
            if instruction.get("type") in known_instruction_types:
                recognized_instruction = True
            entries = instruction.get("entries")
            if not isinstance(entries, list):
                entry = instruction.get("entry")
                entries = [entry] if isinstance(entry, dict) else []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_id = str(entry.get("entryId") or f"entry-{entry_index}")
                entry_index += 1
                content = entry.get("content")
                if not isinstance(content, dict):
                    continue
                promoted = entry_id.startswith("promoted-") or any(
                    key == "promotedMetadata" for key, _ in _walk(content)
                )
                cursor_values = [content, *(value for _, value in _walk(content))]
                for value in cursor_values:
                    if isinstance(value, dict) and value.get("cursorType") == "Bottom":
                        candidate = value.get("value")
                        if isinstance(candidate, str) and candidate:
                            bottom_cursors[entry_id] = candidate
                if promoted:
                    continue
                for key, value in _walk(content):
                    if key != "tweet_results" or not isinstance(value, dict):
                        continue
                    tweet = parse_tweet_result(value.get("result"))
                    if tweet and tweet.tweet_id not in seen:
                        seen.add(tweet.tweet_id)
                        tweets.append(tweet)
                    break

    if not recognized_instruction:
        raise SchemaDriftError("Timeline response contained no recognized instructions.")

    distinct_cursors = list(dict.fromkeys(bottom_cursors.values()))
    if len(distinct_cursors) > 1:
        raise SchemaDriftError("Timeline response contained multiple bottom cursors.")
    return tweets, distinct_cursors[0] if distinct_cursors else None


def _matches_request(tweet: Tweet, request: CollectionRequest) -> bool:
    if not request.include_replies and tweet.is_reply:
        return False
    if request.media_only and not tweet.has_media:
        return False
    if (request.start_date or request.end_date) and not tweet.created_at:
        return False
    if tweet.created_at:
        created = datetime.fromisoformat(tweet.created_at).date()
        if request.start_date and created < date.fromisoformat(request.start_date):
            return False
        if request.end_date and created > date.fromisoformat(request.end_date):
            return False
    return True


def _build_search_query(request: CollectionRequest) -> str:
    parts = [request.source_value]
    if request.start_date:
        parts.append(f"since:{request.start_date}")
    if request.end_date:
        exclusive_end = date.fromisoformat(request.end_date) + timedelta(days=1)
        parts.append(f"until:{exclusive_end.isoformat()}")
    if not request.include_replies:
        parts.append("-filter:replies")
    if request.media_only:
        parts.append("filter:media")
    return " ".join(parts)


def _replace_cursor(url: str, cursor: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    variables_raw = query.get("variables", ["{}"])[0]
    variables = json.loads(variables_raw)
    variables["cursor"] = cursor
    query["variables"] = [json.dumps(variables, separators=(",", ":"))]
    encoded = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=encoded))


def _operation_from_url(url: str) -> str | None:
    operation = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return operation if operation in TIMELINE_OPERATIONS else None


def _expected_operation(request: CollectionRequest) -> str:
    if request.source_type is SourceType.SEARCH:
        return "SearchTimeline"
    return "UserTweetsAndReplies" if request.include_replies else "UserTweets"


def _cursor_context(request: CollectionRequest, operation: str) -> dict[str, Any]:
    return {
        "provider": PROVIDER_ID,
        "version": CURSOR_CONTEXT_VERSION,
        "operation": operation,
        "requestFingerprint": request.fingerprint(include_limit=False, include_sentiment=False),
        "sort": "live",
    }


def _validate_cursor_context(
    request: CollectionRequest,
    operation: str,
    cursor: str | None,
    cursor_context: dict[str, Any] | None,
) -> dict[str, Any]:
    expected = _cursor_context(request, operation)
    if cursor and cursor_context != expected:
        raise ResumeIncompatibleError(
            "Saved cursor does not match the current request and provider operation. "
            "Start a new collection instead of resuming this job."
        )
    return expected


class PlaywrightProvider:
    def __init__(
        self,
        settings: Settings,
        *,
        _page_observer: Callable[[TimelinePageObservation], None] | None = None,
        _page_limit: int | None = None,
    ):
        self.settings = settings
        self._page_observer = _page_observer
        self._page_limit = _page_limit
        self._browser_version: str | None = None

    def session_status(self) -> dict[str, str | bool]:
        path = self.settings.storage_state_path
        if not path.exists():
            return {"status": "missing", "valid": False, "message": "Run: python -m xscraper auth"}
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(storage_state=str(path))
                page = context.new_page()
                page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30_000)
                expired = (
                    "/i/flow/login" in page.url or page.locator('input[name="text"]').count() > 0
                )
                authenticated = (
                    page.locator('[data-testid="primaryColumn"]').count() > 0
                    or page.locator('a[data-testid="AppTabBar_Home_Link"]').count() > 0
                )
                browser.close()
            if expired:
                return {
                    "status": "expired",
                    "valid": False,
                    "message": "Saved X session has expired.",
                }
            if not authenticated:
                return {
                    "status": "unavailable",
                    "valid": False,
                    "message": "X loaded, but an authenticated timeline could not be confirmed.",
                }
            return {"status": "valid", "valid": True, "message": "Saved X session is ready."}
        except Exception as exc:
            logger.warning("Saved session validation was unavailable: %s", exc)
            return {
                "status": "unavailable",
                "valid": False,
                "message": "Could not validate the saved X session. Check local logs.",
            }

    def _require_session(self) -> None:
        if not self.settings.storage_state_path.exists():
            raise SessionMissingError("No saved X session. Run: python -m xscraper auth")

    @staticmethod
    def _target_url(request: CollectionRequest) -> str:
        if request.source_type is SourceType.SEARCH:
            query = quote(_build_search_query(request), safe="")
            return f"https://x.com/search?q={query}&src=typed_query&f=live"
        suffix = "/with_replies" if request.include_replies else ""
        return f"https://x.com/{request.source_value}{suffix}"

    @staticmethod
    def _page_failure(page: Page) -> ScraperError | None:
        if "/i/flow/login" in page.url or page.locator('input[name="text"]').count() > 0:
            return SessionExpiredError("The saved X session expired; authenticate again.")
        text = page.locator("body").inner_text(timeout=3_000).lower()
        if "rate limit exceeded" in text or "try again later" in text:
            return RateLimitedError("X rate-limited this collection. Resume it later.")
        if "this account doesn’t exist" in text or "account suspended" in text:
            return ProfileUnavailableError("The requested profile is unavailable.")
        return None

    @staticmethod
    def _captured_response(response: Response) -> dict[str, Any] | None:
        operation = _operation_from_url(response.url)
        if not operation:
            return None
        if response.status == 429:
            raise RateLimitedError("X rate-limited the timeline request.")
        if response.status in {401, 403}:
            raise SessionExpiredError("X rejected the saved browser session.")
        if response.status >= 400:
            return None
        try:
            payload = response.json()
            headers = response.request.all_headers()
        except Exception:
            return None
        return {
            "payload": payload,
            "url": response.url,
            "headers": headers,
            "operation": operation,
        }

    @staticmethod
    def _request_page(context: BrowserContext, capture: dict[str, Any], cursor: str) -> Any:
        url = _replace_cursor(capture["url"], cursor)
        excluded = {"cookie", "content-length", "host", "accept-encoding"}
        headers = {
            key: value for key, value in capture["headers"].items() if key.lower() not in excluded
        }
        response = context.request.get(url, headers=headers, timeout=60_000)
        if response.status == 429:
            raise RateLimitedError("X rate-limited the timeline request.")
        if response.status in {401, 403}:
            raise SessionExpiredError("X rejected the saved browser session.")
        if not response.ok:
            raise ScraperError(
                f"Timeline request failed with HTTP {response.status}.",
                retryable=response.status >= 500,
            )
        return response.json()

    def _write_diagnostics(self, page: Page, job_hint: str, error: Exception) -> str:
        self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        stem = f"{job_hint}-{stamp}"
        screenshot_path = self.settings.artifacts_dir / f"{stem}.png"
        try:
            page.screenshot(
                path=str(screenshot_path),
                full_page=False,
            )
            screenshot_path.chmod(0o600)
        except Exception:
            pass
        summary = {
            "timestamp": stamp,
            "pageUrl": page.url.split("?")[0],
            "errorType": type(error).__name__,
            "message": str(error),
        }
        summary_path = self.settings.artifacts_dir / f"{stem}.json"
        try:
            summary_path.write_text(json.dumps(summary, indent=2))
            summary_path.chmod(0o600)
        except OSError:
            pass
        return stem

    def collect(
        self,
        request: CollectionRequest,
        *,
        cursor: str | None,
        cursor_context: dict[str, Any] | None,
        on_batch: BatchCallback,
        should_cancel: CancelCallback,
    ) -> CollectionSummary:
        self._require_session()
        operation = _expected_operation(request)
        expected_cursor_context = _validate_cursor_context(
            request, operation, cursor, cursor_context
        )
        started = time.monotonic()
        warnings: list[str] = []
        seen: set[str] = set()
        seen_cursors: set[str] = {cursor} if cursor else set()
        collected = 0
        no_progress_pages = 0
        current_cursor = cursor
        completion_reason = "timeline_exhausted"
        partial = False
        page_number = 0

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.settings.headless)
            self._browser_version = browser.version
            context = browser.new_context(storage_state=str(self.settings.storage_state_path))
            if self.settings.enable_tracing:
                context.tracing.start(screenshots=True, snapshots=True, sources=False)
            trace_stopped = False
            page = context.new_page()
            page.set_default_timeout(self.settings.page_timeout_ms)
            captures: list[dict[str, Any]] = []
            capture_errors: list[Exception] = []

            def on_response(response: Response) -> None:
                if _operation_from_url(response.url) != operation:
                    return
                try:
                    captured = self._captured_response(response)
                    if captured:
                        captures.append(captured)
                except Exception as exc:
                    capture_errors.append(exc)

            page.on("response", on_response)
            try:
                page.goto(
                    self._target_url(request),
                    wait_until="domcontentloaded",
                    timeout=self.settings.page_timeout_ms,
                )
                page.wait_for_timeout(4_000)
                if capture_errors:
                    raise capture_errors[0]
                failure = self._page_failure(page)
                if failure:
                    raise failure
                matching_captures = [
                    capture for capture in captures if capture["operation"] == operation
                ]
                if not matching_captures:
                    page.mouse.wheel(0, 1_500)
                    page.wait_for_timeout(3_000)
                if capture_errors:
                    raise capture_errors[0]
                matching_captures = [
                    capture for capture in captures if capture["operation"] == operation
                ]
                if not matching_captures:
                    raise SchemaDriftError(
                        f"No {operation} response was captured. X may have changed its operations."
                    )

                capture = matching_captures[-1]
                initial_payload = capture["payload"]
                if current_cursor:
                    page_started = time.monotonic()
                    payload = self._request_page(context, capture, current_cursor)
                else:
                    payload = initial_payload
                    page_started = started

                while True:
                    if should_cancel():
                        raise CollectionCancelled("Collection cancelled by the user.")
                    if time.monotonic() - started > self.settings.job_timeout_seconds:
                        raise CollectionTimeoutError(
                            "Collection exceeded its configured deadline; resume to continue."
                        )

                    page_number += 1
                    try:
                        parsed, next_cursor = parse_timeline(payload)
                    except SchemaDriftError as exc:
                        if self._page_observer:
                            self._page_observer(
                                TimelinePageObservation(
                                    operation=operation,
                                    page_number=page_number,
                                    posts=[],
                                    cursor=None,
                                    duration_ms=round(
                                        (time.monotonic() - page_started) * 1_000
                                    ),
                                    raw_payload=payload,
                                    parse_error=str(exc),
                                )
                            )
                        raise
                    if self._page_observer:
                        self._page_observer(
                            TimelinePageObservation(
                                operation=operation,
                                page_number=page_number,
                                posts=parsed,
                                cursor=next_cursor,
                                duration_ms=round((time.monotonic() - page_started) * 1_000),
                                raw_payload=payload,
                            )
                        )
                    dated_posts = [
                        datetime.fromisoformat(tweet.created_at).date()
                        for tweet in parsed
                        if tweet.created_at
                    ]
                    batch: list[Tweet] = []
                    new_raw_posts = 0
                    for tweet in parsed:
                        if tweet.tweet_id in seen:
                            continue
                        seen.add(tweet.tweet_id)
                        new_raw_posts += 1
                        if not _matches_request(tweet, request):
                            continue
                        batch.append(tweet)
                        if collected + len(batch) >= request.max_tweets:
                            break

                    accepted = on_batch(
                        batch,
                        next_cursor,
                        expected_cursor_context,
                        len(parsed),
                    )
                    collected += accepted
                    if new_raw_posts == 0 or (batch and accepted == 0):
                        no_progress_pages += 1
                    else:
                        no_progress_pages = 0

                    current_cursor = next_cursor
                    if collected >= request.max_tweets:
                        completion_reason = "target_reached"
                        break
                    if request.start_date and dated_posts:
                        start_boundary = date.fromisoformat(request.start_date)
                        if max(dated_posts) < start_boundary:
                            completion_reason = "date_boundary_reached"
                            break
                    if not next_cursor:
                        completion_reason = "timeline_exhausted"
                        break
                    if next_cursor in seen_cursors:
                        completion_reason = "cursor_stalled"
                        partial = True
                        warnings.append("Stopped because X returned a repeated pagination cursor.")
                        break
                    seen_cursors.add(next_cursor)
                    if no_progress_pages >= self.settings.no_new_page_limit:
                        completion_reason = "no_progress"
                        partial = True
                        warnings.append(
                            f"Stopped after {no_progress_pages} pages produced no new unique posts."
                        )
                        break
                    if self._page_limit is not None and page_number >= self._page_limit:
                        completion_reason = "page_limit_reached"
                        partial = True
                        break

                    last_error: Exception | None = None
                    page_started = time.monotonic()
                    for attempt in range(self.settings.max_retries):
                        try:
                            payload = self._request_page(context, capture, next_cursor)
                            last_error = None
                            break
                        except (RateLimitedError, SessionExpiredError):
                            raise
                        except Exception as exc:
                            last_error = exc
                            time.sleep(min(2**attempt + random.random(), 5))
                    if last_error:
                        raise ScraperError(
                            f"Timeline pagination failed after retries: {last_error}",
                            retryable=True,
                        )

                if collected == 0:
                    warnings.append(
                        "The collection completed without any posts matching the filters."
                    )
                return CollectionSummary(
                    warnings=warnings,
                    last_cursor=current_cursor,
                    completion_reason=completion_reason,
                    partial=partial,
                )
            except Exception as exc:
                stem = self._write_diagnostics(page, request.source_type.value, exc)
                if self.settings.enable_tracing:
                    try:
                        trace_path = self.settings.artifacts_dir / f"{stem}.zip"
                        context.tracing.stop(path=str(trace_path))
                        trace_path.chmod(0o600)
                        trace_stopped = True
                    except Exception:
                        pass
                raise
            finally:
                if self.settings.enable_tracing and not trace_stopped:
                    try:
                        context.tracing.stop()
                    except Exception:
                        pass
                browser.close()


def authenticate_interactively(settings: Settings) -> Path:
    settings.ensure_runtime_dirs()
    temporary_state = settings.storage_state_path.with_name(
        f".{settings.storage_state_path.name}.tmp"
    )
    temporary_state.unlink(missing_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")
            print("Log into the dedicated X account in the opened browser.")
            input("After the home timeline is visible, press Enter here to save the session: ")
            authenticated = (
                page.locator('[data-testid="primaryColumn"]').count() > 0
                or page.locator('a[data-testid="AppTabBar_Home_Link"]').count() > 0
            )
            if "/i/flow/login" in page.url or not authenticated:
                browser.close()
                raise SessionExpiredError(
                    "An authenticated X timeline was not confirmed; session was not saved."
                )
            context.storage_state(path=str(temporary_state))
            temporary_state.chmod(0o600)
            temporary_state.replace(settings.storage_state_path)
            browser.close()
    finally:
        temporary_state.unlink(missing_ok=True)
    return settings.storage_state_path
