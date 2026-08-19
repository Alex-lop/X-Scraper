from __future__ import annotations

import importlib.util
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .errors import CollectionCancelled, CollectionError, InvalidRequestError
from .models import CollectionRequest, CollectionSummary, Post, ProviderType, SourceType, utc_now

HOME_URL = "https://x.com/home"
LOGIN_URL = "https://x.com/i/flow/login"
ARTICLE_SELECTOR = 'article[data-testid="tweet"]:visible'
STATUS_RE = re.compile(r"^/([^/]+)/status/(\d+)(?:/.*)?$")
WEB_STATUS_RE = re.compile(r"^/i/web/status/(\d+)(?:/.*)?$")
HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{1,15})(?:\b|$)")

# This only projects visible DOM. Python chooses the outer identity and normalizes it so the
# important parser behavior stays testable without Chromium or an X connection.
DOM_PROJECTION = r"""
articles => articles.filter(article => {
  if (article.parentElement?.closest('article[data-testid="tweet"]')) return false;
  const box = article.getBoundingClientRect();
  return box.width > 0 && box.height > 0 && getComputedStyle(article).visibility !== "hidden";
}).map((article, sourcePosition) => {
  const nestedSelector = '[data-testid="quoteTweet"], [data-testid="card.wrapper"]';
  const nested = element => Boolean(element.closest(nestedSelector))
    || element.closest('article[data-testid="tweet"]') !== article;
  const outer = selector => [...article.querySelectorAll(selector)]
    .find(element => !nested(element));
  const depth = element => {
    let value = 0;
    for (let node = element; node && node !== article; node = node.parentElement) value += 1;
    return value;
  };
  const identityCandidates = [...article.querySelectorAll('a[href*="/status/"]')]
    .map((anchor, order) => ({
      href: anchor.href,
      depth: depth(anchor),
      order,
      hasTime: Boolean(anchor.querySelector("time")),
      timestamp: anchor.querySelector("time")?.getAttribute("datetime") || null,
      nested: nested(anchor),
    }));
  const text = outer('[data-testid="tweetText"]');
  const user = outer('[data-testid="User-Name"]');
  const social = outer('[data-testid="socialContext"]');
  const label = id => outer(`[data-testid="${id}"]`)
    ?.getAttribute("aria-label") || null;
  const media = [...article.querySelectorAll('[data-testid="tweetPhoto"] img, video[poster]')]
    .filter(item => !nested(item))
    .map(item => ({
      type: item.tagName === "VIDEO" ? "video" : "photo",
      url: item.currentSrc || item.src || item.poster || null,
      altText: item.getAttribute("alt") || null,
    }));
  return {
    identityCandidates,
    text: text ? text.innerText : null,
    userText: user ? user.innerText : null,
    socialContext: social ? social.innerText : null,
    articleText: article.innerText,
    metrics: {
      reply: label("reply"),
      repost: label("retweet"),
      like: label("like"),
      bookmark: label("bookmark"),
    },
    media,
    sourcePosition,
  };
})
"""


class BrowserCollectionError(CollectionError):
    code = "browser_failure"
    retryable = True


class BrowserUnavailableError(BrowserCollectionError):
    code = "browser_unavailable"


class BrowserSessionMissingError(BrowserCollectionError):
    code = "session_missing"
    retryable = False


class BrowserSessionExpiredError(BrowserCollectionError):
    code = "session_expired"
    retryable = False


class BrowserManualActionRequired(BrowserCollectionError):
    code = "manual_action_required"
    retryable = False


class BrowserRateLimitedError(BrowserCollectionError):
    code = "browser_rate_limited"


class BrowserSchemaError(BrowserCollectionError):
    code = "browser_schema_failure"


class BrowserTimeoutError(BrowserCollectionError):
    code = "job_timeout"


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 0 else default


def _status_identity(href: Any) -> tuple[str, str, str | None] | None:
    parsed = urlparse(str(href or ""))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
    }:
        return None
    if match := STATUS_RE.fullmatch(parsed.path):
        handle, post_id = match.groups()
        return post_id, f"https://x.com/{handle}/status/{post_id}", handle
    if match := WEB_STATUS_RE.fullmatch(parsed.path):
        post_id = match.group(1)
        return post_id, f"https://x.com/i/web/status/{post_id}", None
    return None


