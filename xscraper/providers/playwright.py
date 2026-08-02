from __future__ import annotations

import json
import random
import time
from collections.abc import Iterator
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
    SchemaDriftError,
    ScraperError,
    SessionExpiredError,
    SessionMissingError,
)
from ..models import CollectionRequest, CollectionSummary, SourceType, Tweet
from .base import BatchCallback, CancelCallback

TIMELINE_OPERATIONS = ("SearchTimeline", "UserTweets", "UserTweetsAndReplies")


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
        username = legacy.get("screen_name") or "unknown"
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
        like_count=int(legacy.get("favorite_count") or 0),
        reply_count=int(legacy.get("reply_count") or 0),
        retweet_count=int(legacy.get("retweet_count") or 0),
        quote_count=int(legacy.get("quote_count") or 0),
        bookmark_count=int(legacy.get("bookmark_count") or 0),
        is_reply=bool(in_reply_to),
        is_retweet=bool(legacy.get("retweeted_status_result") or text.startswith("RT @")),
        is_quote=bool(legacy.get("is_quote_status") or result.get("quoted_status_result")),
        has_media=bool(media),
        media=media,
        raw=result,
    )


def parse_timeline(payload: Any) -> tuple[list[Tweet], str | None]:
    tweets: list[Tweet] = []
    seen: set[str] = set()
    cursor: str | None = None
    for key, value in _walk(payload):
        if key == "tweet_results" and isinstance(value, dict):
            tweet = parse_tweet_result(value.get("result"))
            if tweet and tweet.tweet_id not in seen:
                seen.add(tweet.tweet_id)
                tweets.append(tweet)
        if isinstance(value, dict) and value.get("cursorType") == "Bottom":
            candidate = value.get("value")
            if isinstance(candidate, str) and candidate:
                cursor = candidate
    return tweets, cursor


def _matches_request(tweet: Tweet, request: CollectionRequest) -> bool:
    if not request.include_replies and tweet.is_reply:
        return False
    if request.media_only and not tweet.has_media:
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


class PlaywrightProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

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
                browser.close()
            if expired:
                return {
                    "status": "expired",
                    "valid": False,
                    "message": "Saved X session has expired.",
                }
            return {"status": "valid", "valid": True, "message": "Saved X session is ready."}
        except Exception as exc:
            return {"status": "unavailable", "valid": False, "message": str(exc)}

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
        if not any(operation in response.url for operation in TIMELINE_OPERATIONS):
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
        return {"payload": payload, "url": response.url, "headers": headers}

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
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        stem = f"{job_hint}-{stamp}"
        try:
            page.screenshot(
                path=str(self.settings.artifacts_dir / f"{stem}.png"),
                full_page=False,
            )
        except Exception:
            pass
        summary = {
            "timestamp": stamp,
            "pageUrl": page.url.split("?")[0],
            "errorType": type(error).__name__,
            "message": str(error),
        }
        try:
            (self.settings.artifacts_dir / f"{stem}.json").write_text(json.dumps(summary, indent=2))
        except OSError:
            pass
        return stem

    def collect(
        self,
        request: CollectionRequest,
        *,
        cursor: str | None,
        on_batch: BatchCallback,
        should_cancel: CancelCallback,
    ) -> CollectionSummary:
        self._require_session()
        started = time.monotonic()
        warnings: list[str] = []
        seen: set[str] = set()
        collected = 0
        no_new_pages = 0
        current_cursor = cursor

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.settings.headless)
            context = browser.new_context(storage_state=str(self.settings.storage_state_path))
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
            trace_stopped = False
            page = context.new_page()
            page.set_default_timeout(self.settings.page_timeout_ms)
            captures: list[dict[str, Any]] = []
            capture_errors: list[Exception] = []

            def on_response(response: Response) -> None:
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
                if not captures:
                    page.mouse.wheel(0, 1_500)
                    page.wait_for_timeout(3_000)
                if capture_errors:
                    raise capture_errors[0]
                if not captures:
                    raise SchemaDriftError(
                        "No structured timeline response was captured. "
                        "X may have changed its operations."
                    )

                capture = captures[-1]
                initial_payload = capture["payload"]
                if current_cursor:
                    payload = self._request_page(context, capture, current_cursor)
                else:
                    payload = initial_payload

                while True:
                    if should_cancel():
                        raise CollectionCancelled("Collection cancelled by the user.")
                    if time.monotonic() - started > self.settings.job_timeout_seconds:
                        raise CollectionTimeoutError(
                            "Collection exceeded its ten-minute deadline; resume to continue."
                        )

                    parsed, next_cursor = parse_timeline(payload)
                    batch: list[Tweet] = []
                    for tweet in parsed:
                        if tweet.tweet_id in seen or not _matches_request(tweet, request):
                            continue
                        seen.add(tweet.tweet_id)
                        batch.append(tweet)
                        if collected + len(batch) >= request.max_tweets:
                            break

                    if batch:
                        on_batch(batch, next_cursor)
                        collected += len(batch)
                        no_new_pages = 0
                    else:
                        on_batch([], next_cursor)
                        no_new_pages += 1

                    current_cursor = next_cursor
                    if collected >= request.max_tweets or not next_cursor:
                        break
                    if no_new_pages >= self.settings.no_new_page_limit:
                        warnings.append(
                            f"Stopped after {no_new_pages} pages produced no matching new posts."
                        )
                        break

                    last_error: Exception | None = None
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
                return CollectionSummary(warnings=warnings, last_cursor=current_cursor)
            except Exception as exc:
                stem = self._write_diagnostics(page, request.source_type.value, exc)
                try:
                    context.tracing.stop(path=str(self.settings.artifacts_dir / f"{stem}.zip"))
                    trace_stopped = True
                except Exception:
                    pass
                raise
            finally:
                if not trace_stopped:
                    try:
                        context.tracing.stop()
                    except Exception:
                        pass
                browser.close()


def authenticate_interactively(settings: Settings) -> Path:
    settings.ensure_runtime_dirs()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")
        print("Log into the dedicated X account in the opened browser.")
        input("After the home timeline is visible, press Enter here to save the session: ")
        if "/i/flow/login" in page.url:
            browser.close()
            raise SessionExpiredError("Login was not completed; session was not saved.")
        context.storage_state(path=str(settings.storage_state_path))
        browser.close()
    return settings.storage_state_path
