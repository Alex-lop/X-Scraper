from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from .errors import InvalidRequestError

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


class SourceType(StrEnum):
    HOME = "home"
    PROFILE = "profile"
    SEARCH = "search"


class ProviderType(StrEnum):
    PLAYWRIGHT_BROWSER = "playwright_browser"
    OFFICIAL_X_API = "official_x_api"


class SearchMode(StrEnum):
    RECENT = "recent"
    FULL_ARCHIVE = "fullArchive"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    PARTIAL = "partial"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_date(value: Any, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise InvalidRequestError(f"{field_name} must use YYYY-MM-DD format.") from exc


def normalize_profile(value: str) -> str:
    raw = value.strip()
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            raise InvalidRequestError("Profile URL must point to x.com or twitter.com.")
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise InvalidRequestError("Profile URL does not contain a username.")
        raw = parts[0]
    raw = raw.lstrip("@")
    if not HANDLE_RE.fullmatch(raw):
        raise InvalidRequestError("Username must be 1–15 letters, numbers, or underscores.")
    return raw


@dataclass(frozen=True, slots=True)
class CollectionRequest:
    source_type: SourceType
    source_value: str
    provider: ProviderType = ProviderType.OFFICIAL_X_API
    search_mode: SearchMode | None = SearchMode.RECENT
    max_posts: int = 25
    start_date: str | None = None
    end_date: str | None = None
    include_replies: bool = False
    media_only: bool = False

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> CollectionRequest:
        if not isinstance(body, dict):
            raise InvalidRequestError("Request body must be a JSON object.")
        allowed = {
            "provider",
            "sourceType",
            "sourceValue",
            "searchMode",
            "maxPosts",
            "startDate",
            "endDate",
            "includeReplies",
            "mediaOnly",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise InvalidRequestError(f"Unknown request field(s): {', '.join(unknown)}.")
        raw_provider = str(body.get("provider", ProviderType.OFFICIAL_X_API.value))
        if raw_provider == "x_api_search":
            raw_provider = ProviderType.OFFICIAL_X_API.value
        try:
            provider = ProviderType(raw_provider)
        except ValueError as exc:
            raise InvalidRequestError(
                "provider must be 'playwright_browser' or 'official_x_api'."
            ) from exc

        if provider is ProviderType.PLAYWRIGHT_BROWSER:
            return cls._browser_request(body)

        try:
            source_type = SourceType(str(body.get("sourceType", "")))
        except ValueError as exc:
            raise InvalidRequestError("sourceType must be 'profile' or 'search'.") from exc
        if source_type is SourceType.HOME:
            raise InvalidRequestError("official_x_api supports only profile or search sources.")
        try:
            search_mode = SearchMode(str(body.get("searchMode", SearchMode.RECENT.value)))
        except ValueError as exc:
            raise InvalidRequestError("searchMode must be 'recent' or 'fullArchive'.") from exc
        raw_value = body.get("sourceValue")
        if not isinstance(raw_value, str):
            raise InvalidRequestError("sourceValue must be a string.")
        source_value = unicodedata.normalize("NFC", raw_value.strip())
        if not source_value:
            raise InvalidRequestError("sourceValue is required.")
        if source_type is SourceType.PROFILE:
            source_value = normalize_profile(source_value)
        elif len(source_value) > 1_024:
            raise InvalidRequestError("Search query cannot exceed 1,024 characters.")
        max_posts = body.get("maxPosts", 25)
        if isinstance(max_posts, bool) or not isinstance(max_posts, int):
            raise InvalidRequestError("maxPosts must be an integer.")
        if not 10 <= max_posts <= 500:
            raise InvalidRequestError("maxPosts must be between 10 and 500.")
        start_date = _parse_date(body.get("startDate"), "startDate")
        end_date = _parse_date(body.get("endDate"), "endDate")
        if start_date and end_date and start_date > end_date:
            raise InvalidRequestError("startDate cannot be after endDate.")
        if search_mode is SearchMode.FULL_ARCHIVE and not (start_date and end_date):
            raise InvalidRequestError("Full-archive search requires startDate and endDate.")
        for name in ("includeReplies", "mediaOnly"):
            if not isinstance(body.get(name, False), bool):
                raise InvalidRequestError(f"{name} must be a boolean.")
        return cls(
            source_type=source_type,
            source_value=source_value,
            provider=provider,
            search_mode=search_mode,
            max_posts=max_posts,
            start_date=start_date,
            end_date=end_date,
            include_replies=body.get("includeReplies", False),
            media_only=body.get("mediaOnly", False),
        )

    @classmethod
    def _browser_request(cls, body: dict[str, Any]) -> CollectionRequest:
        try:
            source_type = SourceType(str(body.get("sourceType", SourceType.HOME.value)))
        except ValueError as exc:
            raise InvalidRequestError("Browser capture supports only the Home feed.") from exc
        if source_type is not SourceType.HOME:
            raise InvalidRequestError("Browser capture supports only the Home feed.")
        source_value = body.get("sourceValue", "home")
        if source_value not in (None, "", "home"):
            raise InvalidRequestError("Browser capture source is fixed to the Home feed.")
        unsupported = sorted(
            name
            for name in ("searchMode", "startDate", "endDate", "includeReplies", "mediaOnly")
            if name in body
        )
        if unsupported:
            raise InvalidRequestError(
                f"Browser capture does not support: {', '.join(unsupported)}."
            )
        max_posts = body.get("maxPosts", 5)
        if isinstance(max_posts, bool) or not isinstance(max_posts, int):
            raise InvalidRequestError("maxPosts must be an integer.")
        if not 1 <= max_posts <= 25:
            raise InvalidRequestError("Browser maxPosts must be between 1 and 25.")
        return cls(
            provider=ProviderType.PLAYWRIGHT_BROWSER,
            source_type=SourceType.HOME,
            source_value="home",
            search_mode=None,
            max_posts=max_posts,
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "provider": self.provider.value,
            "sourceType": self.source_type.value,
            "sourceValue": self.source_value,
            "maxPosts": self.max_posts,
        }
        if self.provider is ProviderType.PLAYWRIGHT_BROWSER:
            return result
        return {
            **result,
            "searchMode": self.search_mode.value if self.search_mode else None,
            "startDate": self.start_date,
            "endDate": self.end_date,
            "includeReplies": self.include_replies,
            "mediaOnly": self.media_only,
        }


@dataclass(slots=True)
class Post:
    post_id: str
    text: str | None
    author_username: str | None
    url: str
    created_at: str | None
    author_id: str | None = None
    observed_at: str = field(default_factory=utc_now)
    language: str | None = None
    conversation_id: str | None = None
    in_reply_to_post_id: str | None = None
    like_count: int | None = None
    reply_count: int | None = None
    repost_count: int | None = None
    quote_count: int | None = None
    bookmark_count: int | None = None
    is_reply: bool | None = None
    is_repost: bool | None = None
    is_quote: bool | None = None
    has_media: bool | None = None
    media: list[dict[str, Any]] | None = None
    source_position: int | None = None


@dataclass(slots=True)
class CollectionSummary:
    warnings: list[str] = field(default_factory=list)
    completion_reason: str = "timeline_exhausted"
    partial: bool = False
