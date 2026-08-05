from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import CollectionRequest, JobStatus, Post, utc_now

SCHEMA_FAMILY = "official_x_api_mvp"
SCHEMA_VERSION = "1"


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        try:
            with self.connect() as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                }
                if tables:
                    metadata = (
                        {
                            row[0]: row[1]
                            for row in connection.execute("SELECT key, value FROM schema_meta")
                        }
                        if "schema_meta" in tables
                        else {}
                    )
                    if metadata.get("schema_family") != SCHEMA_FAMILY:
                        raise RuntimeError(
                            "This database uses the retired scraper schema. Point "
                            "XSCRAPER_DB_PATH at a new file to use the official X API MVP."
                        )
                    if metadata.get("schema_version") != SCHEMA_VERSION:
                        raise RuntimeError("This database schema version is not supported.")
                connection.execute("PRAGMA journal_mode = WAL")
                self._create_schema(connection)
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"Cannot open database at {self.path}. The file was preserved for recovery."
            ) from exc

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            INSERT OR REPLACE INTO schema_meta(key, value)
            VALUES ('schema_family', 'official_x_api_mvp');
            INSERT OR REPLACE INTO schema_meta(key, value)
            VALUES ('schema_version', '1');

            CREATE TABLE IF NOT EXISTS jobs (
                id                    TEXT PRIMARY KEY,
                request_json          TEXT NOT NULL,
                compiled_request_json TEXT NOT NULL,
                request_fingerprint   TEXT NOT NULL,
                account_scope         TEXT NOT NULL,
                status                TEXT NOT NULL,
                collected_count       INTEGER NOT NULL DEFAULT 0,
                cursor                TEXT,
                warnings_json         TEXT NOT NULL DEFAULT '[]',
                error_code            TEXT,
                error_message         TEXT,
                error_retryable       INTEGER NOT NULL DEFAULT 0,
                cancel_requested      INTEGER NOT NULL DEFAULT 0,
                completion_reason     TEXT,
                retry_at              TEXT,
                rate_limit_remaining  INTEGER,
                rate_limit_reset      INTEGER,
                billable_read_count   INTEGER NOT NULL DEFAULT 0,
                created_at            TEXT NOT NULL,
                started_at            TEXT,
                finished_at           TEXT,
                updated_at            TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS post_observations (
                job_id                TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                post_id               TEXT NOT NULL,
                position              INTEGER NOT NULL,
                text                  TEXT NOT NULL,
                author_username       TEXT NOT NULL,
                url                   TEXT NOT NULL,
                created_at            TEXT,
                observed_at           TEXT NOT NULL,
                language              TEXT,
                conversation_id       TEXT,
                in_reply_to_post_id   TEXT,
                like_count            INTEGER NOT NULL DEFAULT 0,
                reply_count           INTEGER NOT NULL DEFAULT 0,
                repost_count          INTEGER NOT NULL DEFAULT 0,
                quote_count           INTEGER NOT NULL DEFAULT 0,
                bookmark_count        INTEGER NOT NULL DEFAULT 0,
                is_reply              INTEGER NOT NULL DEFAULT 0,
                is_repost             INTEGER NOT NULL DEFAULT 0,
                is_quote              INTEGER NOT NULL DEFAULT 0,
                has_media             INTEGER NOT NULL DEFAULT 0,
                media_json            TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (job_id, post_id),
                UNIQUE (job_id, position)
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status_retry
                ON jobs(status, retry_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_cache
                ON jobs(account_scope, request_fingerprint, finished_at DESC);
            CREATE INDEX IF NOT EXISTS idx_observations_position
                ON post_observations(job_id, position);
            """
        )

    def create_job(self, request: CollectionRequest, compiled_request: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, request_json, compiled_request_json, request_fingerprint,
                    account_scope, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    json.dumps(request.to_dict(), separators=(",", ":")),
                    json.dumps(compiled_request, separators=(",", ":")),
                    compiled_request["requestFingerprint"],
                    compiled_request["accountScope"],
                    JobStatus.QUEUED.value,
                    now,
                    now,
                ),
            )
        return job_id

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        item["compiled_request"] = json.loads(item.pop("compiled_request_json"))
        item["warnings"] = json.loads(item.pop("warnings_json"))
        item["error_retryable"] = bool(item["error_retryable"])
        item["cancel_requested"] = bool(item["cancel_requested"])
        return item

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job_dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._job_dict(row) for row in rows]

    def claim_job(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?),
                    finished_at = NULL, retry_at = NULL, error_code = NULL,
                    error_message = NULL, error_retryable = 0,
                    completion_reason = NULL, updated_at = ?
                WHERE id = ? AND status = ? AND cancel_requested = 0
                """,
                (JobStatus.RUNNING.value, now, now, job_id, JobStatus.QUEUED.value),
            ).rowcount
            row = (
                connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if changed
                else None
            )
        return self._job_dict(row) if row else None

    def add_posts(
        self,
        job_id: str,
        posts: Iterable[Post],
        cursor: str | None,
        page_stats: dict[str, Any],
    ) -> int:
        posts = list(posts)
        with self.connect() as connection:
            current = connection.execute(
                "SELECT collected_count FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if current is None:
                return 0
            position = int(current[0])
            added = 0
            for post in posts:
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO post_observations (
                        job_id, post_id, position, text, author_username, url,
                        created_at, observed_at, language, conversation_id,
                        in_reply_to_post_id, like_count, reply_count, repost_count,
                        quote_count, bookmark_count, is_reply, is_repost, is_quote,
                        has_media, media_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        post.post_id,
                        position,
                        post.text,
                        post.author_username,
                        post.url,
                        post.created_at,
                        post.observed_at,
                        post.language,
                        post.conversation_id,
                        post.in_reply_to_post_id,
                        post.like_count,
                        post.reply_count,
                        post.repost_count,
                        post.quote_count,
                        post.bookmark_count,
                        int(post.is_reply),
                        int(post.is_repost),
                        int(post.is_quote),
                        int(post.has_media),
                        json.dumps(post.media, separators=(",", ":")),
                    ),
                ).rowcount
                if inserted:
                    position += 1
                    added += 1
            connection.execute(
                """
                UPDATE jobs SET collected_count = ?, cursor = ?,
                    billable_read_count = billable_read_count + ?,
                    rate_limit_remaining = COALESCE(?, rate_limit_remaining),
                    rate_limit_reset = COALESCE(?, rate_limit_reset), updated_at = ?
                WHERE id = ?
                """,
                (
                    position,
                    cursor,
                    int(page_stats.get("billableReads") or 0),
                    page_stats.get("rateLimitRemaining"),
                    page_stats.get("rateLimitReset"),
                    utc_now(),
                    job_id,
                ),
            )
        return added

    def finish_job(
        self,
        job_id: str,
        warnings: list[str],
        *,
        completion_reason: str,
        partial: bool = False,
    ) -> str | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row or row["status"] != JobStatus.RUNNING.value:
                return None
            if row["cancel_requested"]:
                connection.execute(
                    """
                    UPDATE jobs SET status = ?, error_code = 'cancelled',
                        error_message = 'Collection cancelled.', error_retryable = 1,
                        completion_reason = 'cancelled', finished_at = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (JobStatus.CANCELLED.value, now, now, job_id, JobStatus.RUNNING.value),
                )
                return JobStatus.CANCELLED.value
            status = JobStatus.PARTIAL if partial else JobStatus.SUCCEEDED
            changed = connection.execute(
                """
                UPDATE jobs SET status = ?, warnings_json = ?, completion_reason = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status = ? AND cancel_requested = 0
                """,
                (
                    status.value,
                    json.dumps(warnings),
                    completion_reason,
                    now,
                    now,
                    job_id,
                    JobStatus.RUNNING.value,
                ),
            ).rowcount
        return status.value if changed else None

    def fail_job(
        self,
        job_id: str,
        status: JobStatus,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = ?, error_code = ?, error_message = ?,
                    error_retryable = ?, completion_reason = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running', 'waiting')
                """,
                (status.value, code, message, int(retryable), code, now, now, job_id),
            )

    def wait_job(
        self,
        job_id: str,
        retry_at: str,
        remaining: int | None,
        reset: int | None,
        message: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'waiting', retry_at = ?,
                    rate_limit_remaining = ?, rate_limit_reset = ?,
                    error_code = 'rate_limited', error_message = ?, error_retryable = 1,
                    completion_reason = 'rate_limited', updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (retry_at, remaining, reset, message, utc_now(), job_id),
            )

    def request_cancel(self, job_id: str) -> bool:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            queued = connection.execute(
                """
                UPDATE jobs SET status = 'cancelled', cancel_requested = 1,
                    error_code = 'cancelled', error_message = 'Collection cancelled.',
                    error_retryable = 1, completion_reason = 'cancelled',
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'waiting')
                """,
                (now, now, job_id),
            ).rowcount
            running = connection.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (now, job_id),
            ).rowcount
        return bool(queued or running)

    def cancel_requested(self, job_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and row[0])

    def resume_job(self, job_id: str) -> bool:
        with self.connect() as connection:
            changed = connection.execute(
                """
                UPDATE jobs SET status = 'queued', cancel_requested = 0,
                    error_code = NULL, error_message = NULL, error_retryable = 0,
                    completion_reason = NULL, finished_at = NULL, retry_at = NULL,
                    updated_at = ?
                WHERE id = ? AND (
                    status IN ('cancelled', 'interrupted', 'partial')
                    OR (status = 'failed' AND error_retryable = 1)
                )
                """,
                (utc_now(), job_id),
            ).rowcount
        return bool(changed)

    def recover_jobs(self) -> list[str]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'cancelled', error_code = 'cancelled',
                    error_message = 'Collection cancellation was preserved across restart.',
                    error_retryable = 1, completion_reason = 'cancelled',
                    finished_at = ?, updated_at = ?
                WHERE status = 'running' AND cancel_requested = 1
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE jobs SET status = 'queued', error_code = 'interrupted',
                    error_message = 'Server restarted; job queued for automatic resume.',
                    error_retryable = 1, cancel_requested = 0,
                    completion_reason = 'interrupted', updated_at = ?
                WHERE status = 'running' AND cancel_requested = 0
                """,
                (now,),
            )
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at"
            ).fetchall()
        return [row[0] for row in rows]

    def requeue_due_jobs(self) -> list[str]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id FROM jobs
                WHERE status = 'waiting' AND retry_at IS NOT NULL AND retry_at <= ?
                ORDER BY retry_at
                """,
                (now,),
            ).fetchall()
            ids = [row[0] for row in rows]
            connection.executemany(
                """
                UPDATE jobs SET status = 'queued', retry_at = NULL,
                    error_code = NULL, error_message = NULL, updated_at = ?
                WHERE id = ? AND status = 'waiting'
                """,
                [(now, job_id) for job_id in ids],
            )
        return ids

    def find_cached_job(
        self, request_fingerprint: str, account_scope: str, *, cutoff: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'succeeded' AND request_fingerprint = ? AND account_scope = ?
                  AND finished_at IS NOT NULL AND finished_at >= ?
                ORDER BY finished_at DESC LIMIT 1
                """,
                (request_fingerprint, account_scope, cutoff),
            ).fetchone()
        return self._job_dict(row) if row else None

    def get_job_posts(
        self, job_id: str, *, limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM post_observations WHERE job_id = ? "
                "ORDER BY position LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("job_id")
            item.pop("position")
            item["media"] = json.loads(item.pop("media_json"))
            for name in ("is_reply", "is_repost", "is_quote", "has_media"):
                item[name] = bool(item[name])
            item["tweet_id"] = item.pop("post_id")
            item["scraped_at"] = item.pop("observed_at")
            item["in_reply_to_tweet_id"] = item.pop("in_reply_to_post_id")
            item["retweet_count"] = item.pop("repost_count")
            item["is_retweet"] = item.pop("is_repost")
            result.append(item)
        return result

    def count_job_posts(self, job_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM post_observations WHERE job_id = ?", (job_id,)
            ).fetchone()
        return int(row[0]) if row else 0
