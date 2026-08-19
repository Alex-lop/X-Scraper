from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import CollectionRequest, JobStatus, Post, ProviderType, utc_now

SCHEMA_FAMILY = "x_collection_workbench"
SCHEMA_VERSION = "3"
SCHEMA_TABLES = {"schema_meta", "jobs", "post_observations"}
SCHEMA_COLUMNS = {
    "schema_meta": {"key", "value"},
    "jobs": {
        "id",
        "request_json",
        "compiled_request_json",
        "status",
        "collected_count",
        "cursor",
        "warnings_json",
        "error_code",
        "error_message",
        "error_retryable",
        "cancel_requested",
        "completion_reason",
        "retry_at",
        "rate_limit_remaining",
        "rate_limit_reset",
        "post_resource_count",
        "user_resource_count",
        "media_resource_count",
        "created_at",
        "started_at",
        "finished_at",
        "updated_at",
        "capture_segment",
    },
    "post_observations": {
        "job_id",
        "post_id",
        "snapshot_position",
        "capture_segment",
        "scan_ordinal",
        "dom_position",
        "text",
        "author_id",
        "author_username",
        "url",
        "created_at",
        "observed_at",
        "language",
        "conversation_id",
        "in_reply_to_post_id",
        "like_count",
        "reply_count",
        "repost_count",
        "quote_count",
        "bookmark_count",
        "view_count",
        "is_reply",
        "is_repost",
        "is_quote",
        "has_media",
        "media_json",
    },
}
LEGACY_OBSERVATION_COLUMNS = (
    "job_id",
    "post_id",
    "position",
    "text",
    "author_id",
    "author_username",
    "url",
    "created_at",
    "observed_at",
    "language",
    "conversation_id",
    "in_reply_to_post_id",
    "like_count",
    "reply_count",
    "repost_count",
    "quote_count",
    "bookmark_count",
    "is_reply",
    "is_repost",
    "is_quote",
    "has_media",
    "media_json",
)


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        try:
            details = self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise RuntimeError(f"Database path is not a regular file: {self.path}")
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            self.path.chmod(0o600)
        except OSError:
            connection.close()
            raise
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
                    if tables != SCHEMA_TABLES:
                        raise RuntimeError(self._incompatible_message())
                    metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))
                    if metadata.get("schema_family") != SCHEMA_FAMILY or set(metadata) != {
                        "schema_family",
                        "schema_version",
                    }:
                        raise RuntimeError(self._incompatible_message())
                    version = metadata["schema_version"]
                    if version in {"1", "2"} and self._schema_is_compatible(
                        connection, version=int(version)
                    ):
                        self._backup_before_migration(connection, version=version)
                        self._migrate_to_v3(connection)
                    elif version != SCHEMA_VERSION or not self._schema_is_compatible(
                        connection, version=3
                    ):
                        raise RuntimeError(self._incompatible_message())
                else:
                    self._create_schema(connection)
                connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(
                f"Cannot open database at {self.path}. The file was preserved for recovery."
            ) from exc

    def _incompatible_message(self) -> str:
        return (
            f"Database at {self.path} is not a {SCHEMA_FAMILY} v{SCHEMA_VERSION} database. "
            "Export it with its original release or use a new database path; it was not changed."
        )

    @staticmethod
    def _schema_is_compatible(connection: sqlite3.Connection, *, version: int = 3) -> bool:
        if version not in {1, 2, 3}:
            return False
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('view', 'trigger') LIMIT 1"
        ).fetchone():
            return False
        expected_columns = {name: set(columns) for name, columns in SCHEMA_COLUMNS.items()}
        if version < 3:
            expected_columns["jobs"].remove("capture_segment")
            expected_columns["post_observations"] = set(LEGACY_OBSERVATION_COLUMNS)
        details = {}
        for table, expected in expected_columns.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            details[table] = {row["name"]: row for row in rows}
            if set(details[table]) != expected:
                return False
        expected_primary_keys = {
            "schema_meta": ["key"],
            "jobs": ["id"],
            "post_observations": ["job_id", "post_id"],
        }
        for table, expected in expected_primary_keys.items():
            primary_key = [
                row["name"]
                for row in sorted(details[table].values(), key=lambda item: item["pk"])
                if row["pk"]
            ]
            if primary_key != expected:
                return False
        observations = details["post_observations"]
        metrics = {
            "like_count",
            "reply_count",
            "repost_count",
            "quote_count",
            "bookmark_count",
        }
        if version == 3:
            metrics.add("view_count")
        if any(observations[name]["notnull"] for name in metrics):
            return False
        newly_nullable = {
            "text",
            "is_reply",
            "is_repost",
            "is_quote",
            "has_media",
            "media_json",
        }
        if any(bool(observations[name]["notnull"]) == (version >= 2) for name in newly_nullable):
            return False
        position = "snapshot_position" if version == 3 else "position"
        indexes: dict[str, tuple[bool, list[str]]] = {}
        for table in ("jobs", "post_observations"):
            for index in connection.execute(f"PRAGMA index_list({table})"):
                columns = [
                    row["name"]
                    for row in connection.execute(f"PRAGMA index_info({index['name']})")
                ]
                indexes[index["name"]] = (bool(index["unique"]), columns)
        if indexes.get("idx_jobs_status_retry") != (False, ["status", "retry_at"]):
            return False
        if indexes.get("idx_observations_position") != (False, ["job_id", position]):
            return False
        if not any(
            unique and columns == ["job_id", position]
            for unique, columns in indexes.values()
        ):
            return False
        foreign_keys = connection.execute("PRAGMA foreign_key_list(post_observations)").fetchall()
        return len(foreign_keys) == 1 and any(
            row["table"] == "jobs"
            and row["from"] == "job_id"
            and row["to"] == "id"
            and row["on_delete"].upper() == "CASCADE"
            for row in foreign_keys
        )

    def _backup_before_migration(
        self, connection: sqlite3.Connection, *, version: str
    ) -> None:
        backup_path = self.path.with_name(f"{self.path.name}.pre-v{version}-to-v3.bak")
        try:
            backup_path.lstat()
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError(f"Migration backup already exists; migration stopped: {backup_path}")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{backup_path.name}.", dir=backup_path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.chmod(0o600)
            with sqlite3.connect(temporary) as backup:
                connection.backup(backup)
                integrity = backup.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise RuntimeError("Migration backup failed its SQLite integrity check.")
            temporary.replace(backup_path)
            backup_path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _create_observations(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE post_observations (
                job_id                TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                post_id               TEXT NOT NULL,
                snapshot_position     INTEGER NOT NULL,
                capture_segment       INTEGER,
                scan_ordinal          INTEGER,
                dom_position          INTEGER,
                text                  TEXT,
                author_id             TEXT,
                author_username       TEXT,
                url                   TEXT NOT NULL,
                created_at            TEXT,
                observed_at           TEXT NOT NULL,
                language              TEXT,
                conversation_id       TEXT,
                in_reply_to_post_id   TEXT,
                like_count            INTEGER,
                reply_count           INTEGER,
                repost_count          INTEGER,
                quote_count           INTEGER,
                bookmark_count        INTEGER,
                view_count            INTEGER,
                is_reply              INTEGER,
                is_repost             INTEGER,
                is_quote              INTEGER,
                has_media             INTEGER,
                media_json            TEXT,
                PRIMARY KEY (job_id, post_id),
                UNIQUE (job_id, snapshot_position)
            )
            """
        )

    def _migrate_to_v3(self, connection: sqlite3.Connection) -> None:
        legacy_columns = ", ".join(LEGACY_OBSERVATION_COLUMNS)
        migrated_columns = legacy_columns.replace("position", "snapshot_position", 1)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE jobs ADD COLUMN capture_segment INTEGER")
            connection.execute("ALTER TABLE post_observations RENAME TO post_observations_legacy")
            self._create_observations(connection)
            connection.execute(
                f"INSERT INTO post_observations ({migrated_columns}) "
                f"SELECT {legacy_columns} FROM post_observations_legacy"
            )
            connection.execute("DROP TABLE post_observations_legacy")
            connection.execute(
                "CREATE INDEX idx_observations_position "
                "ON post_observations(job_id, snapshot_position)"
            )
            connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (SCHEMA_VERSION,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES
                ('schema_family', 'x_collection_workbench'),
                ('schema_version', '3');

            CREATE TABLE jobs (
                id                    TEXT PRIMARY KEY,
                request_json          TEXT NOT NULL,
                compiled_request_json TEXT NOT NULL,
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
                post_resource_count   INTEGER NOT NULL DEFAULT 0,
                user_resource_count   INTEGER NOT NULL DEFAULT 0,
                media_resource_count  INTEGER NOT NULL DEFAULT 0,
                capture_segment       INTEGER,
                created_at            TEXT NOT NULL,
                started_at            TEXT,
                finished_at           TEXT,
                updated_at            TEXT NOT NULL
            );

            CREATE INDEX idx_jobs_status_retry ON jobs(status, retry_at);
            """
        )
        Storage._create_observations(connection)
        connection.execute(
            "CREATE INDEX idx_observations_position "
            "ON post_observations(job_id, snapshot_position)"
        )

    def create_job(self, request: CollectionRequest, execution_plan: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, request_json, compiled_request_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    json.dumps(request.to_dict(), separators=(",", ":")),
                    json.dumps(execution_plan, separators=(",", ":")),
                    JobStatus.QUEUED.value,
                    now,
                    now,
                ),
            )
        return job_id

    @staticmethod
    def _decode_json(value: str | None, expected: type) -> tuple[Any, bool]:
        try:
            decoded = json.loads(value) if value is not None else None
        except (TypeError, json.JSONDecodeError):
            return expected(), False
        return (decoded, True) if isinstance(decoded, expected) else (expected(), False)

    @staticmethod
    def _decode_provider_state(value: str | None) -> tuple[Any, dict[str, Any]]:
        if value is None:
            return None, {}
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value, {}
        if isinstance(decoded, dict) and decoded.get("checkpointVersion") == 1:
            metadata = decoded.get("metadata")
            return decoded.get("providerState"), metadata if isinstance(metadata, dict) else {}
        return value, {}

    @staticmethod
    def _provider_state_is_valid(value: str | None) -> bool:
        if value is None:
            return True
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return not value.lstrip().startswith(("{", "["))
        return not isinstance(decoded, dict) or (
            decoded.get("checkpointVersion") == 1
            and isinstance(decoded.get("metadata"), dict)
        )

    @staticmethod
    def _encode_provider_state(
        provider_state: Any, metadata: dict[str, Any]
    ) -> str | None:
        if provider_state is None and not metadata:
            return None
        return json.dumps(
            {
                "checkpointVersion": 1,
                "providerState": provider_state,
                "metadata": metadata,
            },
            separators=(",", ":"),
        )

    @classmethod
    def _job_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        request, request_valid = cls._decode_json(item.pop("request_json"), dict)
        execution_plan, plan_valid = cls._decode_json(
            item.pop("compiled_request_json"), dict
        )
        warnings, warnings_valid = cls._decode_json(item.pop("warnings_json"), list)
        raw_provider = request.get("provider", ProviderType.OFFICIAL_X_API.value)
        if raw_provider == "x_api_search":
            raw_provider = ProviderType.OFFICIAL_X_API.value
        try:
            provider = ProviderType(str(raw_provider))
        except ValueError:
            provider = None
        request["provider"] = provider.value if provider else str(raw_provider)
        raw_cursor = item["cursor"]
        provider_state, checkpoint_metadata = cls._decode_provider_state(raw_cursor)
        if provider is ProviderType.OFFICIAL_X_API:
            checkpoint_metadata = {
                **checkpoint_metadata,
                "resourcesReturned": {
                    "posts": int(item["post_resource_count"]),
                    "users": int(item["user_resource_count"]),
                    "media": int(item["media_resource_count"]),
                },
                "rateLimitRemaining": item["rate_limit_remaining"],
                "rateLimitReset": item["rate_limit_reset"],
            }
        item["request"] = request
        item["provider"] = provider.value if provider else str(raw_provider)
        item["execution_plan"] = execution_plan
        item["compiled_request"] = execution_plan
        item["provider_state"] = provider_state
        item["cursor"] = provider_state
        item["checkpoint"] = {
            "providerState": provider_state,
            "storedCount": int(item["collected_count"]),
            "metadata": checkpoint_metadata,
        }
        storage_warnings = []
        if not request_valid:
            storage_warnings.append("Stored job request metadata is unreadable.")
        if not plan_valid:
            storage_warnings.append("Stored execution-plan metadata is unreadable.")
        if not warnings_valid:
            storage_warnings.append("Stored job warnings are unreadable.")
        if provider is None:
            storage_warnings.append("Stored collection provider is unknown.")
        if not cls._provider_state_is_valid(raw_cursor):
            storage_warnings.append("Stored provider checkpoint is unreadable.")
        item["stored_metadata_valid"] = not storage_warnings
        item["warnings"] = [
            *[warning for warning in warnings if isinstance(warning, str)],
            *storage_warnings,
        ]
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
                UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?),
                    finished_at = NULL, retry_at = NULL, error_code = NULL,
                    error_message = NULL, error_retryable = 0,
                    completion_reason = NULL,
                    capture_segment = COALESCE(capture_segment, -1) + 1, updated_at = ?
                WHERE id = ? AND status = 'queued' AND cancel_requested = 0
                """,
                (now, now, job_id),
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
        provider_state: Any,
        metadata: dict[str, Any],
    ) -> int:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT collected_count, warnings_json, cursor, capture_segment
                FROM jobs
                WHERE id = ? AND status = 'running' AND cancel_requested = 0
                """,
                (job_id,),
            ).fetchone()
            if current is None:
                return 0
            position = int(current["collected_count"])
            capture_segment = current["capture_segment"]
            if not isinstance(capture_segment, int) or capture_segment < 0:
                raise RuntimeError("Running job capture segment is invalid.")
            raw_scan_ordinal = metadata.get(
                "scanOrdinal",
                metadata.get("segmentScanIterations", metadata.get("scanIterations")),
            )
            scan_ordinal = (
                raw_scan_ordinal
                if isinstance(raw_scan_ordinal, int)
                and not isinstance(raw_scan_ordinal, bool)
                and raw_scan_ordinal >= 0
                else None
            )
            stored_warnings, warnings_valid = self._decode_json(
                current["warnings_json"], list
            )
            warnings = list(
                dict.fromkeys(
                    [
                        *(
                            item for item in stored_warnings if isinstance(item, str)
                        ),
                        *(
                            []
                            if warnings_valid
                            else ["Stored job warnings are unreadable."]
                        ),
                        *(
                            str(item)
                            for item in metadata.get("warnings", [])
                            if isinstance(item, str)
                        ),
                    ]
                )
            )
            added = 0
            for post in posts:
                inserted = connection.execute(
                    """
                    INSERT INTO post_observations (
                        job_id, post_id, snapshot_position, capture_segment,
                        scan_ordinal, dom_position, text, author_id, author_username,
                        url, created_at, observed_at, language, conversation_id,
                        in_reply_to_post_id, like_count, reply_count, repost_count,
                        quote_count, bookmark_count, view_count,
                        is_reply, is_repost, is_quote,
                        has_media, media_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(job_id, post_id) DO NOTHING
                    """,
                    (
                        job_id,
                        post.post_id,
                        position,
                        capture_segment,
                        scan_ordinal,
                        (
                            post.source_position
                            if isinstance(post.source_position, int)
                            and not isinstance(post.source_position, bool)
                            and post.source_position >= 0
                            else None
                        ),
                        post.text,
                        post.author_id,
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
                        post.view_count,
                        None if post.is_reply is None else int(post.is_reply),
                        None if post.is_repost is None else int(post.is_repost),
                        None if post.is_quote is None else int(post.is_quote),
                        None if post.has_media is None else int(post.has_media),
                        (
                            json.dumps(post.media, separators=(",", ":"))
                            if post.media is not None
                            else None
                        ),
                    ),
                ).rowcount
                if inserted:
                    position += 1
                    added += 1
            resources = metadata.get("resourcesReturned") or {}
            _, persisted_metadata = self._decode_provider_state(current["cursor"])
            provider_metadata = {
                **persisted_metadata,
                **{
                    key: value
                    for key, value in metadata.items()
                    if key
                    not in {
                        "warnings",
                        "resourcesReturned",
                        "rateLimitRemaining",
                        "rateLimitReset",
                    }
                },
            }
            connection.execute(
                """
                UPDATE jobs SET collected_count = ?, cursor = ?,
                    post_resource_count = post_resource_count + ?,
                    user_resource_count = user_resource_count + ?,
                    media_resource_count = media_resource_count + ?, warnings_json = ?,
                    rate_limit_remaining = COALESCE(?, rate_limit_remaining),
                    rate_limit_reset = COALESCE(?, rate_limit_reset), updated_at = ?
                WHERE id = ? AND status = 'running' AND cancel_requested = 0
                """,
                (
                    position,
                    self._encode_provider_state(provider_state, provider_metadata),
                    int(resources.get("posts") or 0),
                    int(resources.get("users") or 0),
                    int(resources.get("media") or 0),
                    json.dumps(warnings),
                    metadata.get("rateLimitRemaining"),
                    metadata.get("rateLimitReset"),
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
        return self._transition_job(
            job_id,
            allowed={JobStatus.RUNNING.value},
            target=JobStatus.PARTIAL if partial else JobStatus.SUCCEEDED,
            warnings=warnings,
            completion_reason=completion_reason,
        )

    def _transition_job(
        self,
        job_id: str,
        *,
        allowed: set[str],
        target: JobStatus,
        warnings: list[str] | None = None,
        completion_reason: str,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
        rate_limit_remaining: int | None = None,
        rate_limit_reset: int | None = None,
    ) -> str | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, cancel_requested, collected_count FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row or row["status"] not in allowed:
                return None
            if row["cancel_requested"]:
                target = JobStatus.CANCELLED
                completion_reason = error_code = "cancelled"
                error_message = "Collection cancelled."
                retryable = True
                warnings = None
            elif target is JobStatus.WAITING:
                target = (
                    JobStatus.PARTIAL if int(row["collected_count"]) else JobStatus.FAILED
                )
                error_code = completion_reason = "rate_limited"
                retryable = False
            changed = connection.execute(
                """
                UPDATE jobs SET status = ?,
                    warnings_json = COALESCE(?, warnings_json),
                    error_code = ?, error_message = ?, error_retryable = ?,
                    completion_reason = ?, retry_at = NULL,
                    rate_limit_remaining = COALESCE(?, rate_limit_remaining),
                    rate_limit_reset = COALESCE(?, rate_limit_reset),
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    target.value,
                    json.dumps(warnings) if warnings is not None else None,
                    error_code,
                    error_message,
                    int(retryable),
                    completion_reason,
                    rate_limit_remaining,
                    rate_limit_reset,
                    now,
                    now,
                    job_id,
                    row["status"],
                ),
            ).rowcount
        return target.value if changed else None

    def fail_job(
        self,
        job_id: str,
        status: JobStatus,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        self._transition_job(
            job_id,
            allowed={
                JobStatus.QUEUED.value,
                JobStatus.RUNNING.value,
                JobStatus.WAITING.value,
            },
            target=status,
            completion_reason=code,
            error_code=code,
            error_message=message,
            retryable=retryable,
        )

    def wait_job(
        self,
        job_id: str,
        retry_at: str,
        remaining: int | None,
        reset: int | None,
        message: str,
    ) -> None:
        del retry_at
        self._transition_job(
            job_id,
            allowed={JobStatus.RUNNING.value},
            target=JobStatus.WAITING,
            completion_reason="rate_limited",
            error_code="rate_limited",
            error_message=message,
            rate_limit_remaining=remaining,
            rate_limit_reset=reset,
        )

    def request_cancel(self, job_id: str) -> bool:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stopped = connection.execute(
                """
                UPDATE jobs SET status = 'cancelled', cancel_requested = 1,
                    error_code = 'cancelled', error_message = 'Collection cancelled.',
                    error_retryable = 1, completion_reason = 'cancelled',
                    retry_at = NULL, finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running', 'waiting')
                """,
                (now, now, job_id),
            ).rowcount
        return bool(stopped)

    def delete_job(self, job_id: str) -> bool:
        with self.connect() as connection:
            changed = connection.execute(
                "DELETE FROM jobs WHERE id = ? AND status NOT IN ('queued', 'running', 'waiting')",
                (job_id,),
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
                    completion_reason = NULL, finished_at = NULL, retry_at = NULL,
                    updated_at = ?
                WHERE id = ? AND (
                    status IN ('cancelled', 'interrupted', 'partial')
                    OR (status = 'failed' AND error_retryable = 1)
                )
                AND COALESCE(error_code, '') NOT IN (
                    'manual_action_required', 'session_expired',
                    'rate_limited', 'browser_rate_limited'
                )
                """,
                (utc_now(), job_id),
            ).rowcount
        return bool(changed)

    def recover_jobs(self) -> list[str]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE jobs SET status = 'cancelled', error_code = 'cancelled',
                    error_message = 'Collection cancellation was preserved across restart.',
                    error_retryable = 1, completion_reason = 'cancelled',
                    finished_at = ?, updated_at = ?
                WHERE status IN ('queued', 'running', 'waiting') AND cancel_requested = 1
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE jobs SET
                    status = CASE WHEN collected_count > 0 THEN 'partial' ELSE 'failed' END,
                    error_code = COALESCE(error_code, 'rate_limited'),
                    error_retryable = 0, retry_at = NULL,
                    completion_reason = COALESCE(error_code, 'rate_limited'),
                    finished_at = ?, updated_at = ?
                WHERE status = 'waiting'
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
            connection.execute(
                """
                UPDATE jobs SET status = 'cancelled', error_code = 'cancelled',
                    error_message = 'Collection cancellation was preserved.',
                    error_retryable = 1, completion_reason = 'cancelled', retry_at = NULL,
                    finished_at = ?, updated_at = ?
                WHERE status = 'waiting' AND cancel_requested = 1
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE jobs SET
                    status = CASE WHEN collected_count > 0 THEN 'partial' ELSE 'failed' END,
                    error_code = COALESCE(error_code, 'rate_limited'),
                    error_retryable = 0, retry_at = NULL,
                    completion_reason = COALESCE(error_code, 'rate_limited'),
                    finished_at = ?, updated_at = ?
                WHERE status = 'waiting'
                """,
                (now, now),
            )
        return []

    def get_job_posts(
        self, job_id: str, *, limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM post_observations WHERE job_id = ? "
                "ORDER BY snapshot_position LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("job_id")
            item["source_position"] = item["dom_position"]
            media = item.pop("media_json")
            if media is None:
                item["media"] = None
            else:
                item["media"], media_valid = self._decode_json(media, list)
                if not media_valid:
                    item["media"] = None
                    item["storage_warnings"] = ["Stored media metadata is unreadable."]
            for name in ("is_reply", "is_repost", "is_quote", "has_media"):
                if item[name] is not None:
                    item[name] = bool(item[name])
            result.append(item)
        return result

    def count_job_posts(self, job_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM post_observations WHERE job_id = ?", (job_id,)
            ).fetchone()
        return int(row[0]) if row else 0