def _outer_identity(candidates: Any) -> tuple[str, str, str | None, str | None] | None:
    valid = []
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        identity = _status_identity(candidate.get("href"))
        if not identity:
            continue
        valid.append((candidate, identity))
    if not valid:
        return None
    outer = [item for item in valid if not item[0].get("nested")]
    if not outer:
        return None
    pool = outer
    timed = [item for item in pool if item[0].get("hasTime")]
    candidate, identity = min(
        timed or pool,
        key=lambda item: (
            _as_int(item[0].get("depth"), 1_000_000),
            _as_int(item[0].get("order"), 1_000_000),
        ),
    )
    return *identity, str(candidate["timestamp"]) if candidate.get("timestamp") else None


def _metric(label: Any) -> int | None:
    if not isinstance(label, str) or re.search(r"\d[\d,.]*\s*[KMB]\b", label, re.I):
        return None
    match = re.search(r"(?<!\d)(\d[\d,]*)(?!\d)", label)
    return int(match.group(1).replace(",", "")) if match else None


def parse_projected_article(
    article: dict[str, Any], *, observed_at: str | None = None
) -> Post | None:
    """Normalize one article projection; malformed or identity-less cards are ignored."""
    if not (identity := _outer_identity(article.get("identityCandidates"))):
        return None
    post_id, url, identity_handle, created_at = identity
    user_text = article.get("userText")
    handle_match = HANDLE_RE.search(user_text) if isinstance(user_text, str) else None
    author = handle_match.group(1) if handle_match else identity_handle
    media = [
        {
            "type": item.get("type") if item.get("type") in {"photo", "video"} else None,
            "url": item["url"],
            "altText": item.get("altText") if isinstance(item.get("altText"), str) else None,
        }
        for item in article.get("media", [])
        if isinstance(item, dict) and isinstance(item.get("url"), str)
    ]
    metrics = article.get("metrics") if isinstance(article.get("metrics"), dict) else {}
    article_text = article.get("articleText")
    social = article.get("socialContext")
    candidates = article.get("identityCandidates")
    is_quote = any(
        isinstance(item, dict) and item.get("nested") and _status_identity(item.get("href"))
        for item in candidates if isinstance(candidates, list)
    )
    position = article.get("sourcePosition")
    return Post(
        post_id=post_id,
        text=article.get("text") if isinstance(article.get("text"), str) else None,
        author_username=author,
        url=url,
        created_at=created_at,
        observed_at=observed_at or utc_now(),
        like_count=_metric(metrics.get("like")),
        reply_count=_metric(metrics.get("reply")),
        repost_count=_metric(metrics.get("repost")),
        bookmark_count=_metric(metrics.get("bookmark")),
        is_reply=(
            True
            if isinstance(article_text, str) and "replying to @" in article_text.casefold()
            else None
        ),
        is_repost=(
            True if isinstance(social, str) and "repost" in social.casefold() else None
        ),
        is_quote=True if is_quote else None,
        has_media=True if media else None,
        media=media or None,
        source_position=_as_int(position) if position is not None else None,
    )


def _state_path(settings: Settings) -> Path:
    return Path(settings.storage_state_path)


def _status_path(settings: Settings) -> Path:
    path = _state_path(settings)
    return path.with_name(f".{path.name}.auth-status")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(text)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _record_status(settings: Settings, status: str) -> None:
    try:
        _atomic_text(_status_path(settings), json.dumps({"status": status}) + "\n")
    except OSError:
        pass


