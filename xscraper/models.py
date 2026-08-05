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
    PROFILE = "profile"
    SEARCH = "search"


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
            "sourceType",
            "sourceValue",
            "maxPosts",
            "startDate",
            "endDate",
            "includeReplies",
            "mediaOnly",
        }
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise InvalidRequestError(f"Unknown request field(s): {', '.join(unknown)}.")
        try:
            source_type = SourceType(str(body.get("sourceType", "")))
        except ValueError as exc:
            raise InvalidRequestError("sourceType must be 'profile' or 'search'.") from exc
        raw_value = body.get("sourceValue")
        if not isinstance(raw_value, str):
            raise InvalidRequestError("sourceValue must be a string.")
        source_value = unicodedata.normalize("NFC", raw_value.strip())
        if not source_value:
            raise InvalidRequestError("sourceValue is required.")
        if source_type is SourceType.PROFILE:
            source_value = normalize_profile(source_value)
        elif len(source_value) > 500:
            raise InvalidRequestError("Search query cannot exceed 500 characters.")
        max_posts = body.get("maxPosts", 25)
        if isinstance(max_posts, bool) or not isinstance(max_posts, int):
            raise InvalidRequestError("maxPosts must be an integer.")
        if not 10 <= max_posts <= 500:
            raise InvalidRequestError("maxPosts must be between 10 and 500.")
        start_date = _parse_date(body.get("startDate"), "startDate")
        end_date = _parse_date(body.get("endDate"), "endDate")
        if start_date and end_date and start_date > end_date:
            raise InvalidRequestError("startDate cannot be after endDate.")
        for name in ("includeReplies", "mediaOnly"):
            if not isinstance(body.get(name, False), bool):
                raise InvalidRequestError(f"{name} must be a boolean.")
        return cls(
            source_type=source_type,
            source_value=source_value,
            max_posts=max_posts,
            start_date=start_date,
            end_date=end_date,
            include_replies=body.get("includeReplies", False),
            media_only=body.get("mediaOnly", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceType": self.source_type.value,
            "sourceValue": self.source_value,
            "maxPosts": self.max_posts,
            "startDate": self.start_date,
            "endDate": self.end_date,
            "includeReplies": self.include_replies,
            "mediaOnly": self.media_only,
        }


@dataclass(slots=True)
class Post:
    post_id: str
    text: str
    author_username: str
    url: str
    created_at: str | None
    observed_at: str = field(default_factory=utc_now)
    language: str | None = None
    conversation_id: str | None = None
    in_reply_to_post_id: str | None = None
    like_count: int = 0
    reply_count: int = 0
    repost_count: int = 0
    quote_count: int = 0
    bookmark_count: int = 0
    is_reply: bool = False
    is_repost: bool = False
    is_quote: bool = False
    has_media: bool = False
    media: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class CollectionSummary:
    warnings: list[str] = field(default_factory=list)
    completion_reason: str = "timeline_exhausted"
    partial: bool = False
