from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import CollectionRequest, JobStatus, Tweet, utc_now

SCHEMA_VERSION = 1


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        try:
            with self.connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                version = int(row[0]) if row else 0
                if version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"Database schema {version} is newer than supported {SCHEMA_VERSION}."
                    )
                if version < 1:
                    self._migration_1(connection)
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"Cannot open database at {self.path}. The file was preserved for recovery."
            ) from exc

    @staticmethod
    def _migration_1(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tweets (
                tweet_id              TEXT PRIMARY KEY,
                text                  TEXT NOT NULL,
                author_username       TEXT NOT NULL,
                url                   TEXT NOT NULL,
                created_at            TEXT,
                scraped_at            TEXT NOT NULL,
                language              TEXT,
                conversation_id       TEXT,
                in_reply_to_tweet_id  TEXT,
                like_count            INTEGER NOT NULL DEFAULT 0,
                reply_count           INTEGER NOT NULL DEFAULT 0,
                retweet_count         INTEGER NOT NULL DEFAULT 0,
                quote_count           INTEGER NOT NULL DEFAULT 0,
                bookmark_count        INTEGER NOT NULL DEFAULT 0,
                is_reply              INTEGER NOT NULL DEFAULT 0,
                is_retweet            INTEGER NOT NULL DEFAULT 0,
                is_quote              INTEGER NOT NULL DEFAULT 0,
                has_media             INTEGER NOT NULL DEFAULT 0,
                media_json            TEXT NOT NULL DEFAULT '[]',
                raw_json              TEXT
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id                    TEXT PRIMARY KEY,
                request_json          TEXT NOT NULL,
                source_type           TEXT NOT NULL,
                source_value          TEXT NOT NULL,
                status                TEXT NOT NULL,
                target_count          INTEGER NOT NULL,
                collected_count       INTEGER NOT NULL DEFAULT 0,
                cursor                TEXT,
                warnings_json         TEXT NOT NULL DEFAULT '[]',
                error_code            TEXT,
                error_message         TEXT,
                error_retryable       INTEGER NOT NULL DEFAULT 0,
                cancel_requested      INTEGER NOT NULL DEFAULT 0,
                created_at            TEXT NOT NULL,
                started_at            TEXT,
                finished_at           TEXT,
                updated_at            TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_tweets (
                job_id                TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                tweet_id              TEXT NOT NULL REFERENCES tweets(tweet_id) ON DELETE CASCADE,
                position              INTEGER NOT NULL,
                PRIMARY KEY (job_id, tweet_id),
                UNIQUE (job_id, position)
            );

            CREATE TABLE IF NOT EXISTS tweet_enrichments (
                tweet_id              TEXT PRIMARY KEY
                                      REFERENCES tweets(tweet_id) ON DELETE CASCADE,
                sentiment_label       TEXT NOT NULL,
                sentiment_score       REAL NOT NULL,
                analyzer              TEXT NOT NULL,
                analyzer_version      TEXT NOT NULL,
                analyzed_at           TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_job_tweets_position ON job_tweets(job_id, position);
            """
        )
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def create_job(self, request: CollectionRequest) -> str:
        job_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, request_json, source_type, source_value, status,
                    target_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    json.dumps(request.to_dict(), separators=(",", ":")),
                    request.source_type.value,
                    request.source_value,
                    JobStatus.QUEUED.value,
                    request.max_tweets,
                    now,
                    now,
                ),
            )
        return job_id

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
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

    def set_running(self, job_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = ?, started_at = COALESCE(started_at, ?),
                    finished_at = NULL, error_code = NULL, error_message = NULL,
                    error_retryable = 0, cancel_requested = 0, updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.RUNNING.value, now, now, job_id),
            )

    def add_tweets(self, job_id: str, tweets: Iterable[Tweet], cursor: str | None) -> int:
        tweets = list(tweets)
        with self.connect() as connection:
            current = connection.execute(
                "SELECT collected_count FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if current is None:
                return 0
            position = int(current[0])
            added = 0
            for tweet in tweets:
                connection.execute(
                    """
                    INSERT INTO tweets (
                        tweet_id, text, author_username, url, created_at, scraped_at,
                        language, conversation_id, in_reply_to_tweet_id,
                        like_count, reply_count, retweet_count, quote_count, bookmark_count,
                        is_reply, is_retweet, is_quote, has_media, media_json, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tweet_id) DO UPDATE SET
                        text = excluded.text,
                        author_username = excluded.author_username,
                        url = excluded.url,
                        created_at = COALESCE(excluded.created_at, tweets.created_at),
                        scraped_at = excluded.scraped_at,
                        language = COALESCE(excluded.language, tweets.language),
                        conversation_id = COALESCE(
                            excluded.conversation_id, tweets.conversation_id
                        ),
                        in_reply_to_tweet_id = COALESCE(
                            excluded.in_reply_to_tweet_id, tweets.in_reply_to_tweet_id
                        ),
                        like_count = excluded.like_count,
                        reply_count = excluded.reply_count,
                        retweet_count = excluded.retweet_count,
                        quote_count = excluded.quote_count,
                        bookmark_count = excluded.bookmark_count,
                        is_reply = excluded.is_reply,
                        is_retweet = excluded.is_retweet,
                        is_quote = excluded.is_quote,
                        has_media = excluded.has_media,
                        media_json = excluded.media_json,
                        raw_json = excluded.raw_json
                    """,
                    (
                        tweet.tweet_id,
                        tweet.text,
                        tweet.author_username,
                        tweet.url,
                        tweet.created_at,
                        tweet.scraped_at,
                        tweet.language,
                        tweet.conversation_id,
                        tweet.in_reply_to_tweet_id,
                        tweet.like_count,
                        tweet.reply_count,
                        tweet.retweet_count,
                        tweet.quote_count,
                        tweet.bookmark_count,
                        int(tweet.is_reply),
                        int(tweet.is_retweet),
                        int(tweet.is_quote),
                        int(tweet.has_media),
                        json.dumps(tweet.media, separators=(",", ":")),
                        json.dumps(tweet.raw, separators=(",", ":")) if tweet.raw else None,
                    ),
                )
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO job_tweets(job_id, tweet_id, position) VALUES (?, ?, ?)",
                    (job_id, tweet.tweet_id, position),
                ).rowcount
                if inserted:
                    position += 1
                    added += 1
            connection.execute(
                """
                UPDATE jobs SET collected_count = ?, cursor = ?, updated_at = ?
                WHERE id = ?
                """,
                (position, cursor, utc_now(), job_id),
            )
        return added

    def save_enrichment(
        self,
        tweet_id: str,
        label: str,
        score: float,
        analyzer: str,
        analyzer_version: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tweet_enrichments VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id) DO UPDATE SET
                    sentiment_label = excluded.sentiment_label,
                    sentiment_score = excluded.sentiment_score,
                    analyzer = excluded.analyzer,
                    analyzer_version = excluded.analyzer_version,
                    analyzed_at = excluded.analyzed_at
                """,
                (tweet_id, label, score, analyzer, analyzer_version, utc_now()),
            )

    def finish_job(self, job_id: str, warnings: list[str]) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = ?, warnings_json = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.SUCCEEDED.value, json.dumps(warnings), now, now, job_id),
            )

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
                    error_retryable = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, code, message, int(retryable), now, now, job_id),
            )

    def request_cancel(self, job_id: str) -> bool:
        with self.connect() as connection:
            changed = connection.execute(
                """
                UPDATE jobs SET cancel_requested = 1, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (utc_now(), job_id),
            ).rowcount
        return bool(changed)

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
                    finished_at = NULL, updated_at = ?
                WHERE id = ? AND status IN ('failed', 'cancelled', 'interrupted')
                """,
                (utc_now(), job_id),
            ).rowcount
        return bool(changed)

    def recover_jobs(self) -> list[str]:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'queued', error_code = 'interrupted',
                    error_message = 'Server restarted; job queued for automatic resume.',
                    error_retryable = 1, cancel_requested = 0, updated_at = ?
                WHERE status = 'running'
                """,
                (utc_now(),),
            )
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at"
            ).fetchall()
        return [row[0] for row in rows]

    def get_job_tweets(
        self, job_id: str, *, limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT t.*, e.sentiment_label, e.sentiment_score,
                       e.analyzer, e.analyzer_version
                FROM job_tweets jt
                JOIN tweets t ON t.tweet_id = jt.tweet_id
                LEFT JOIN tweet_enrichments e ON e.tweet_id = t.tweet_id
                WHERE jt.job_id = ? ORDER BY jt.position LIMIT ? OFFSET ?
                """,
                (job_id, limit, offset),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["media"] = json.loads(item.pop("media_json"))
            item.pop("raw_json", None)
            for name in ("is_reply", "is_retweet", "is_quote", "has_media"):
                item[name] = bool(item[name])
            result.append(item)
        return result
