from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import RateLimitedError, SchemaDriftError, ScraperError, SessionExpiredError
from .models import CollectionRequest, Tweet
from .providers.playwright import (
    CURSOR_CONTEXT_VERSION,
    PROVIDER_ID,
    QUERY_COMPILER_VERSION,
    PlaywrightProvider,
    TimelinePageObservation,
    _build_search_query,
    _cursor_context,
    _walk,
    parse_timeline,
)
from .storage import Storage

REPORT_VERSION = "graphql-smoke-report/v1"
EXIT_PRECONDITION = 2
EXIT_SESSION = 3
EXIT_SEMANTIC = 4
EXIT_RATE_LIMIT = 5
KNOWN_INSTRUCTIONS = {
    "TimelineAddEntries",
    "TimelineReplaceEntry",
    "TimelinePinEntry",
    "TimelineTerminateTimeline",
    "TimelineClearCache",
}
SECRET_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "csrf",
    "ct0",
    "password",
    "storage-state",
    "token",
    "x-csrf-token",
}
CONTENT_KEYS = {
    "cursor",
    "expanded_url",
    "full_text",
    "id",
    "id_str",
    "media_url",
    "media_url_https",
    "rest_id",
    "screen_name",
    "text",
    "url",
    "value",
}
FIXTURE_SCALARS = {
    "__typename",
    "bookmark_count",
    "conversation_id_str",
    "created_at",
    "cursorType",
    "entryId",
    "expanded_url",
    "favorite_count",
    "full_text",
    "id",
    "id_str",
    "in_reply_to_status_id_str",
    "is_quote_status",
    "lang",
    "media_url",
    "media_url_https",
    "quote_count",
    "reply_count",
    "rest_id",
    "retweet_count",
    "screen_name",
    "text",
    "type",
    "value",
}
FIXTURE_CONTAINERS = {
    "content",
    "core",
    "entities",
    "entries",
    "entry",
    "extended_entities",
    "instructions",
    "itemContent",
    "legacy",
    "media",
    "note_tweet",
    "note_tweet_results",
    "result",
    "tweet",
    "tweet_results",
    "user_results",
}
FIXTURE_PRESENCE = {"promotedMetadata", "quoted_status_result", "retweeted_status_result"}
_DROP = object()


class SmokePreconditionError(Exception):
    pass


class SmokeSemanticError(Exception):
    pass


class SmokeRunError(Exception):
    def __init__(self, message: str, exit_code: int, report_path: Path | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.report_path = report_path


def validate_saved_state(path: Path) -> None:
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise SmokePreconditionError("No saved X session. Run: python -m xscraper auth") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise SmokePreconditionError("Saved X session must be a regular file owned by this user.")
    if not metadata.st_mode & stat.S_IRUSR:
        raise SmokePreconditionError("Saved X session is not owner-readable.")
    try:
        state = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokePreconditionError("Saved X session is not valid JSON.") from exc
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("cookies"), list)
        or not isinstance(state.get("origins"), list)
    ):
        raise SmokePreconditionError(
            "Saved X session does not have Playwright storage-state shape."
        )


def _secret_key(key: str) -> bool:
    normalized = key.casefold().replace("_", "-")
    return (
        normalized in SECRET_KEYS
        or "authorization" in normalized
        or normalized.endswith("-token")
        or normalized.endswith("-secret")
    )


