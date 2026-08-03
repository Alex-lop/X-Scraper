from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
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
    max_tweets: int = 25
    start_date: str | None = None
    end_date: str | None = None
    include_replies: bool = False
    media_only: bool = False
    analyze_sentiment: bool = False

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> CollectionRequest:
        if not isinstance(body, dict):
            raise InvalidRequestError("Request body must be a JSON object.")
        allowed_fields = {
            "sourceType",
            "sourceValue",
            "maxTweets",
            "startDate",
            "endDate",
            "includeReplies",
            "mediaOnly",
            "analyzeSentiment",
        }
        unknown_fields = sorted(set(body) - allowed_fields)
        if unknown_fields:
            raise InvalidRequestError(
                f"Unknown request field(s): {', '.join(unknown_fields)}."
            )
        try:
            source_type = SourceType(str(body.get("sourceType", "")))
        except ValueError as exc:
            raise InvalidRequestError("sourceType must be 'profile' or 'search'.") from exc

        raw_source_value = body.get("sourceValue")
        if not isinstance(raw_source_value, str):
            raise InvalidRequestError("sourceValue must be a string.")
        source_value = unicodedata.normalize("NFC", raw_source_value.strip())
        if not source_value:
            raise InvalidRequestError("sourceValue is required.")
        if source_type is SourceType.PROFILE:
            source_value = normalize_profile(source_value)
        elif len(source_value) > 500:
            raise InvalidRequestError("Search query cannot exceed 500 characters.")

        max_tweets = body.get("maxTweets", 25)
        if isinstance(max_tweets, bool) or not isinstance(max_tweets, int):
            raise InvalidRequestError("maxTweets must be an integer.")
        if not 1 <= max_tweets <= 500:
            raise InvalidRequestError("maxTweets must be between 1 and 500.")

        start_date = _parse_date(body.get("startDate"), "startDate")
        end_date = _parse_date(body.get("endDate"), "endDate")
        if start_date and end_date and start_date > end_date:
            raise InvalidRequestError("startDate cannot be after endDate.")

        boolean_fields = {
            "includeReplies": body.get("includeReplies", False),
            "mediaOnly": body.get("mediaOnly", False),
            "analyzeSentiment": body.get("analyzeSentiment", False),
        }
        for field_name, value in boolean_fields.items():
            if not isinstance(value, bool):
                raise InvalidRequestError(f"{field_name} must be a boolean.")

        return cls(
            source_type=source_type,
            source_value=source_value,
            max_tweets=max_tweets,
            start_date=start_date,
            end_date=end_date,
            include_replies=boolean_fields["includeReplies"],
            media_only=boolean_fields["mediaOnly"],
            analyze_sentiment=boolean_fields["analyzeSentiment"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceType": self.source_type.value,
            "sourceValue": self.source_value,
            "maxTweets": self.max_tweets,
            "startDate": self.start_date,
            "endDate": self.end_date,
            "includeReplies": self.include_replies,
            "mediaOnly": self.media_only,
            "analyzeSentiment": self.analyze_sentiment,
        }

    def fingerprint(
        self, *, include_limit: bool = True, include_sentiment: bool = True
    ) -> str:
        canonical = {
            "version": 1,
            "provider": "x_web_playwright",
            "sourceType": self.source_type.value,
            "sourceValue": (
                self.source_value.casefold()
                if self.source_type is SourceType.PROFILE
                else self.source_value
            ),
            "startDate": self.start_date,
            "endDate": self.end_date,
            "includeReplies": self.include_replies,
            "mediaOnly": self.media_only,
        }
        if include_limit:
            canonical["maxTweets"] = self.max_tweets
        if include_sentiment:
            canonical["analyzeSentiment"] = self.analyze_sentiment
        serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Tweet:
    tweet_id: str
    text: str
    author_username: str
    url: str
    created_at: str | None
    scraped_at: str = field(default_factory=utc_now)
    language: str | None = None
    conversation_id: str | None = None
    in_reply_to_tweet_id: str | None = None
    like_count: int = 0
    reply_count: int = 0
    retweet_count: int = 0
    quote_count: int = 0
    bookmark_count: int = 0
    is_reply: bool = False
    is_retweet: bool = False
    is_quote: bool = False
    has_media: bool = False
    media: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        result = asdict(self)
        if not include_raw:
            result.pop("raw", None)
        return result


@dataclass(slots=True)
class CollectionSummary:
    warnings: list[str] = field(default_factory=list)
    last_cursor: str | None = None
    completion_reason: str = "timeline_exhausted"
    partial: bool = False
