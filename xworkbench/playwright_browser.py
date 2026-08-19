from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from .config import Settings
from .errors import CollectionCancelled, CollectionError, CredentialError, InvalidRequestError
from .models import CollectionRequest, CollectionSummary, Post, ProviderType, SourceType, utc_now

HOME_URL = "https://x.com/home"
LOGIN_URL = "https://x.com/i/flow/login"
ARTICLE_SELECTOR = 'article[data-testid="tweet"]:visible'
STATUS_RE = re.compile(r"^/([^/]+)/status/(\d+)(?:/.*)?$")
WEB_STATUS_RE = re.compile(r"^/i/web/status/(\d+)(?:/.*)?$")
HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{1,15})(?:\b|$)")
STATE_SCHEMA_VERSION = 1
MAX_STATE_BYTES = 2 * 1024 * 1024
PROGRESS_POLL_MS = 100
PROGRESS_WAIT_MS = 2_000
SYNC_CALL_MAX_MS = 5_000
TELEMETRY_COUNT_MAX = 100_000
BOUND_STATUSES = {
    "verified_live",
    "expired",
    "manual_action_required",
    "unavailable",
}

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
      quote: label("quote"),
      like: label("like"),
      bookmark: label("bookmark"),
      view: label("analytics"),
    },
    media,
    sourcePosition,
  };
})
"""

DRIFT_PROJECTION = r"""
nodes => nodes.filter(node => {
  const box = node.getBoundingClientRect();
  return box.width > 0 && box.height > 0 && getComputedStyle(node).visibility !== "hidden";
}).slice(0, 20).map(node => ({
  tag: node.tagName.toLowerCase(),
  testId: node.getAttribute("data-testid"),
  role: node.getAttribute("role"),
  statusLinks: node.querySelectorAll('a[href*="/status/"]').length,
  times: node.querySelectorAll("time[datetime]").length,
  textNodes: node.querySelectorAll('[data-testid="tweetText"]').length,
  userNodes: node.querySelectorAll('[data-testid="User-Name"]').length,
}))
"""


class BrowserCollectionError(CollectionError):
    code = "browser_failure"
    retryable = True


class BrowserUnavailableError(BrowserCollectionError, CredentialError):
    code = "browser_unavailable"


class BrowserSessionMissingError(BrowserCollectionError, CredentialError):
    code = "session_missing"
    retryable = False


class BrowserSessionInvalidError(BrowserCollectionError, CredentialError):
    code = "session_invalid"
    retryable = False


class BrowserSessionExpiredError(BrowserCollectionError, CredentialError):
    code = "session_expired"
    retryable = False


class BrowserManualActionRequired(BrowserCollectionError, CredentialError):
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


def _source_url(request: CollectionRequest) -> str:
    if request.source_type is SourceType.HOME:
        return HOME_URL
    if request.source_type is SourceType.PROFILE:
        return f"https://x.com/{request.source_value}"
    if request.source_type is SourceType.SEARCH:
        query = urlencode(
            (("q", request.source_value), ("src", "typed_query"), ("f", "live"))
        )
        return f"https://x.com/search?{query}"
    raise InvalidRequestError("Browser capture source is unsupported.")


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
    if not isinstance(label, str):
        return None
    match = re.search(
        r"(?<![\d.,])(\d+(?:,\d{3})*(?:\.\d+)?)\s*([KMB])?\b",
        label,
        re.I,
    )
    if not match or ("." in match.group(1) and not match.group(2)):
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(
        (match.group(2) or "").upper(), 1
    )
    result = value * multiplier
    return int(result) if result == result.to_integral_value() else None


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
        quote_count=_metric(metrics.get("quote")),
        bookmark_count=_metric(metrics.get("bookmark")),
        view_count=_metric(metrics.get("view")),
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


def _require_private_directory(path: Path) -> os.stat_result:
    parent = path.lstat()
    if not stat.S_ISDIR(parent.st_mode):
        raise OSError("Browser state parent must be a real directory.")
    if os.name != "nt" and (
        stat.S_IMODE(parent.st_mode) != 0o700 or parent.st_uid != os.getuid()
    ):
        raise OSError("Browser state parent must be owned by this user with mode 0700.")
    return parent


def _atomic_text(path: Path, text: str) -> None:
    _require_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _private_file_bytes(path: Path) -> bytes:
    _require_private_directory(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("state is not a regular file")
        if os.name != "nt" and (
            file_stat.st_mode & 0o077
            or file_stat.st_uid != os.getuid()
        ):
            raise ValueError("state permissions are not private")
        if not 0 < file_stat.st_size <= MAX_STATE_BYTES:
            raise ValueError("state size is invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            value = source.read(MAX_STATE_BYTES + 1)
        if len(value) > MAX_STATE_BYTES:
            raise ValueError("state size changed while being read")
        return value
    finally:
        os.close(descriptor)


def _allowed_x_host(value: Any) -> bool:
    host = str(value or "").lower().lstrip(".")
    return any(host == base or host.endswith(f".{base}") for base in ("x.com", "twitter.com"))


def _allowed_origin(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or ""))
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and _allowed_x_host(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and port in (None, 443)
        and parsed.path in ("", "/")
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def _filtered_storage_state(value: Any, *, reject_disallowed: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("storage state must be an object")
    raw_cookies = value.get("cookies")
    raw_origins = value.get("origins")
    if not isinstance(raw_cookies, list) or not isinstance(raw_origins, list):
        raise ValueError("storage state cookies and origins must be arrays")
    cookies = []
    for cookie in raw_cookies:
        valid = (
            isinstance(cookie, dict)
            and isinstance(cookie.get("name"), str)
            and isinstance(cookie.get("value"), str)
            and isinstance(cookie.get("domain"), str)
        )
        if not valid:
            raise ValueError("storage state cookie is malformed")
        if not _allowed_x_host(cookie["domain"]):
            if reject_disallowed:
                raise ValueError("storage state contains a non-X cookie")
            continue
        cookies.append(cookie)
    origins = []
    for origin in raw_origins:
        local_storage = origin.get("localStorage") if isinstance(origin, dict) else None
        valid = (
            isinstance(origin, dict)
            and _allowed_origin(origin.get("origin"))
            and isinstance(local_storage, list)
            and all(
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("value"), str)
                for item in local_storage
            )
        )
        if not valid:
            if reject_disallowed or not isinstance(origin, dict):
                raise ValueError("storage state origin is malformed or outside X")
            continue
        origins.append(origin)
    return {"cookies": cookies, "origins": origins}


def _valid_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return value if datetime.fromisoformat(value).tzinfo is not None else None
    except ValueError:
        return None


def _state_details(settings: Settings) -> dict[str, Any]:
    path = _state_path(settings)
    try:
        raw = _private_file_bytes(path)
    except FileNotFoundError:
        return {"status": "missing", "valid": False, "digest": None, "verifiedAt": None}
    except (OSError, ValueError):
        return {
            "status": "invalid_local_state",
            "valid": False,
            "digest": None,
            "verifiedAt": None,
        }
    try:
        _filtered_storage_state(json.loads(raw), reject_disallowed=True)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {
            "status": "invalid_local_state",
            "valid": False,
            "digest": None,
            "verifiedAt": None,
        }
    digest = hashlib.sha256(raw).hexdigest()
    try:
        marker = json.loads(_private_file_bytes(_status_path(settings)))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        marker = None
    status = marker.get("status") if isinstance(marker, dict) else None
    verified_at = _valid_timestamp(marker.get("verifiedAt")) if isinstance(marker, dict) else None
    if not (
        isinstance(marker, dict)
        and marker.get("schemaVersion") == STATE_SCHEMA_VERSION
        and marker.get("stateSha256") == digest
        and status in BOUND_STATUSES
        and (status != "verified_live" or verified_at)
    ):
        status = "present_unverified"
        verified_at = None
    return {"status": status, "valid": True, "digest": digest, "verifiedAt": verified_at}


def _record_status(
    settings: Settings, status: str, *, verified_at: str | None = None
) -> bool:
    status = "verified_live" if status == "ready" else status
    if status not in BOUND_STATUSES:
        return False
    details = _state_details(settings)
    if not details["valid"]:
        return False
    if status == "verified_live":
        verified_at = verified_at or utc_now()
    else:
        verified_at = details["verifiedAt"] or verified_at
    marker = {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "stateSha256": details["digest"],
        "status": status,
        "verifiedAt": verified_at,
    }
    try:
        _atomic_text(
            _status_path(settings),
            json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        )
    except OSError:
        return False
    return True


def _saved_status(settings: Settings) -> str:
    return str(_state_details(settings)["status"])


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
            "Playwright is not installed. Install it, then run: "
            "python -m playwright install chromium"
        ) from exc
    return sync_playwright()


def _close(value: Any) -> None:
    if value is None:
        return
    try:
        value.close()
    except Exception:
        pass


def _save_storage_state(
    context: Any, path: Path, *, expected_digest: str | None = None
) -> bool:
    _require_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        context.storage_state(path=str(temporary))
        raw = temporary.read_bytes()
        if not 0 < len(raw) <= MAX_STATE_BYTES:
            raise ValueError("Playwright storage state has an invalid size.")
        filtered = _filtered_storage_state(json.loads(raw), reject_disallowed=False)
        if expected_digest is not None:
            try:
                current_digest = hashlib.sha256(_private_file_bytes(path)).hexdigest()
            except (OSError, ValueError):
                return False
            if current_digest != expected_digest:
                return False
        _atomic_text(
            path,
            json.dumps(filtered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
        )
        return True
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
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
            browser = playwright.chromium.launch(headless=False, timeout=remaining_ms)
            context = browser.new_context()
            page = context.new_page()
            page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=min(30_000, max(1, int((deadline - time.monotonic()) * 1_000))),
            )
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
                    if not _save_storage_state(context, _state_path(settings)):
                        raise BrowserUnavailableError("Browser session changed while being saved.")
                    verified_at = utc_now()
                    if not _record_status(settings, "verified_live", verified_at=verified_at):
                        raise BrowserUnavailableError(
                            "Browser session verification could not be saved."
                        )
                    return {
                        "status": "verified_live",
                        "valid": True,
                        "ready": True,
                        "verifiedAt": verified_at,
                        "message": "Browser session saved locally.",
                    }
                page.wait_for_timeout(
                    min(250, max(1, int((deadline - time.monotonic()) * 1_000)))
                )
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
    provider_version = 2
    parser_version = 2

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
            "parserVersion": self.parser_version,
            "sources": [source.value for source in SourceType],
            "limits": {"minimum": 1, "default": 5, "maximum": 25},
            "confirmation": {"field": "confirmBrowserCapture", "kind": "browser_capture"},
            "headed": not self.settings.browser_headless,
            "experimental": True,
        }

    def connection_status(self) -> dict[str, Any]:
        details = _state_details(self.settings)
        if not _playwright_installed() and self._playwright_factory is None:
            return {
                "provider": self.provider_id.value,
                "providerVersion": self.provider_version,
                "status": "unavailable",
                "valid": details["valid"],
                "localStateValid": details["valid"],
                "ready": False,
                "verifiedAt": details["verifiedAt"],
                "message": "Install Playwright and Chromium. Run: "
                "python -m playwright install chromium",
            }
        status = details["status"]
        messages = {
            "missing": "No browser session is saved. Run: xworkbench auth",
            "invalid_local_state": "Saved browser state is invalid or not private; run auth again.",
            "present_unverified": "Saved browser state has not been live-verified; run auth again.",
            "verified_live": "Saved browser session was verified live.",
            "expired": "Saved browser session expired; run xworkbench auth.",
            "manual_action_required": "X requires normal manual action in a headed browser.",
            "unavailable": "Saved browser session could not be used.",
        }
        ready = status == "verified_live"
        return {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            "parserVersion": self.parser_version,
            "status": status,
            "valid": details["valid"],
            "localStateValid": details["valid"],
            "ready": ready,
            "verifiedAt": details["verifiedAt"],
            "message": messages[status],
        }

    def _require_ready(self) -> dict[str, Any]:
        status = self.connection_status()
        if status["ready"]:
            return status
        error = {
            "missing": BrowserSessionMissingError,
            "invalid_local_state": BrowserSessionInvalidError,
            "present_unverified": BrowserSessionInvalidError,
            "expired": BrowserSessionExpiredError,
            "manual_action_required": BrowserManualActionRequired,
        }.get(status["status"], BrowserUnavailableError)
        raise error(status["message"])

    def prepare(
        self,
        request: CollectionRequest,
        supplied_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if request.provider is not self.provider_id or not 1 <= request.max_posts <= 25:
            raise InvalidRequestError("Browser capture supports 1–25 visible posts only.")
        try:
            normalized_request = CollectionRequest.from_dict(request.to_dict())
        except (AttributeError, TypeError, ValueError) as exc:
            raise InvalidRequestError("Browser capture request is invalid.") from exc
        if normalized_request != request:
            raise InvalidRequestError(
                "Browser capture requires a validated, normalized source."
            )
        source_url = _source_url(request)
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
            "parserVersion": self.parser_version,
            "sourceKind": request.source_type.value,
            "sourceValue": request.source_value,
            "sourceUrl": source_url,
            "targetPosts": request.max_posts,
            "preparedAt": prepared_at,
            "browserHeadless": self.settings.browser_headless,
            "jobTimeoutSeconds": self.settings.job_timeout_seconds,
            "pageTimeoutMs": self.settings.page_timeout_ms,
            "noProgressLimit": self.settings.no_progress_limit,
        }
        if supplied_plan is None:
            self._require_ready()
            return plan
        if supplied_plan != plan:
            raise InvalidRequestError("Browser execution plan does not match the request.")
        self._require_ready()
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
            return BrowserUnavailableError("The X capture surface is currently unavailable.")
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

    def _project(
        self,
        page: Any,
        should_cancel: Callable[[], bool],
        deadline: float,
        page_timeout: int,
    ) -> list[dict[str, Any]]:
        self._set_page_timeout(page, should_cancel, deadline, page_timeout)
        try:
            result = page.locator(ARTICLE_SELECTOR).evaluate_all(DOM_PROJECTION)
        except Exception as exc:
            raise BrowserSchemaError("Visible X timeline articles could not be read.") from exc
        if not isinstance(result, list):
            raise BrowserSchemaError("The X timeline article schema changed.")
        return [item for item in result if isinstance(item, dict)]

    def _selector_drift_report(self, page: Any) -> dict[str, Any]:
        try:
            rows = page.locator(
                'article, [role="article"], [data-testid*="tweet" i]'
            ).evaluate_all(DRIFT_PROJECTION)
        except Exception:
            rows = []
        candidates = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            descriptor = {}
            for source, target in (("tag", "tag"), ("testId", "testId"), ("role", "role")):
                value = row.get(source)
                descriptor[target] = (
                    value
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value)
                    else None
                )
            evidence = {
                key: min(_as_int(row.get(key)), 99)
                for key in ("statusLinks", "times", "textNodes", "userNodes")
            }
            score = min(
                0.95,
                (0.45 if evidence["statusLinks"] else 0)
                + (0.20 if evidence["times"] else 0)
                + (0.20 if evidence["textNodes"] else 0)
                + (0.15 if evidence["userNodes"] else 0),
            )
            if score:
                candidates.append(
                    {"candidate": descriptor, "confidence": score, "evidence": evidence}
                )
        candidates.sort(key=lambda item: item["confidence"], reverse=True)
        return {
            "candidates": candidates[:3],
            "action": "maintainer_review_required",
            "autoPromoted": False,
        }

    @staticmethod
    def _projection_signature(projected: list[dict[str, Any]]) -> tuple[frozenset[str], str]:
        identities = {
            identity[0]
            for article in projected
            if (identity := _outer_identity(article.get("identityCandidates")))
        }
        digest = hashlib.sha256(
            json.dumps(projected, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()
        return frozenset(identities), digest

    def _check_stop(self, should_cancel: Callable[[], bool], deadline: float) -> None:
        if should_cancel():
            raise CollectionCancelled("Browser capture cancelled by the user.")
        if self._monotonic() >= deadline:
            raise BrowserTimeoutError("Browser capture reached its bounded job timeout.")

    def _remaining_ms(
        self,
        should_cancel: Callable[[], bool],
        deadline: float,
        cap: int | None = None,
    ) -> int:
        self._check_stop(should_cancel, deadline)
        remaining = max(1, int((deadline - self._monotonic()) * 1_000))
        return min(remaining, cap) if cap is not None else remaining

    def _set_page_timeout(
        self,
        page: Any,
        should_cancel: Callable[[], bool],
        deadline: float,
        page_timeout: int,
    ) -> int:
        timeout = self._remaining_ms(
            should_cancel, deadline, min(page_timeout, SYNC_CALL_MAX_MS)
        )
        page.set_default_timeout(timeout)
        return timeout

    def _wait_for_first_article(
        self,
        page: Any,
        should_cancel: Callable[[], bool],
        deadline: float,
        page_timeout: int,
    ) -> None:
        budget = self._remaining_ms(should_cancel, deadline, page_timeout)
        last_error = None
        while budget > 0:
            interval = min(PROGRESS_POLL_MS, budget)
            try:
                page.locator(ARTICLE_SELECTOR).first.wait_for(
                    state="visible", timeout=interval
                )
                return
            except Exception as exc:
                last_error = exc
            budget -= interval
            self._check_stop(should_cancel, deadline)
            self._raise_page_failure(page)
        report = self._selector_drift_report(page)
        raise BrowserSchemaError(
            "No visible X timeline posts appeared before the bounded timeout. "
            f"Sanitized selector drift report: {json.dumps(report, separators=(',', ':'))}"
        ) from last_error

    def _wait_for_progress(
        self,
        page: Any,
        previous: tuple[frozenset[str], str],
        should_cancel: Callable[[], bool],
        deadline: float,
        page_timeout: int,
    ) -> list[dict[str, Any]]:
        budget = self._remaining_ms(
            should_cancel, deadline, min(page_timeout, PROGRESS_WAIT_MS)
        )
        projected: list[dict[str, Any]] = []
        while budget > 0:
            interval = min(PROGRESS_POLL_MS, budget)
            page.wait_for_timeout(interval)
            budget -= interval
            self._check_stop(should_cancel, deadline)
            self._raise_page_failure(page)
            projected = self._project(page, should_cancel, deadline, page_timeout)
            if self._projection_signature(projected) != previous:
                return projected
        return projected

    def _refresh_state(
        self, context: Any, state_path: Path, expected_digest: str
    ) -> str | None:
        try:
            if not _save_storage_state(
                context, state_path, expected_digest=expected_digest
            ):
                return "Browser state changed during capture and was not overwritten."
            if not _record_status(self.settings, "verified_live", verified_at=utc_now()):
                return "Browser state was refreshed but could not be marked verified."
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return "Browser state could not be refreshed; capture results were preserved."
        return None

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
        session = _state_details(self.settings)
        if session["status"] != "verified_live" or not session["digest"]:
            raise BrowserSessionInvalidError(
                "Saved browser state changed after preview; run xworkbench auth again."
            )
        loaded_digest = str(session["digest"])
        source_kind = request.source_type.value
        source_value = request.source_value
        source_url = _source_url(request)

        prior = checkpoint.get("providerState")
        prior = prior if isinstance(prior, dict) else {}
        seen = {
            str(value)
            for value in prior.get("seenPostIds", [])
            if isinstance(value, str | int)
        }
        scans = _as_int(prior.get("scanIterations"))
        scrolls = _as_int(prior.get("scrollIterations"))
        checkpoint_metadata = checkpoint.get("metadata")
        checkpoint_metadata = (
            checkpoint_metadata if isinstance(checkpoint_metadata, dict) else {}
        )
        segment = min(
            _as_int(checkpoint_metadata.get("captureSegment")), TELEMETRY_COUNT_MAX
        )
        segment_scans = 0
        stored = _as_int(checkpoint.get("storedCount"))
        if stored >= request.max_posts:
            return CollectionSummary(completion_reason="target_reached")

        timeout = max(1.0, float(self.settings.job_timeout_seconds))
        page_timeout = max(1, self.settings.page_timeout_ms)
        no_progress_limit = max(1, self.settings.no_progress_limit)
        started_at = self._monotonic()
        deadline = started_at + timeout
        maximum_elapsed_ms = min(int(timeout * 1_000), 3_600_000)
        browser = context = page = None
        no_progress = 0
        callback_failure = None
        visible_cards = parsed_cards = duplicate_ids = skipped_cards = 0
        skip_reasons: dict[str, int] = {}
        coverage_fields = {
            "text": 0,
            "authorUsername": 0,
            "createdAt": 0,
            "likeCount": 0,
            "replyCount": 0,
            "repostCount": 0,
            "quoteCount": 0,
            "bookmarkCount": 0,
            "viewCount": 0,
            "media": 0,
        }
        drift_report = None
        first_post_latency_ms = None
        last_progress_elapsed_ms = None
        scan_duration_ms = 0

        def elapsed_ms() -> int:
            return min(
                maximum_elapsed_ms,
                max(0, int((self._monotonic() - started_at) * 1_000)),
            )

        def bounded_count(value: int) -> int:
            return min(max(0, value), TELEMETRY_COUNT_MAX)

        def capture_metadata(stop_reason: str | None = None) -> dict[str, Any]:
            elapsed = elapsed_ms()
            last_progress = last_progress_elapsed_ms
            evidence = {
                field: {
                    "present": bounded_count(present),
                    "missing": bounded_count(max(0, parsed_cards - present)),
                }
                for field, present in coverage_fields.items()
            }
            metadata = {
                "browserVersion": browser_version,
                "providerVersion": self.provider_version,
                "parserVersion": self.parser_version,
                "sourceKind": source_kind,
                "sourceValue": source_value,
                "sourceUrl": source_url,
                "scanIterations": bounded_count(scans),
                "scrollIterations": bounded_count(scrolls),
                "captureSegment": segment,
                "segmentScanIterations": bounded_count(segment_scans),
                "observedAt": observed_at,
                "elapsedMs": elapsed,
                "firstPostLatencyMs": first_post_latency_ms,
                "lastProgressElapsedMs": last_progress,
                "scanDurationMs": min(scan_duration_ms, maximum_elapsed_ms),
                "stallDurationMs": min(
                    max(0, elapsed - (last_progress if last_progress is not None else 0)),
                    maximum_elapsed_ms,
                ),
                "stopReason": stop_reason,
                "visibleCards": bounded_count(visible_cards),
                "parsedCards": bounded_count(parsed_cards),
                "duplicatePostIds": bounded_count(duplicate_ids),
                "skippedCards": bounded_count(skipped_cards),
                "skipReasons": {
                    reason: bounded_count(count)
                    for reason, count in sorted(skip_reasons.items())
                },
                "fieldCoverage": {
                    field: {
                        "present": evidence[field]["present"],
                        "total": bounded_count(parsed_cards),
                        "ratio": round(present / parsed_cards, 3)
                        if parsed_cards
                        else None,
                    }
                    for field, present in coverage_fields.items()
                },
                "fieldExtractionEvidence": evidence,
            }
            if drift_report and drift_report["candidates"]:
                metadata["selectorDriftReport"] = drift_report
            return metadata

        def persist_batch(
            posts: list[Post], state: dict[str, Any], metadata: dict[str, Any]
        ) -> int:
            nonlocal callback_failure
            try:
                return _as_int(on_batch(posts, state, metadata))
            except Exception as exc:
                callback_failure = exc
                raise

        try:
            with (self._playwright_factory or _sync_playwright)() as playwright:
                browser = playwright.chromium.launch(
                    headless=self.settings.browser_headless,
                    timeout=self._remaining_ms(
                        should_cancel,
                        deadline,
                        min(page_timeout, SYNC_CALL_MAX_MS),
                    ),
                )
                self._check_stop(should_cancel, deadline)
                browser_version = str(getattr(browser, "version", "unknown"))
                context = browser.new_context(storage_state=str(state_path))
                self._check_stop(should_cancel, deadline)
                page = context.new_page()
                navigation_timeout = self._set_page_timeout(
                    page, should_cancel, deadline, page_timeout
                )
                page.goto(
                    source_url,
                    wait_until="domcontentloaded",
                    timeout=navigation_timeout,
                )
                self._check_stop(should_cancel, deadline)
                self._raise_page_failure(page)
                self._wait_for_first_article(
                    page, should_cancel, deadline, page_timeout
                )
                projected = self._project(
                    page, should_cancel, deadline, page_timeout
                )

                while stored < request.max_posts:
                    self._check_stop(should_cancel, deadline)
                    self._raise_page_failure(page)
                    scan_started_at = self._monotonic()
                    observed_at = utc_now()
                    scans += 1
                    segment_scans += 1
                    visible_cards += len(projected)
                    posts = []
                    for article in projected:
                        post = parse_projected_article(article, observed_at=observed_at)
                        if post is None:
                            skipped_cards += 1
                            candidates = article.get("identityCandidates")
                            reason = (
                                "missing_status_identity"
                                if not isinstance(candidates, list) or not candidates
                                else "missing_outer_identity"
                            )
                            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                            continue
                        parsed_cards += 1
                        values = {
                            "text": post.text,
                            "authorUsername": post.author_username,
                            "createdAt": post.created_at,
                            "likeCount": post.like_count,
                            "replyCount": post.reply_count,
                            "repostCount": post.repost_count,
                            "quoteCount": post.quote_count,
                            "bookmarkCount": post.bookmark_count,
                            "viewCount": post.view_count,
                            "media": post.media,
                        }
                        for field, value in values.items():
                            coverage_fields[field] += int(value is not None)
                        if post.post_id in seen:
                            duplicate_ids += 1
                            continue
                        seen.add(post.post_id)
                        posts.append(post)
                        if stored + len(posts) >= request.max_posts:
                            break

                    now_elapsed_ms = elapsed_ms()
                    scan_duration_ms = min(
                        maximum_elapsed_ms,
                        max(0, int((self._monotonic() - scan_started_at) * 1_000)),
                    )
                    if posts and first_post_latency_ms is None:
                        first_post_latency_ms = now_elapsed_ms

                    if skipped_cards and drift_report is None:
                        drift_report = self._selector_drift_report(page)

                    state = {
                        "seenPostIds": sorted(seen),
                        "scanIterations": scans,
                        "scrollIterations": scrolls,
                        "captureSegment": segment,
                        "segmentScanIterations": segment_scans,
                    }
                    added = persist_batch(posts, state, capture_metadata())
                    stored += added
                    if added:
                        last_progress_elapsed_ms = elapsed_ms()
                    self._check_stop(should_cancel, deadline)
                    if stored >= request.max_posts:
                        persist_batch(
                            [], state, capture_metadata(stop_reason="target_reached")
                        )
                        warning = self._refresh_state(context, state_path, loaded_digest)
                        return CollectionSummary(
                            warnings=[warning] if warning else [],
                            completion_reason="target_reached",
                        )

                    no_progress = 0 if posts and added else no_progress + 1
                    if no_progress >= no_progress_limit:
                        persist_batch([], state, capture_metadata(stop_reason="no_progress"))
                        warnings = [
                            "Browser capture stopped after repeated scans found no new "
                            "unique post IDs."
                        ]
                        if warning := self._refresh_state(
                            context, state_path, loaded_digest
                        ):
                            warnings.append(warning)
                        return CollectionSummary(
                            warnings=warnings,
                            completion_reason="no_progress",
                            partial=True,
                        )
                    self._check_stop(should_cancel, deadline)
                    scrolls += 1
                    self._set_page_timeout(
                        page, should_cancel, deadline, page_timeout
                    )
                    page.evaluate(
                        "window.scrollBy(0, Math.max(400, window.innerHeight * 0.8))"
                    )
                    self._check_stop(should_cancel, deadline)
                    projected = self._wait_for_progress(
                        page,
                        self._projection_signature(projected),
                        should_cancel,
                        deadline,
                        page_timeout,
                    )
        except CollectionError:
            raise
        except Exception as exc:
            if exc is callback_failure:
                raise
            self._check_stop(should_cancel, deadline)
            _record_status(self.settings, "unavailable")
            raise BrowserUnavailableError("Headed Chromium became unavailable.") from exc
        finally:
            _close(page)
            _close(context)
            _close(browser)

        raise BrowserUnavailableError("Headed Chromium closed before capture completed.")