def structure_tree(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: structure_tree(value[key])
            for key in sorted(value)
            if not _secret_key(key)
        }
    if isinstance(value, list):
        return [structure_tree(item) for item in value]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def structural_hash(value: Any) -> str:
    canonical = json.dumps(structure_tree(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class _FixtureProjector:
    def __init__(self) -> None:
        self.ids: dict[str, str] = {}
        self.cursors: dict[str, str] = {}

    @staticmethod
    def _mapped(mapping: dict[str, str], value: Any, prefix: str) -> str:
        raw = str(value)
        if raw not in mapping:
            mapping[raw] = f"{prefix}-{len(mapping) + 1}"
        return mapping[raw]

    def project(self, value: Any, key: str = "", parent: dict[str, Any] | None = None) -> Any:
        if isinstance(value, dict):
            return {
                child_key: self.project(child, child_key, value)
                for child_key, child in value.items()
                if not _secret_key(child_key)
            }
        if isinstance(value, list):
            return [self.project(child, key, parent) for child in value]
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return 1 if value >= 0 else 0
        if isinstance(value, float):
            return 1.0 if value >= 0 else 0.0

        raw = str(value)
        if key == "__typename":
            return raw if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", raw) else "Tweet"
        if key == "type" and raw in {*KNOWN_INSTRUCTIONS, "photo", "video", "animated_gif"}:
            return raw
        if key == "cursorType" and raw in {"Top", "Bottom", "ShowMore", "ShowMoreThreads"}:
            return raw
        if key in {"created_at", "createdAt"}:
            return "Tue Jun 02 14:29:55 +0000 2026"
        if key in {"screen_name", "username"}:
            return "fixture_handle"
        if key in {"full_text", "text"}:
            return "Fixture post text"
        if key == "lang":
            return "en"
        if key == "value" and parent and parent.get("cursorType"):
            return self._mapped(self.cursors, raw, "fixture-cursor")
        if key in {"media_url", "media_url_https", "expanded_url", "url"}:
            return "https://example.invalid/fixture"
        if "id" in key.casefold():
            return self._mapped(self.ids, raw, "fixture-id")
        return "fixture-value"


def _prune_fixture(value: Any, key: str = "") -> Any:
    if key in FIXTURE_PRESENCE:
        return {"present": True}
    if isinstance(value, dict):
        result = {}
        for child_key, child in value.items():
            if _secret_key(child_key):
                continue
            pruned = _prune_fixture(child, child_key)
            if pruned is not _DROP:
                result[child_key] = pruned
        return result if result or key in FIXTURE_CONTAINERS else _DROP
    if isinstance(value, list):
        items = [item for child in value if (item := _prune_fixture(child, key)) is not _DROP]
        return items if items or key in FIXTURE_CONTAINERS else _DROP
    return value if key in FIXTURE_SCALARS else _DROP


def semantic_fixture(payload: Any) -> Any:
    pruned = _prune_fixture(payload)
    return _FixtureProjector().project({} if pruned is _DROP else pruned)


def _original_content_strings(payload: Any) -> set[str]:
    values: set[str] = set()
    for key, value in _walk(payload):
        if key in CONTENT_KEYS and isinstance(value, str) and len(value) >= 4:
            values.add(value)
    return values


def validate_projection(payload: Any, fixture: Any, parsed_count: int, has_cursor: bool) -> None:
    projected_strings = {
        value for _, value in _walk(fixture) if isinstance(value, str)
    }
    leaked = sorted(_original_content_strings(payload).intersection(projected_strings))
    if leaked:
        raise SmokeSemanticError("Candidate fixture retained original scalar content.")
    projected_posts, projected_cursor = parse_timeline(fixture)
    if len(projected_posts) != parsed_count or bool(projected_cursor) != has_cursor:
        raise SmokeSemanticError("Candidate fixture did not preserve parser semantics.")


def _instruction_types(payload: Any) -> list[str]:
    result = {
        str(instruction.get("type"))
        for key, instructions in _walk(payload)
        if key == "instructions" and isinstance(instructions, list)
        for instruction in instructions
        if isinstance(instruction, dict) and instruction.get("type")
    }
    return sorted(result)


def _entry_count(payload: Any) -> int:
    return sum(
        len(entries)
        for key, entries in _walk(payload)
        if key == "entries" and isinstance(entries, list)
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def _safe_message(error: Exception, profile: str) -> str:
    if isinstance(error, SchemaDriftError):
        return "X timeline schema or GraphQL semantics changed."
    message = re.sub(re.escape(profile), "<target>", str(error), flags=re.IGNORECASE)
    message = re.sub(r"https?://\S+", "<url>", message)
    return re.sub(r"\b[A-Za-z0-9_=-]{40,}\b", "<redacted>", message)[:300]


def _assert_posts(posts: list[dict[str, Any]]) -> None:
    if not posts:
        raise SmokeSemanticError("Scenario returned no accepted posts.")
    metrics = ("like_count", "reply_count", "retweet_count", "quote_count", "bookmark_count")
    for post in posts:
        required = ("tweet_id", "author_username", "text", "url", "created_at")
        if any(not post.get(field) for field in required):
            raise SmokeSemanticError("An accepted post was missing required fields.")
        if any(not isinstance(post[field], int) or post[field] < 0 for field in metrics):
            raise SmokeSemanticError("An accepted post had invalid engagement metrics.")


def _assert_smoke_summary(summary: Any) -> None:
    if summary.completion_reason in {"no_progress", "cursor_stalled", "target_reached"}:
        raise SmokeSemanticError(
            f"Scenario ended with invalid reason: {summary.completion_reason}."
        )


def run_graphql_smoke(settings: Settings, profile: str) -> Path:
    validate_saved_state(settings.storage_state_path)
    validation_provider = PlaywrightProvider(settings)
    session = validation_provider.session_status()
    if not session.get("valid"):
        raise SmokeRunError(
            str(session.get("message") or "Saved X session is invalid."), EXIT_SESSION
        )

    started_at = datetime.now(UTC)
    run_id = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    report_dir = settings.runtime_dir / "smoke-reports" / run_id
    report_dir.mkdir(parents=True, mode=0o700)
    report: dict[str, Any] = {
        "schema": REPORT_VERSION,
        "runId": run_id,
        "startedAt": started_at.isoformat(),
        "targetHandleHash": hashlib.sha256(profile.casefold().encode()).hexdigest(),
        "versions": {
            "python": platform.python_version(),
            "playwright": importlib.metadata.version("playwright"),
            "browser": None,
            "provider": PROVIDER_ID,
            "cursorContract": CURSOR_CONTEXT_VERSION,
            "queryCompiler": QUERY_COMPILER_VERSION,
        },
        "redaction": {"passed": True, "originalScalarContentStored": False},
        "scenarios": [],
        "status": "running",
        "failure": None,
    }
    browser_versions: set[str] = set()

    def finish(status: str, error: Exception | None = None) -> Path:
        finished = datetime.now(UTC)
        report["status"] = status
        report["finishedAt"] = finished.isoformat()
        report["durationMs"] = round((finished - started_at).total_seconds() * 1_000)
        report["versions"]["browser"] = ", ".join(sorted(browser_versions)) or "unknown"
        if error:
            report["failure"] = {
                "classification": getattr(error, "code", "semantic_failure"),
                "message": _safe_message(error, profile),
            }
        path = report_dir / "report.json"
        _write_json(path, report)
        return path

    try:
        with tempfile.TemporaryDirectory(prefix="xscraper-smoke-") as temporary:
            temporary_path = Path(temporary)
            smoke_settings = replace(
                settings,
                database_path=temporary_path / "smoke.db",
                artifacts_dir=temporary_path / "artifacts",
                headless=True,
                enable_tracing=False,
                max_retries=1,
            )
            storage = Storage(smoke_settings.database_path)
            storage.initialize()
            fixture_index = 0

            def collect_page(
                name: str,
                request: CollectionRequest,
                job_id: str,
                cursor: str | None = None,
                cursor_context: dict[str, Any] | None = None,
            ) -> tuple[Any, str | None, dict[str, Any] | None, list[dict[str, Any]]]:
                nonlocal fixture_index
                page_records: list[dict[str, Any]] = []
                saved_context: dict[str, Any] | None = None

                def observe(observation: TimelinePageObservation) -> None:
                    nonlocal fixture_index
                    fixture_index += 1
                    tree = structure_tree(observation.raw_payload)
                    record = {
                        "operation": observation.operation,
                        "pageNumber": observation.page_number,
                        "durationMs": observation.duration_ms,
                        "instructionTypes": _instruction_types(observation.raw_payload),
                        "entryCount": _entry_count(observation.raw_payload),
                        "parsedCount": len(observation.posts),
                        "cursorPresent": bool(observation.cursor),
                        "structuralHash": structural_hash(observation.raw_payload),
                        "redactionPassed": True,
                    }
                    if observation.parse_error:
                        _write_json(
                            report_dir / "structures" / f"{fixture_index:02d}-{name}.json",
                            tree,
                        )
                    else:
                        fixture = semantic_fixture(observation.raw_payload)
                        validate_projection(
                            observation.raw_payload,
                            fixture,
                            len(observation.posts),
                            bool(observation.cursor),
                        )
                        _write_json(
                            report_dir / "candidate-fixtures" / f"{fixture_index:02d}-{name}.json",
                            fixture,
                        )
                    page_records.append(record)

                provider = PlaywrightProvider(
                    smoke_settings, _page_observer=observe, _page_limit=1
                )

                def on_batch(
                    tweets: list[Tweet],
                    next_cursor: str | None,
                    context: dict[str, Any],
                    raw_count: int,
                ) -> int:
                    nonlocal saved_context
                    saved_context = context
                    for tweet in tweets:
                        tweet.raw = None
                    return storage.add_tweets(
                        job_id,
                        tweets,
                        next_cursor,
                        cursor_context=context,
                        raw_posts_seen=raw_count,
                    )

                summary = provider.collect(
                    request,
                    cursor=cursor,
                    cursor_context=cursor_context,
                    on_batch=on_batch,
                    should_cancel=lambda: False,
                )
                if provider._browser_version:
                    browser_versions.add(provider._browser_version)
                return summary, summary.last_cursor, saved_context, page_records

            profile_request = CollectionRequest.from_dict(
                {"sourceType": "profile", "sourceValue": profile, "maxTweets": 500}
            )
            profile_job = storage.create_job(profile_request)
            first, first_cursor, first_context, first_pages = collect_page(
                "profile-initial", profile_request, profile_job
            )
            first_count = len(storage.get_job_tweets(profile_job, limit=500))
            if not first_cursor or first_context != _cursor_context(profile_request, "UserTweets"):
                raise SmokeSemanticError("Profile page one lacked a compatible bottom cursor.")
            second, second_cursor, second_context, second_pages = collect_page(
                "profile-resume",
                profile_request,
                profile_job,
                first_cursor,
                first_context,
            )
            profile_posts = storage.get_job_tweets(profile_job, limit=500)
            if len(profile_posts) <= first_count:
                raise SmokeSemanticError("Profile resume added no unique posts.")
            if second_cursor == first_cursor:
                raise SmokeSemanticError("Profile resume repeated its bottom cursor.")
            if second_context != first_context or any(post["is_reply"] for post in profile_posts):
                raise SmokeSemanticError("Profile resume changed context or retained replies.")
            profile_pages = [*first_pages, *second_pages]
            if len(profile_pages) != 2 or any(
                page["operation"] != "UserTweets" for page in profile_pages
            ):
                raise SmokeSemanticError("Profile smoke did not capture two UserTweets pages.")
            if any(
                summary.completion_reason in {"no_progress", "cursor_stalled", "target_reached"}
                for summary in (first, second)
            ):
                raise SmokeSemanticError("Profile pagination ended with an invalid reason.")
            _assert_posts(profile_posts)
            report["scenarios"].append(
                {
                    "name": "profile_initial_resume",
                    "status": "passed",
                    "operation": "UserTweets",
                    "pageCount": 2,
                    "pages": profile_pages,
                    "uniqueStoredCount": len(profile_posts),
                    "completionReason": second.completion_reason,
                    "cursorChangedOrExhausted": second_cursor != first_cursor,
                    "cursorContextMatched": second_context == first_context,
                }
            )

            replies_request = CollectionRequest.from_dict(
                {
                    "sourceType": "profile",
                    "sourceValue": profile,
                    "maxTweets": 500,
                    "includeReplies": True,
                }
            )
            replies_job = storage.create_job(replies_request)
            replies, _, replies_context, replies_pages = collect_page(
                "profile-replies", replies_request, replies_job
            )
            replies_posts = storage.get_job_tweets(replies_job, limit=500)
            _assert_posts(replies_posts)
            _assert_smoke_summary(replies)
            if not any(post["is_reply"] for post in replies_posts):
                raise SmokeSemanticError("Fixture reply was not detected.")
            if not any(post["is_quote"] for post in replies_posts):
                raise SmokeSemanticError("Fixture quote was not detected.")
            if replies_context != _cursor_context(replies_request, "UserTweetsAndReplies"):
                raise SmokeSemanticError("Replies cursor context was incompatible.")
            if len(replies_pages) != 1 or replies_pages[0]["operation"] != "UserTweetsAndReplies":
                raise SmokeSemanticError("Replies smoke captured the wrong operation.")
            report["scenarios"].append(
                {
                    "name": "profile_with_replies",
                    "status": "passed",
                    "operation": "UserTweetsAndReplies",
                    "pageCount": 1,
                    "pages": replies_pages,
                    "uniqueStoredCount": len(replies_posts),
                    "completionReason": replies.completion_reason,
                    "cursorContextMatched": True,
                }
            )

            end_date = datetime.now(UTC).date()
            start_date = end_date - timedelta(days=13)
            search_request = CollectionRequest.from_dict(
                {
                    "sourceType": "search",
                    "sourceValue": f"from:{profile}",
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "maxTweets": 500,
                }
            )
            search_job = storage.create_job(search_request)
            search, _, search_context, search_pages = collect_page(
                "bounded-search", search_request, search_job
            )
            search_posts = storage.get_job_tweets(search_job, limit=500)
            _assert_posts(search_posts)
            _assert_smoke_summary(search)
            if any(
                post["author_username"].casefold() != profile.casefold()
                for post in search_posts
            ):
                raise SmokeSemanticError("Bounded search returned another profile.")
            if any(
                not start_date <= datetime.fromisoformat(post["created_at"]).date() <= end_date
                for post in search_posts
            ):
                raise SmokeSemanticError("Bounded search returned an out-of-range date.")
            if search_context != _cursor_context(search_request, "SearchTimeline"):
                raise SmokeSemanticError("Bounded-search cursor context was incompatible.")
            expected_until = (end_date + timedelta(days=1)).isoformat()
            if f"until:{expected_until}" not in _build_search_query(search_request):
                raise SmokeSemanticError(
                    "Bounded search did not compile an exclusive next-day until."
                )
            report["scenarios"].append(
                {
                    "name": "bounded_search",
                    "status": "passed",
                    "operation": "SearchTimeline",
                    "pageCount": 1,
                    "pages": search_pages,
                    "uniqueStoredCount": len(search_posts),
                    "completionReason": search.completion_reason,
                    "inclusiveDatesMatched": True,
                    "compiledUntilIsFollowingDay": True,
                    "cursorContextMatched": True,
                }
            )

            media_request = CollectionRequest.from_dict(
                {
                    "sourceType": "search",
                    "sourceValue": f"from:{profile}",
                    "startDate": start_date.isoformat(),
                    "endDate": end_date.isoformat(),
                    "mediaOnly": True,
                    "maxTweets": 500,
                }
            )
            media_job = storage.create_job(media_request)
            media, _, media_context, media_pages = collect_page(
                "media-search", media_request, media_job
            )
            media_posts = storage.get_job_tweets(media_job, limit=500)
            _assert_posts(media_posts)
            _assert_smoke_summary(media)
            if any(not post["has_media"] for post in media_posts):
                raise SmokeSemanticError("Media search retained a text-only post.")
            if not any(
                item.get("type") == "photo" and item.get("url")
                for post in media_posts
                for item in post["media"]
            ):
                raise SmokeSemanticError("Fixture photo was not represented with type and URL.")
            if media_context != _cursor_context(media_request, "SearchTimeline"):
                raise SmokeSemanticError("Media-search cursor context was incompatible.")
            report["scenarios"].append(
                {
                    "name": "media_search",
                    "status": "passed",
                    "operation": "SearchTimeline",
                    "pageCount": 1,
                    "pages": media_pages,
                    "uniqueStoredCount": len(media_posts),
                    "completionReason": media.completion_reason,
                    "allResultsHaveMedia": True,
                    "cursorContextMatched": True,
                }
            )

            if fixture_index != 5:
                raise SmokeSemanticError("Smoke run did not observe exactly five payloads.")
        return finish("passed")
    except RateLimitedError as exc:
        path = finish("failed", exc)
        raise SmokeRunError(_safe_message(exc, profile), EXIT_RATE_LIMIT, path) from exc
    except SessionExpiredError as exc:
        path = finish("failed", exc)
        raise SmokeRunError(_safe_message(exc, profile), EXIT_SESSION, path) from exc
    except (SchemaDriftError, SmokeSemanticError) as exc:
        path = finish("failed", exc)
        raise SmokeRunError(_safe_message(exc, profile), EXIT_SEMANTIC, path) from exc
    except ScraperError as exc:
        path = finish("failed", exc)
        raise SmokeRunError(_safe_message(exc, profile), EXIT_SEMANTIC, path) from exc
    except Exception as exc:
        path = finish("failed", exc)
        raise SmokeRunError(
            "Unexpected smoke failure; see the sanitized report.", EXIT_SEMANTIC, path
        ) from exc