def _saved_status(settings: Settings) -> str:
    try:
        value = json.loads(_status_path(settings).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return "ready"
    status = value.get("status") if isinstance(value, dict) else None
    allowed = {"ready", "expired", "manual_action_required", "unavailable"}
    return status if status in allowed else "unavailable"


def _playwright_installed() -> bool:
    try:
        return importlib.util.find_spec("playwright.sync_api") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailableError(
            "Playwright is not installed. Install it, then run: playwright install chromium"
        ) from exc
    return sync_playwright()


def _close(value: Any) -> None:
    if value is None:
        return
    try:
        value.close()
    except Exception:
        pass


def _save_storage_state(context: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        context.storage_state(path=str(temporary))
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def authenticate(
    settings: Settings,
    *,
    timeout_seconds: float = 600,
    _playwright_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Let the user sign in on X itself, then atomically save app-owned browser state."""
    browser = context = page = None
    deadline = time.monotonic() + timeout_seconds
    try:
        with (_playwright_factory or _sync_playwright)() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            while time.monotonic() < deadline:
                parsed = urlparse(str(page.url))
                home_surface = page.locator(
                    'a[data-testid="AppTabBar_Home_Link"], [data-testid="primaryColumn"]'
                ).count()
                if (
                    parsed.hostname in {"x.com", "www.x.com"}
                    and parsed.path.rstrip("/") == "/home"
                    and home_surface
                ):
                    _save_storage_state(context, _state_path(settings))
                    _record_status(settings, "ready")
                    return {
                        "status": "ready",
                        "valid": True,
                        "message": "Browser session saved locally.",
                    }
                page.wait_for_timeout(500)
    except CollectionError:
        raise
    except Exception as exc:
        _record_status(settings, "unavailable")
        raise BrowserUnavailableError(
            "The headed Chromium authentication window became unavailable."
        ) from exc
    finally:
        _close(page)
        _close(context)
        _close(browser)
    _record_status(settings, "manual_action_required")
    raise BrowserManualActionRequired(
        "Authentication was not completed in time; run xworkbench auth again."
    )


def authenticate_interactively(settings: Settings) -> Path:
    authenticate(settings)
    return _state_path(settings)


class PlaywrightBrowserProvider:
    provider_id = ProviderType.PLAYWRIGHT_BROWSER
    provider_version = 1

    def __init__(
        self,
        settings: Settings,
        *,
        _playwright_factory: Callable[[], Any] | None = None,
        _monotonic: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self._playwright_factory = _playwright_factory
        self._monotonic = _monotonic

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            "sources": [SourceType.HOME.value],
            "limits": {"minimum": 1, "default": 5, "maximum": 25},
            "confirmation": {"field": "confirmBrowserCapture", "kind": "browser_capture"},
            "headed": not self.settings.browser_headless,
            "experimental": True,
        }

    def connection_status(self) -> dict[str, Any]:
        if not _playwright_installed() and self._playwright_factory is None:
            return {
                "provider": self.provider_id.value,
                "providerVersion": self.provider_version,
                "status": "unavailable",
                "valid": False,
                "ready": False,
                "message": "Install Playwright and Chromium.",
            }
        path = _state_path(self.settings)
        try:
            present = path.is_file() and path.stat().st_size > 0
        except OSError:
            return {
                "provider": self.provider_id.value,
                "providerVersion": self.provider_version,
                "status": "unavailable",
                "valid": False,
                "ready": False,
                "message": "Browser authentication state is unavailable.",
            }
        if not present:
            return {
                "provider": self.provider_id.value,
                "providerVersion": self.provider_version,
                "status": "missing",
                "valid": False,
                "ready": False,
                "message": "Run: xworkbench auth",
            }
        status = _saved_status(self.settings)
        messages = {
            "ready": "Saved browser session is ready.",
            "expired": "Saved browser session expired; run xworkbench auth.",
            "manual_action_required": "X requires normal manual action in a headed browser.",
            "unavailable": "Saved browser session could not be used.",
        }
        ready = status == "ready"
        return {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            "status": status,
            "valid": ready,
            "ready": ready,
            "message": messages[status],
        }

    def prepare(
        self,
        request: CollectionRequest,
        supplied_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if (
            request.provider is not self.provider_id
            or request.source_type is not SourceType.HOME
            or not 1 <= request.max_posts <= 25
        ):
            raise InvalidRequestError("Browser capture supports 1–25 Home-feed posts only.")
        prepared_at = supplied_plan.get("preparedAt") if supplied_plan else utc_now()
        if not isinstance(prepared_at, str) or not prepared_at:
            raise InvalidRequestError("Browser execution plan is missing preparedAt.")
        try:
            if datetime.fromisoformat(prepared_at).tzinfo is None:
                raise ValueError
        except ValueError as exc:
            raise InvalidRequestError("Browser execution plan has an invalid preparedAt.") from exc
        plan = {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            "sourceKind": SourceType.HOME.value,
            "sourceUrl": HOME_URL,
            "targetPosts": request.max_posts,
            "preparedAt": prepared_at,
            "browserHeadless": self.settings.browser_headless,
            "jobTimeoutSeconds": self.settings.job_timeout_seconds,
            "pageTimeoutMs": self.settings.page_timeout_ms,
            "noProgressLimit": self.settings.no_progress_limit,
        }
        if supplied_plan is None:
            return plan
        if supplied_plan != plan:
            raise InvalidRequestError("Browser execution plan does not match the request.")
        return supplied_plan

    def _page_failure(self, page: Any) -> BrowserCollectionError | None:
        url = str(getattr(page, "url", "")).casefold()
        try:
            login_form = page.locator(
                'input[autocomplete="username"], input[name="text"]'
            ).count()
        except Exception:
            login_form = 0
        if "/i/flow/login" in url or url.rstrip("/").endswith("/login") or login_form:
            return BrowserSessionExpiredError(
                "The saved X browser session expired; run xworkbench auth."
            )
        if any(part in url for part in ("/challenge", "/account/access", "arkose")):
            return BrowserManualActionRequired(
                "X requires normal manual action; no challenge was bypassed."
            )
        try:
            challenge_surface = page.locator(
                'iframe[src*="arkose" i], iframe[src*="captcha" i], '
                '[data-testid*="challenge" i], [aria-label*="captcha" i]'
            ).count()
        except Exception:
            challenge_surface = 0
        if challenge_surface:
            return BrowserManualActionRequired(
                "X requires normal manual action; no challenge was bypassed."
            )
        try:
            body = page.locator("body").evaluate(
                """body => {
                  const copy = body.cloneNode(true);
                  copy.querySelectorAll('article[data-testid="tweet"]')
                    .forEach(node => node.remove());
                  return copy.textContent || "";
                }"""
            ).casefold()
        except Exception:
            body = ""
        if any(
            phrase in body
            for phrase in (
                "verify you are human",
                "unusual activity",
                "complete the captcha",
                "security check",
            )
        ):
            return BrowserManualActionRequired(
                "X requires normal manual action; no challenge was bypassed."
            )
        if "rate limit exceeded" in body or "you are over the rate limit" in body:
            return BrowserRateLimitedError("X rate-limited this browser capture.")
        if "something went wrong. try reloading" in body or "this page isn’t available" in body:
            return BrowserUnavailableError("The X Home feed is currently unavailable.")
        return None

    def _raise_page_failure(self, page: Any) -> None:
        failure = self._page_failure(page)
        if failure is None:
            return
        if isinstance(failure, BrowserSessionExpiredError):
            _record_status(self.settings, "expired")
        elif isinstance(failure, BrowserManualActionRequired):
            _record_status(self.settings, "manual_action_required")
        raise failure

    def _project(self, page: Any) -> list[dict[str, Any]]:
        try:
            result = page.locator(ARTICLE_SELECTOR).evaluate_all(DOM_PROJECTION)
        except Exception as exc:
            raise BrowserSchemaError("Visible Home-feed articles could not be read.") from exc
        if not isinstance(result, list):
            raise BrowserSchemaError("The X Home-feed article schema changed.")
        return [item for item in result if isinstance(item, dict)]

    def _check_stop(self, should_cancel: Callable[[], bool], deadline: float) -> None:
        if should_cancel():
            raise CollectionCancelled("Browser capture cancelled by the user.")
        if self._monotonic() >= deadline:
            raise BrowserTimeoutError("Browser capture reached its bounded job timeout.")

    def collect(
        self,
        request: CollectionRequest,
        *,
        execution_plan: dict[str, Any],
        checkpoint: dict[str, Any],
        on_batch: Callable[[list[Post], Any, dict[str, Any]], int],
        should_cancel: Callable[[], bool],
    ) -> CollectionSummary:
        self.prepare(request, execution_plan)
        state_path = _state_path(self.settings)
        if not state_path.is_file():
            raise BrowserSessionMissingError("No saved browser session. Run: xworkbench auth")

        prior = checkpoint.get("providerState")
        prior = prior if isinstance(prior, dict) else {}
        seen = {
            str(value)
            for value in prior.get("seenPostIds", [])
            if isinstance(value, str | int)
        }
        scans = _as_int(prior.get("scanIterations"))
        scrolls = _as_int(prior.get("scrollIterations"))
        stored = _as_int(checkpoint.get("storedCount"))
        if stored >= request.max_posts:
            return CollectionSummary(completion_reason="target_reached")

        timeout = max(1.0, float(self.settings.job_timeout_seconds))
        page_timeout = max(1, self.settings.page_timeout_ms)
        no_progress_limit = max(1, self.settings.no_progress_limit)
        deadline = self._monotonic() + timeout
        browser = context = page = None
        no_progress = 0
        try:
            with (self._playwright_factory or _sync_playwright)() as playwright:
                browser = playwright.chromium.launch(headless=self.settings.browser_headless)
                browser_version = str(getattr(browser, "version", "unknown"))
                context = browser.new_context(storage_state=str(state_path))
                page = context.new_page()
                page.set_default_timeout(page_timeout)
                page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=min(page_timeout, int(timeout * 1_000)),
                )
                self._raise_page_failure(page)
                try:
                    page.locator(ARTICLE_SELECTOR).first.wait_for(
                        state="visible", timeout=page_timeout
                    )
                except Exception as exc:
                    self._raise_page_failure(page)
                    raise BrowserSchemaError(
                        "No visible Home-feed posts appeared before the bounded timeout."
                    ) from exc

                while stored < request.max_posts:
                    self._check_stop(should_cancel, deadline)
                    self._raise_page_failure(page)
                    observed_at = utc_now()
                    scans += 1
                    posts = []
                    for article in self._project(page):
                        post = parse_projected_article(article, observed_at=observed_at)
                        if post is None or post.post_id in seen:
                            continue
                        seen.add(post.post_id)
                        posts.append(post)
                        if stored + len(posts) >= request.max_posts:
                            break

                    state = {
                        "seenPostIds": sorted(seen),
                        "scanIterations": scans,
                        "scrollIterations": scrolls,
                    }
                    metadata = {
                        "browserVersion": browser_version,
                        "providerVersion": self.provider_version,
                        "sourceKind": SourceType.HOME.value,
                        "sourceUrl": HOME_URL,
                        "scanIterations": scans,
                        "scrollIterations": scrolls,
                        "observedAt": observed_at,
                    }
                    added = _as_int(on_batch(posts, state, metadata))
                    stored += added
                    if stored >= request.max_posts:
                        _record_status(self.settings, "ready")
                        return CollectionSummary(completion_reason="target_reached")

                    no_progress = 0 if posts and added else no_progress + 1
                    if no_progress >= no_progress_limit:
                        return CollectionSummary(
                            warnings=[
                                "Browser capture stopped after repeated scans found no new "
                                "unique post IDs."
                            ],
                            completion_reason="no_progress",
                            partial=True,
                        )
                    self._check_stop(should_cancel, deadline)
                    scrolls += 1
                    page.evaluate(
                        "window.scrollBy(0, Math.max(400, window.innerHeight * 0.8))"
                    )
                    page.wait_for_timeout(500)
        except (CollectionError, CollectionCancelled):
            raise
        except Exception as exc:
            _record_status(self.settings, "unavailable")
            raise BrowserUnavailableError("Headed Chromium became unavailable.") from exc
        finally:
            _close(page)
            _close(context)
            _close(browser)

        raise BrowserUnavailableError("Headed Chromium closed before capture completed.")
