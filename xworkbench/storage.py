from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import (
    CollectionRequest,
    JobStatus,
    Post,
    ProviderType,
    SourceDefinition,
    SourceType,
    request_fingerprint,
    source_fingerprint,
    utc_now,
)

SCHEMA_FAMILY = "x_collection_workbench"
SCHEMA_VERSION = "4"
FTS_TABLES = {
    "post_observations_fts",
    "post_observations_fts_config",
    "post_observations_fts_content",
    "post_observations_fts_data",
    "post_observations_fts_docsize",
    "post_observations_fts_idx",
}
SCHEMA_TABLES = {"schema_meta", "sources", "jobs", "post_observations", *FTS_TABLES}
TERMINAL_STATUSES = {
    JobStatus.SUCCEEDED.value,
    JobStatus.PARTIAL.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
    JobStatus.INTERRUPTED.value,
}
DEFAULT_STALE_AFTER_SECONDS = 86_400
MAX_DEADLINE_SECONDS = 31_536_000
APPROVAL_KEYS = {"approvedAt", "confirmation", "previewFingerprint", "batchId"}
LIMIT_KEYS = {"maxPosts", "deadlineSeconds", "routeAlias", "maxConcurrency"}
BATCH_ITEM_KEYS = {
    "request",
    "plan",
    "priority",
    "source_id",
    "auth_state_id",
    "idempotency_key",
    "limits",
    "deadline_at",
}
SECRET_KEY_PARTS = (
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "password",
    "proxy",
    "secret",
    "session",
    "token",
)
SCHEMA_COLUMNS = {
    "schema_meta": {"key", "value"},
    "sources": {
        "id",
        "display_name",
        "provider",
        "surface",
        "normalized_value",
        "source_fingerprint",
        "created_at",
        "last_status",
    },
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
        "source_id",
        "source_fingerprint",
        "request_fingerprint",
        "parser_version",
        "stale_after_seconds",
        "snapshot_at",
        "snapshot_partial",
        "coverage_json",
        "truncated",
        "reuse_eligible",
        "queue_priority",
        "enqueue_sequence",
        "batch_id",
        "idempotency_key",
        "auth_state_id",
        "approval_json",
        "limits_json",
        "deadline_at",
        "attempt_number",
        "lease_owner",
        "leased_at",
        "lease_heartbeat_at",
        "lease_expires_at",
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
LEGACY_JOB_COLUMNS = (
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
)
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


_CONNECTION_LIFECYCLE_LOCK = threading.RLock()


class _ClosingConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        with _CONNECTION_LIFECYCLE_LOCK:
            super().__init__(*args, **kwargs)

    def close(self) -> None:
        with _CONNECTION_LIFECYCLE_LOCK:
            super().close()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


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
        connection = sqlite3.connect(
            self.path, timeout=30, factory=_ClosingConnection
        )
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
                    if "schema_meta" not in tables:
                        raise RuntimeError(self._incompatible_message())
                    metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))
                    if metadata.get("schema_family") != SCHEMA_FAMILY or set(metadata) != {
                        "schema_family",
                        "schema_version",
                    }:
                        raise RuntimeError(self._incompatible_message())
                    version = metadata["schema_version"]
                    if version not in {"1", "2", "3", SCHEMA_VERSION}:
                        raise RuntimeError(self._incompatible_message())
                    if not self._schema_is_compatible(connection, version=int(version)):
                        raise RuntimeError(self._incompatible_message())
                    if version != SCHEMA_VERSION:
                        self._backup_before_migration(connection, version=version)
                        self._migrate_to_v4(connection, version=int(version))
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
    def _schema_is_compatible(connection: sqlite3.Connection, *, version: int = 4) -> bool:
        if version not in {1, 2, 3, 4}:
            return False
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('view', 'trigger') LIMIT 1"
        ).fetchone():
            return False
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected_tables = (
            SCHEMA_TABLES
            if version == 4
            else {"schema_meta", "jobs", "post_observations"}
        )
        if tables != expected_tables:
            return False
        expected_columns = {name: set(columns) for name, columns in SCHEMA_COLUMNS.items()}
        if version < 4:
            expected_columns.pop("sources")
            expected_columns["jobs"] -= {
                "source_id",
                "source_fingerprint",
                "request_fingerprint",
                "parser_version",
                "stale_after_seconds",
                "snapshot_at",
                "snapshot_partial",
                "coverage_json",
                "truncated",
                "reuse_eligible",
                "queue_priority",
                "enqueue_sequence",
                "batch_id",
                "idempotency_key",
                "auth_state_id",
                "approval_json",
                "limits_json",
                "deadline_at",
                "attempt_number",
                "lease_owner",
                "leased_at",
                "lease_heartbeat_at",
                "lease_expires_at",
            }
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
        if version == 4:
            expected_primary_keys["sources"] = ["id"]
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
        if version >= 3:
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
        position = "snapshot_position" if version >= 3 else "position"
        indexes: dict[str, tuple[bool, list[str]]] = {}
        for table in ("sources", "jobs", "post_observations") if version == 4 else (
            "jobs",
            "post_observations",
        ):
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
        observation_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(post_observations)"
        ).fetchall()
        if len(observation_foreign_keys) != 1 or not any(
            row["table"] == "jobs"
            and row["from"] == "job_id"
            and row["to"] == "id"
            and row["on_delete"].upper() == "CASCADE"
            for row in observation_foreign_keys
        ):
            return False
        if version < 4:
            return True
        if indexes.get("idx_jobs_source_snapshot") != (
            False,
            ["source_fingerprint", "snapshot_at"],
        ) or indexes.get("idx_jobs_request_snapshot") != (
            False,
            ["request_fingerprint", "snapshot_at"],
        ):
            return False
        required_indexes = {
            "idx_jobs_queue": (False, ["status", "queue_priority", "enqueue_sequence"]),
            "idx_jobs_status_source": (False, ["status", "source_id"]),
            "idx_jobs_status_auth": (False, ["status", "auth_state_id"]),
            "idx_jobs_lease_expiry": (False, ["lease_expires_at"]),
            "idx_jobs_batch": (False, ["batch_id"]),
            "idx_jobs_idempotency": (True, ["idempotency_key"]),
        }
        if any(indexes.get(name) != definition for name, definition in required_indexes.items()):
            return False
        def has_unique(table: str, columns: list[str]) -> bool:
            return any(
                bool(index["unique"])
                and [
                    row["name"]
                    for row in connection.execute(f"PRAGMA index_info({index['name']})")
                ]
                == columns
                for index in connection.execute(f"PRAGMA index_list({table})")
            )

        if not has_unique("jobs", ["enqueue_sequence"]) or not has_unique(
            "sources", ["source_fingerprint"]
        ):
            return False
        queue_details = details["jobs"]
        required_not_null = {
            "queue_priority": "0",
            "enqueue_sequence": None,
            "auth_state_id": None,
            "approval_json": None,
            "limits_json": None,
            "deadline_at": None,
            "attempt_number": "0",
        }
        if any(
            not queue_details[name]["notnull"]
            or queue_details[name]["dflt_value"] != default
            for name, default in required_not_null.items()
        ):
            return False
        jobs_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'"
        ).fetchone()
        idempotency_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_jobs_idempotency'"
        ).fetchone()
        normalized_jobs_sql = " ".join((jobs_sql_row[0] if jobs_sql_row else "").split())
        normalized_idempotency_sql = " ".join(
            (idempotency_sql_row[0] if idempotency_sql_row else "").split()
        )
        expected_idempotency_sql = (
            "CREATE UNIQUE INDEX idx_jobs_idempotency ON jobs(idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        )
        if (
            "CHECK (queue_priority BETWEEN 0 AND 100)" not in normalized_jobs_sql
            or normalized_idempotency_sql != expected_idempotency_sql
        ):
            return False
        source_foreign_keys = connection.execute("PRAGMA foreign_key_list(jobs)").fetchall()
        fts_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'post_observations_fts'"
        ).fetchone()
        return (
            len(source_foreign_keys) == 1
            and source_foreign_keys[0]["table"] == "sources"
            and source_foreign_keys[0]["from"] == "source_id"
            and source_foreign_keys[0]["to"] == "id"
            and fts_sql is not None
            and "USING fts5" in fts_sql[0]
            and [
                row["name"]
                for row in connection.execute("PRAGMA table_info(post_observations_fts)")
            ]
            == ["text", "job_id", "post_id"]
        )

    def _backup_before_migration(
        self, connection: sqlite3.Connection, *, version: str
    ) -> None:
        backup_path = self.path.with_name(f"{self.path.name}.pre-v{version}-to-v4.bak")
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
            with sqlite3.connect(temporary, factory=_ClosingConnection) as backup:
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

    @staticmethod
    def _create_sources(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE sources (
                id                 TEXT PRIMARY KEY,
                display_name       TEXT NOT NULL,
                provider           TEXT NOT NULL,
                surface            TEXT NOT NULL,
                normalized_value   TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL UNIQUE,
                created_at         TEXT NOT NULL,
                last_status        TEXT
            )
            """
        )

    @staticmethod
    def _create_jobs(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
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
                source_id             TEXT REFERENCES sources(id),
                source_fingerprint    TEXT,
                request_fingerprint   TEXT,
                parser_version        TEXT,
                stale_after_seconds   INTEGER NOT NULL DEFAULT 86400,
                snapshot_at           TEXT,
                snapshot_partial      INTEGER,
                coverage_json         TEXT,
                truncated             INTEGER,
                reuse_eligible        INTEGER NOT NULL DEFAULT 0,
                queue_priority        INTEGER NOT NULL DEFAULT 0
                                          CHECK (queue_priority BETWEEN 0 AND 100),
                enqueue_sequence      INTEGER NOT NULL UNIQUE,
                batch_id              TEXT,
                idempotency_key       TEXT,
                auth_state_id         TEXT NOT NULL,
                approval_json         TEXT NOT NULL,
                limits_json           TEXT NOT NULL,
                deadline_at           TEXT NOT NULL,
                attempt_number        INTEGER NOT NULL DEFAULT 0,
                lease_owner           TEXT,
                leased_at             TEXT,
                lease_heartbeat_at     TEXT,
                lease_expires_at       TEXT,
                created_at            TEXT NOT NULL,
                started_at            TEXT,
                finished_at           TEXT,
                updated_at            TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _create_job_indexes(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE INDEX idx_jobs_status_retry ON jobs(status, retry_at);
            CREATE INDEX idx_jobs_source_snapshot
                ON jobs(source_fingerprint, snapshot_at);
            CREATE INDEX idx_jobs_request_snapshot
                ON jobs(request_fingerprint, snapshot_at);
            CREATE INDEX idx_jobs_queue
                ON jobs(status, queue_priority, enqueue_sequence);
            CREATE INDEX idx_jobs_status_source ON jobs(status, source_id);
            CREATE INDEX idx_jobs_status_auth ON jobs(status, auth_state_id);
            CREATE INDEX idx_jobs_lease_expiry ON jobs(lease_expires_at);
            CREATE INDEX idx_jobs_batch ON jobs(batch_id);
            CREATE UNIQUE INDEX idx_jobs_idempotency ON jobs(idempotency_key)
                WHERE idempotency_key IS NOT NULL;
            """
        )

    @staticmethod
    def _create_fts(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE VIRTUAL TABLE post_observations_fts USING fts5(
                text,
                job_id UNINDEXED,
                post_id UNINDEXED,
                tokenize = 'unicode61'
            )
            """
        )

    @staticmethod
    def _plan_parser_version(plan: Any) -> str | None:
        if not isinstance(plan, dict):
            return None
        value = plan.get("parserVersion", plan.get("providerVersion"))
        if isinstance(value, bool) or not isinstance(value, str | int):
            return None
        value = str(value)
        return value if 0 < len(value) <= 64 else None

    def _migrate_to_v4(self, connection: sqlite3.Connection, *, version: int) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            legacy_jobs = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at, id"
            ).fetchall()
            connection.execute("DROP INDEX idx_jobs_status_retry")
            connection.execute("DROP INDEX idx_observations_position")
            connection.execute(
                "ALTER TABLE post_observations RENAME TO post_observations_legacy"
            )
            connection.execute("ALTER TABLE jobs RENAME TO jobs_legacy")
            self._create_sources(connection)
            self._create_jobs(connection)
            self._create_observations(connection)
            migration_now = datetime.now(UTC)
            for sequence, row in enumerate(legacy_jobs, 1):
                legacy = dict(row)
                if version < 3:
                    legacy["capture_segment"] = None
                source_value = request_value = parser = None
                reuse = 0
                provider_alias = "provider:unknown"
                try:
                    request_body = json.loads(legacy["request_json"])
                    request = CollectionRequest.from_dict(request_body)
                    source_value = source_fingerprint(request)
                    request_value = request_fingerprint(request)
                    reuse = int(request.source_type is not SourceType.HOME)
                    provider_alias = f"provider:{request.provider.value}"
                except Exception:
                    pass
                try:
                    parser = self._plan_parser_version(
                        json.loads(legacy["compiled_request_json"])
                    )
                except (TypeError, json.JSONDecodeError):
                    pass
                terminal = legacy["status"] in TERMINAL_STATUSES
                snapshot_at = (
                    legacy["finished_at"]
                    or legacy["updated_at"]
                    or legacy["created_at"]
                    if terminal
                    else None
                )
                collected, _ = self._safe_int(legacy["collected_count"], default=0)
                capture_segment, _ = self._safe_int(
                    legacy.get("capture_segment"), default=None
                )
                partial = (
                    int(
                        legacy["status"] == JobStatus.PARTIAL.value
                        or (
                            legacy["status"] != JobStatus.SUCCEEDED.value
                            and bool(collected)
                        )
                    )
                    if terminal
                    else None
                )
                truncated = (
                    int(
                        legacy["completion_reason"]
                        in {"target_reached", "post_resource_limit_reached"}
                    )
                    if terminal
                    else None
                )
                deadline = migration_now + timedelta(hours=1)
                if terminal:
                    safe_deadline, valid_deadline = self._safe_timestamp(snapshot_at)
                    deadline_value = (
                        safe_deadline
                        if valid_deadline and safe_deadline
                        else migration_now.isoformat()
                    )
                else:
                    deadline_value = deadline.isoformat()
                legacy_columns = [
                    name for name in LEGACY_JOB_COLUMNS if name in legacy
                ]
                added_columns = [
                    "source_id",
                    "source_fingerprint",
                    "request_fingerprint",
                    "parser_version",
                    "stale_after_seconds",
                    "snapshot_at",
                    "snapshot_partial",
                    "coverage_json",
                    "truncated",
                    "reuse_eligible",
                    "queue_priority",
                    "enqueue_sequence",
                    "batch_id",
                    "idempotency_key",
                    "auth_state_id",
                    "approval_json",
                    "limits_json",
                    "deadline_at",
                    "attempt_number",
                    "lease_owner",
                    "leased_at",
                    "lease_heartbeat_at",
                    "lease_expires_at",
                ]
                values = [legacy[name] for name in legacy_columns]
                values.extend(
                    [
                        None,
                        source_value,
                        request_value,
                        parser,
                        DEFAULT_STALE_AFTER_SECONDS,
                        snapshot_at,
                        partial,
                        "{}" if terminal else None,
                        truncated,
                        reuse,
                        0,
                        sequence,
                        None,
                        None,
                        provider_alias,
                        "{}",
                        "{}",
                        deadline_value,
                        0 if capture_segment is None else capture_segment + 1,
                        None,
                        None,
                        None,
                        None,
                    ]
                )
                columns = [*legacy_columns, *added_columns]
                connection.execute(
                    f"INSERT INTO jobs ({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})",
                    values,
                )
            legacy_observation_columns = [
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(post_observations_legacy)"
                )
            ]
            target_observation_columns = [
                "snapshot_position" if name == "position" else name
                for name in legacy_observation_columns
            ]
            connection.execute(
                f"INSERT INTO post_observations "
                f"({', '.join(target_observation_columns)}) "
                f"SELECT {', '.join(legacy_observation_columns)} "
                "FROM post_observations_legacy"
            )
            connection.execute("DROP TABLE post_observations_legacy")
            connection.execute("DROP TABLE jobs_legacy")
            self._create_job_indexes(connection)
            connection.execute(
                "CREATE INDEX idx_observations_position "
                "ON post_observations(job_id, snapshot_position)"
            )
            self._create_fts(connection)
            connection.execute(
                """
                INSERT INTO post_observations_fts(text, job_id, post_id)
                SELECT COALESCE(text, ''), job_id, post_id FROM post_observations
                """
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
                ('schema_version', '4');
            """
        )
        Storage._create_sources(connection)
        Storage._create_jobs(connection)
        Storage._create_job_indexes(connection)
        Storage._create_observations(connection)
        connection.execute(
            "CREATE INDEX idx_observations_position "
            "ON post_observations(job_id, snapshot_position)"
        )
        Storage._create_fts(connection)

    @staticmethod
    def _source_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def save_source(self, source: SourceDefinition) -> dict[str, Any]:
        fingerprint = source_fingerprint(source)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM sources WHERE id = ?", (source.source_id,)
            ).fetchone()
            values = (
                source.source_id,
                source.display_name,
                source.provider.value,
                source.surface.value,
                source.normalized_value,
                fingerprint,
                source.created_at,
                source.last_status.value if source.last_status else None,
            )
            if existing:
                immutable_columns = (
                    "id",
                    "display_name",
                    "provider",
                    "surface",
                    "normalized_value",
                    "source_fingerprint",
                    "created_at",
                )
                if tuple(existing[name] for name in immutable_columns) != values[:-1]:
                    raise ValueError("Saved source id is immutable.")
                return self._source_dict(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO sources (
                        id, display_name, provider, surface, normalized_value,
                        source_fingerprint, created_at, last_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Saved source identity already exists.") from exc
        saved = self.get_source(source.source_id)
        if saved is None:
            raise RuntimeError("Saved source could not be read back.")
        return saved

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        return self._source_dict(row) if row else None

    def list_sources(self, limit: int = 100, *, offset: int = 0) -> list[dict[str, Any]]:
        self._validate_page(limit, offset)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sources ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._source_dict(row) for row in rows]

    def update_source_status(self, source_id: str, status: JobStatus) -> bool:
        with self.connect() as connection:
            changed = connection.execute(
                "UPDATE sources SET last_status = ? WHERE id = ?",
                (status.value, source_id),
            ).rowcount
        return bool(changed)

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 10_000
        ):
            raise ValueError("Pagination requires limit 1–100 and offset 0–10000.")

    @staticmethod
    def _canonical_json(value: Any) -> str:
        try:
            return json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Durable job metadata must be JSON serializable.") from exc

    @staticmethod
    def _public_record(
        value: dict[str, Any], *, allowed: set[str], label: str
    ) -> str:
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be a JSON object.")
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"Unsupported public {label} field(s): {', '.join(unknown)}.")
        for key, item in value.items():
            lowered = key.casefold()
            if any(marker in lowered for marker in SECRET_KEY_PARTS):
                raise ValueError(f"{label} cannot contain secret material.")
            if (
                isinstance(item, float | list | dict)
                or item is None
                or not isinstance(item, str | int | bool)
                or isinstance(item, int)
                and not isinstance(item, bool)
                and item < 0
                or isinstance(item, str)
                and len(item) > 256
            ):
                raise ValueError(f"{label} values must be bounded public scalars.")
        return Storage._canonical_json(value)

    @staticmethod
    def _queue_identifier(
        value: str | None, *, label: str, nullable: bool = False
    ) -> str | None:
        if value is None and nullable:
            return None
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ValueError(f"{label} must contain 1–128 characters.")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(f"{label} contains unsupported characters.")
        return value

    @staticmethod
    def _deadline(value: str | datetime, *, require_future: bool = True) -> str:
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("deadline_at must be an ISO-8601 timestamp.") from exc
        if parsed.utcoffset() is None:
            raise ValueError("deadline_at must include a timezone.")
        parsed = parsed.astimezone(UTC)
        now = datetime.now(UTC)
        if require_future and not now < parsed <= now + timedelta(seconds=MAX_DEADLINE_SECONDS):
            raise ValueError("deadline_at must be within the next 365 days.")
        return parsed.isoformat()

    @staticmethod
    def _next_enqueue_sequence(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(enqueue_sequence), 0) + 1 FROM jobs"
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _validate_queue_capacity(queue_capacity: int) -> None:
        if (
            isinstance(queue_capacity, bool)
            or not isinstance(queue_capacity, int)
            or queue_capacity < 0
        ):
            raise ValueError("queue_capacity must be a nonnegative integer.")

    def _prepare_admission(
        self,
        request: CollectionRequest,
        plan: dict[str, Any],
        *,
        priority: int,
        source_id: str | None,
        auth_state_id: str,
        batch_id: str | None,
        idempotency_key: str | None,
        approval: dict[str, Any],
        limits: dict[str, Any],
        deadline_at: str | datetime,
        stale_after_seconds: int,
    ) -> dict[str, Any]:
        if not isinstance(request, CollectionRequest):
            raise ValueError("request must be a CollectionRequest.")
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not 0 <= priority <= 100
        ):
            raise ValueError("priority must be between 0 and 100.")
        if (
            isinstance(stale_after_seconds, bool)
            or not isinstance(stale_after_seconds, int)
            or not 0 <= stale_after_seconds <= 315_360_000
        ):
            raise ValueError("stale_after_seconds must be between 0 and 315360000.")
        if not isinstance(plan, dict):
            raise ValueError("plan must be a JSON object.")
        source_id = self._queue_identifier(source_id, label="source_id", nullable=True)
        auth_state_id = self._queue_identifier(
            auth_state_id, label="auth_state_id"
        )  # type: ignore[assignment]
        batch_id = self._queue_identifier(batch_id, label="batch_id", nullable=True)
        idempotency_key = self._queue_identifier(
            idempotency_key, label="idempotency_key", nullable=True
        )
        approval_json = self._public_record(
            approval, allowed=APPROVAL_KEYS, label="approval"
        )
        limits_json = self._public_record(limits, allowed=LIMIT_KEYS, label="limits")
        deadline = self._deadline(deadline_at)
        request_json = self._canonical_json(request.to_dict())
        plan_json = self._canonical_json(plan)
        source_value = source_fingerprint(request)
        request_value = request_fingerprint(request)
        parser_version = self._plan_parser_version(plan)
        return {
            "request": request,
            "request_json": request_json,
            "plan_json": plan_json,
            "source_id": source_id,
            "source_fingerprint": source_value,
            "request_fingerprint": request_value,
            "parser_version": parser_version,
            "stale_after_seconds": stale_after_seconds,
            "reuse_eligible": int(request.source_type is not SourceType.HOME),
            "priority": priority,
            "batch_id": batch_id,
            "idempotency_key": idempotency_key,
            "auth_state_id": auth_state_id,
            "approval_json": approval_json,
            "limits_json": limits_json,
            "deadline_at": deadline,
        }

    @staticmethod
    def _admission_identity(prepared: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(
            prepared[name]
            for name in (
                "request_json",
                "plan_json",
                "source_id",
                "source_fingerprint",
                "request_fingerprint",
                "priority",
                "batch_id",
                "auth_state_id",
                "approval_json",
                "limits_json",
                "deadline_at",
                "stale_after_seconds",
            )
        )

    @staticmethod
    def _row_admission_identity(row: sqlite3.Row) -> tuple[Any, ...]:
        return tuple(
            row[name]
            for name in (
                "request_json",
                "compiled_request_json",
                "source_id",
                "source_fingerprint",
                "request_fingerprint",
                "queue_priority",
                "batch_id",
                "auth_state_id",
                "approval_json",
                "limits_json",
                "deadline_at",
                "stale_after_seconds",
            )
        )

    def _existing_admission(
        self, connection: sqlite3.Connection, prepared: dict[str, Any]
    ) -> sqlite3.Row | None:
        key = prepared["idempotency_key"]
        if key is None:
            return None
        row = connection.execute(
            "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if row and self._row_admission_identity(row) != self._admission_identity(prepared):
            raise ValueError("idempotency_key was already used for a different job.")
        return row

    @staticmethod
    def _validate_admission_source(
        connection: sqlite3.Connection, prepared: dict[str, Any]
    ) -> None:
        source_id = prepared["source_id"]
        if source_id is None:
            return
        source = connection.execute(
            "SELECT source_fingerprint FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if not source or source["source_fingerprint"] != prepared["source_fingerprint"]:
            raise ValueError("Saved source does not match the collection request.")

    @staticmethod
    def _insert_admission(
        connection: sqlite3.Connection,
        prepared: dict[str, Any],
        *,
        job_id: str,
        sequence: int,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO jobs (
                id, request_json, compiled_request_json, status,
                source_id, source_fingerprint, request_fingerprint, parser_version,
                stale_after_seconds, reuse_eligible, queue_priority,
                enqueue_sequence, batch_id, idempotency_key, auth_state_id,
                approval_json, limits_json, deadline_at, created_at, updated_at
            ) VALUES (
                ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                job_id,
                prepared["request_json"],
                prepared["plan_json"],
                prepared["source_id"],
                prepared["source_fingerprint"],
                prepared["request_fingerprint"],
                prepared["parser_version"],
                prepared["stale_after_seconds"],
                prepared["reuse_eligible"],
                prepared["priority"],
                sequence,
                prepared["batch_id"],
                prepared["idempotency_key"],
                prepared["auth_state_id"],
                prepared["approval_json"],
                prepared["limits_json"],
                prepared["deadline_at"],
                now,
                now,
            ),
        )
        if prepared["source_id"] is not None:
            connection.execute(
                "UPDATE sources SET last_status = 'queued' WHERE id = ?",
                (prepared["source_id"],),
            )

    def _admit_job(
        self,
        request: CollectionRequest,
        plan: dict[str, Any],
        *,
        queue_capacity: int,
        priority: int,
        source_id: str | None,
        auth_state_id: str,
        batch_id: str | None,
        idempotency_key: str | None,
        approval: dict[str, Any],
        limits: dict[str, Any],
        deadline_at: str | datetime,
        stale_after_seconds: int,
    ) -> dict[str, str | None]:
        self._validate_queue_capacity(queue_capacity)
        prepared = self._prepare_admission(
            request,
            plan,
            priority=priority,
            source_id=source_id,
            auth_state_id=auth_state_id,
            batch_id=batch_id,
            idempotency_key=idempotency_key,
            approval=approval,
            limits=limits,
            deadline_at=deadline_at,
            stale_after_seconds=stale_after_seconds,
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._existing_admission(connection, prepared)
            if existing:
                return {"result": "existing", "job_id": existing["id"]}
            active = connection.execute(
                "SELECT COUNT(*) FROM jobs "
                "WHERE status IN ('queued', 'running', 'waiting')"
            ).fetchone()[0]
            if active >= queue_capacity:
                return {"result": "queue_full", "job_id": None}
            self._validate_admission_source(connection, prepared)
            job_id = uuid.uuid4().hex
            self._insert_admission(
                connection,
                prepared,
                job_id=job_id,
                sequence=self._next_enqueue_sequence(connection),
                now=utc_now(),
            )
        return {"result": "created", "job_id": job_id}

    def admit_job(
        self,
        request: CollectionRequest,
        plan: dict[str, Any],
        *,
        queue_capacity: int,
        priority: int,
        source_id: str | None,
        auth_state_id: str,
        batch_id: str | None,
        idempotency_key: str | None,
        approval: dict[str, Any],
        limits: dict[str, Any],
        deadline_at: str | datetime,
    ) -> dict[str, str | None]:
        return self._admit_job(
            request,
            plan,
            queue_capacity=queue_capacity,
            priority=priority,
            source_id=source_id,
            auth_state_id=auth_state_id,
            batch_id=batch_id,
            idempotency_key=idempotency_key,
            approval=approval,
            limits=limits,
            deadline_at=deadline_at,
            stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
        )

    def _prepare_batch_items(
        self, items: list[dict[str, Any]], *, batch_id: str
    ) -> list[dict[str, Any]]:
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("items must contain 1–100 batch jobs.")
        prepared = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or set(item) != BATCH_ITEM_KEYS:
                raise ValueError(f"Batch item {index} has invalid fields.")
            request = item["request"]
            limits = item["limits"]
            if not isinstance(request, CollectionRequest):
                raise ValueError(f"Batch item {index} request is invalid.")
            if item["idempotency_key"] is None:
                raise ValueError(f"Batch item {index} idempotency_key is required.")
            if not isinstance(limits, dict) or set(limits) != LIMIT_KEYS:
                raise ValueError(f"Batch item {index} limits must be exact.")
            if limits["maxPosts"] != request.max_posts:
                raise ValueError(f"Batch item {index} maxPosts does not match its request.")
            deadline_seconds = limits["deadlineSeconds"]
            if (
                isinstance(deadline_seconds, bool)
                or not isinstance(deadline_seconds, int)
                or not 1 <= deadline_seconds <= MAX_DEADLINE_SECONDS
            ):
                raise ValueError(f"Batch item {index} deadlineSeconds is invalid.")
            concurrency = limits["maxConcurrency"]
            if (
                isinstance(concurrency, bool)
                or not isinstance(concurrency, int)
                or not 1 <= concurrency <= 4
            ):
                raise ValueError(f"Batch item {index} maxConcurrency is invalid.")
            route_alias = limits["routeAlias"]
            if not isinstance(route_alias, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", route_alias
            ):
                raise ValueError(f"Batch item {index} routeAlias is invalid.")
            prepared.append(
                self._prepare_admission(
                    request,
                    item["plan"],
                    priority=item["priority"],
                    source_id=item["source_id"],
                    auth_state_id=item["auth_state_id"],
                    batch_id=batch_id,
                    idempotency_key=item["idempotency_key"],
                    approval={},
                    limits=limits,
                    deadline_at=item["deadline_at"],
                    stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
                )
            )
        return prepared

    @classmethod
    def _batch_fingerprint(
        cls, prepared: list[dict[str, Any]], *, batch_id: str
    ) -> str:
        identities = []
        for item in prepared:
            request = item["request"]
            limits = json.loads(item["limits_json"])
            identities.append(
                {
                    "request": json.loads(item["request_json"]),
                    "plan": json.loads(item["plan_json"]),
                    "sourceFingerprint": item["source_fingerprint"],
                    "requestFingerprint": item["request_fingerprint"],
                    "destination": {
                        "savedSourceId": item["source_id"],
                        "provider": request.provider.value,
                        "surface": request.source_type.value,
                        "value": request.source_value,
                    },
                    "postLimit": request.max_posts,
                    "deadlineAt": item["deadline_at"],
                    "staleAfterSeconds": item["stale_after_seconds"],
                    "reuseEligible": bool(item["reuse_eligible"]),
                    "routeAlias": limits["routeAlias"],
                    "priority": item["priority"],
                    "authStateId": item["auth_state_id"],
                    "idempotencyKey": item["idempotency_key"],
                    "limits": limits,
                }
            )
        canonical = cls._canonical_json(
            {
                "kind": "batch_preview",
                "version": 1,
                "batchId": batch_id,
                "items": identities,
            }
        )
        return f"v1:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    def batch_preview_fingerprint(
        self, items: list[dict[str, Any]], batch_id: str
    ) -> str:
        batch_id = self._queue_identifier(
            batch_id, label="batch_id"
        )  # type: ignore[assignment]
        return self._batch_fingerprint(
            self._prepare_batch_items(items, batch_id=batch_id), batch_id=batch_id
        )

    def admit_batch(
        self,
        items: list[dict[str, Any]],
        *,
        queue_capacity: int,
        batch_id: str,
        approval_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        self._validate_queue_capacity(queue_capacity)
        batch_id = self._queue_identifier(
            batch_id, label="batch_id"
        )  # type: ignore[assignment]
        if not isinstance(approval_manifest, dict) or set(approval_manifest) != APPROVAL_KEYS:
            raise ValueError("approval_manifest must contain exactly the public approval fields.")
        approval_json = self._public_record(
            approval_manifest, allowed=APPROVAL_KEYS, label="approval_manifest"
        )
        if approval_manifest["confirmation"] is not True:
            raise ValueError("approval_manifest confirmation must be true.")
        if approval_manifest["batchId"] != batch_id:
            raise ValueError("approval_manifest batchId does not match batch_id.")
        self._time_filter(approval_manifest["approvedAt"], label="approvedAt")
        prepared = self._prepare_batch_items(items, batch_id=batch_id)
        expected_preview = self._batch_fingerprint(prepared, batch_id=batch_id)
        if approval_manifest["previewFingerprint"] != expected_preview:
            raise ValueError("approval_manifest previewFingerprint does not match the batch.")
        for item in prepared:
            item["approval_json"] = approval_json

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior_manifests = connection.execute(
                "SELECT DISTINCT approval_json FROM jobs WHERE batch_id = ?", (batch_id,)
            ).fetchall()
            if prior_manifests:
                expected_core = {
                    key: approval_manifest[key]
                    for key in APPROVAL_KEYS
                    if key != "approvedAt"
                }
                stored_approval_json = prior_manifests[0]["approval_json"]
                for row in prior_manifests:
                    stored, valid = self._decode_json(row["approval_json"], dict)
                    stored_core = {
                        key: stored.get(key)
                        for key in APPROVAL_KEYS
                        if key != "approvedAt"
                    }
                    if (
                        not valid
                        or set(stored) != APPROVAL_KEYS
                        or stored_core != expected_core
                        or row["approval_json"] != stored_approval_json
                    ):
                        raise ValueError(
                            "batch_id was already used with a different manifest."
                        )
                for item in prepared:
                    item["approval_json"] = stored_approval_json

            seen: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
            new_tokens: list[dict[str, Any]] = []
            results: list[tuple[dict[str, Any], str]] = []
            for item in prepared:
                self._validate_admission_source(connection, item)
                key = item["idempotency_key"]
                if key is not None and key in seen:
                    original, token = seen[key]
                    if self._admission_identity(original) != self._admission_identity(item):
                        raise ValueError(
                            "idempotency_key was repeated with a different batch item."
                        )
                    results.append((token, "existing"))
                    continue
                existing = self._existing_admission(connection, item)
                if existing:
                    token = {"prepared": item, "row": existing}
                    result = "existing"
                else:
                    token = {
                        "prepared": item,
                        "job_id": uuid.uuid4().hex,
                    }
                    result = "created"
                    new_tokens.append(token)
                if key is not None:
                    seen[key] = (item, token)
                results.append((token, result))

            active = connection.execute(
                "SELECT COUNT(*) FROM jobs "
                "WHERE status IN ('queued', 'running', 'waiting')"
            ).fetchone()[0]
            if new_tokens and active + len(new_tokens) > queue_capacity:
                return {"result": "queue_full", "jobs": []}

            sequence = self._next_enqueue_sequence(connection)
            now = utc_now()
            for token in new_tokens:
                token["sequence"] = sequence
                self._insert_admission(
                    connection,
                    token["prepared"],
                    job_id=token["job_id"],
                    sequence=sequence,
                    now=now,
                )
                sequence += 1

            jobs = []
            for token, result in results:
                row = token.get("row")
                item = token["prepared"]
                job_id = row["id"] if row else token["job_id"]
                stored_source = row["source_id"] if row else item["source_id"]
                source_value = row["source_fingerprint"] if row else item["source_fingerprint"]
                jobs.append(
                    {
                        "result": result,
                        "job_id": job_id,
                        "status": row["status"] if row else JobStatus.QUEUED.value,
                        "priority": row["queue_priority"] if row else item["priority"],
                        "enqueue_sequence": (
                            row["enqueue_sequence"] if row else token["sequence"]
                        ),
                        "source_id": stored_source or source_value or job_id,
                        "auth_state_id": (
                            row["auth_state_id"] if row else item["auth_state_id"]
                        ),
                    }
                )
        return {"result": "admitted", "jobs": jobs}

    def create_job(
        self,
        request: CollectionRequest,
        execution_plan: dict[str, Any],
        *,
        source_id: str | None = None,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    ) -> str:
        result = self._admit_job(
            request,
            execution_plan,
            queue_capacity=2_147_483_647,
            priority=0,
            source_id=source_id,
            auth_state_id=f"provider:{request.provider.value}",
            batch_id=None,
            idempotency_key=None,
            approval={},
            limits={},
            deadline_at=datetime.now(UTC) + timedelta(hours=1),
            stale_after_seconds=stale_after_seconds,
        )
        return str(result["job_id"])

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
        if not isinstance(value, str):
            return False
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

    @staticmethod
    def _safe_int(
        value: Any, *, default: int | None = None
    ) -> tuple[int | None, bool]:
        if value is None and default is None:
            return None, True
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value, True
        return default, False

    @staticmethod
    def _safe_bool(value: Any, *, nullable: bool = False) -> tuple[bool | None, bool]:
        if value is None and nullable:
            return None, True
        if isinstance(value, int) and not isinstance(value, bool) and value in {0, 1}:
            return bool(value), True
        return (None if nullable else False), False

    @staticmethod
    def _safe_timestamp(value: Any) -> tuple[str | None, bool]:
        if value is None:
            return None, True
        if not isinstance(value, str) or len(value) > 64:
            return None, False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None, False
        return (value, True) if parsed.utcoffset() is not None else (None, False)

    @staticmethod
    def _safe_fingerprint(value: Any) -> tuple[str | None, bool]:
        if value is None:
            return None, True
        valid = (
            isinstance(value, str)
            and len(value) == 67
            and value.startswith("v1:")
            and all(character in "0123456789abcdef" for character in value[3:])
        )
        return (value, True) if valid else (None, False)

    @classmethod
    def _job_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        storage_warnings = []
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
        provider_value = provider.value if provider else "unknown"
        request["provider"] = provider_value
        scalar_valid = True
        for name, default in (
            ("collected_count", 0),
            ("post_resource_count", 0),
            ("user_resource_count", 0),
            ("media_resource_count", 0),
            ("stale_after_seconds", DEFAULT_STALE_AFTER_SECONDS),
            ("rate_limit_remaining", None),
            ("rate_limit_reset", None),
            ("capture_segment", None),
            ("queue_priority", 0),
            ("enqueue_sequence", 0),
            ("attempt_number", 0),
        ):
            item[name], valid = cls._safe_int(item.get(name), default=default)
            scalar_valid &= valid
        if int(item["queue_priority"] or 0) > 100 or int(item["enqueue_sequence"] or 0) < 1:
            item["queue_priority"] = 0
            item["enqueue_sequence"] = 0
            scalar_valid = False
        for name, nullable in (
            ("error_retryable", False),
            ("cancel_requested", False),
            ("snapshot_partial", True),
            ("truncated", True),
            ("reuse_eligible", False),
        ):
            item[name], valid = cls._safe_bool(item.get(name), nullable=nullable)
            scalar_valid &= valid
        for name in (
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
            "retry_at",
            "snapshot_at",
            "deadline_at",
            "leased_at",
            "lease_heartbeat_at",
            "lease_expires_at",
        ):
            item[name], valid = cls._safe_timestamp(item.get(name))
            scalar_valid &= valid
        if item["deadline_at"] is None:
            scalar_valid = False
        try:
            item["status"] = JobStatus(str(item["status"])).value
        except ValueError:
            item["status"] = JobStatus.FAILED.value
            scalar_valid = False
        for name in ("source_fingerprint", "request_fingerprint"):
            item[name], valid = cls._safe_fingerprint(item.get(name))
            scalar_valid &= valid
        source_id = item.get("source_id")
        if source_id is not None and (
            not isinstance(source_id, str) or not 1 <= len(source_id) <= 128
        ):
            item["source_id"] = None
            scalar_valid = False
        parser_version = item.get("parser_version")
        if parser_version is not None and (
            not isinstance(parser_version, str) or not 1 <= len(parser_version) <= 64
        ):
            item["parser_version"] = None
            scalar_valid = False
        for name, nullable in (
            ("auth_state_id", False),
            ("batch_id", True),
            ("idempotency_key", True),
            ("lease_owner", True),
        ):
            value = item.get(name)
            if value is None and nullable:
                continue
            if not isinstance(value, str) or not 1 <= len(value) <= 128:
                item[name] = None if nullable else "invalid"
                scalar_valid = False
        public_records_valid = True
        for column, allowed, public_name in (
            ("approval_json", APPROVAL_KEYS, "approval_recorded"),
            ("limits_json", LIMIT_KEYS, "limits_recorded"),
        ):
            decoded, decoded_valid = cls._decode_json(item.pop(column), dict)
            try:
                cls._public_record(decoded, allowed=allowed, label=column.removesuffix("_json"))
            except ValueError:
                decoded_valid = False
            item[public_name] = decoded_valid
            public_records_valid &= decoded_valid
        raw_coverage = item.pop("coverage_json")
        coverage, coverage_valid = (
            ({}, True)
            if raw_coverage is None and item["snapshot_at"] is None
            else cls._decode_json(raw_coverage, dict)
        )
        item["coverage"] = coverage
        raw_cursor = item["cursor"]
        cursor_valid = cls._provider_state_is_valid(raw_cursor)
        provider_state, checkpoint_metadata = (
            cls._decode_provider_state(raw_cursor) if cursor_valid else (None, {})
        )
        checkpoint_metadata = {
            **checkpoint_metadata,
            "captureSegment": item["capture_segment"],
        }
        if provider is ProviderType.OFFICIAL_X_API:
            checkpoint_metadata = {
                **checkpoint_metadata,
                "resourcesReturned": {
                    "posts": item["post_resource_count"],
                    "users": item["user_resource_count"],
                    "media": item["media_resource_count"],
                },
                "rateLimitRemaining": item["rate_limit_remaining"],
                "rateLimitReset": item["rate_limit_reset"],
            }
        item["request"] = request
        item["provider"] = provider_value
        item["execution_plan"] = execution_plan
        item["compiled_request"] = execution_plan
        item["provider_state"] = provider_state
        item["cursor"] = provider_state
        item["checkpoint"] = {
            "providerState": provider_state,
            "storedCount": item["collected_count"],
            "metadata": checkpoint_metadata,
        }
        if not request_valid:
            storage_warnings.append("Stored job request metadata is unreadable.")
        if not plan_valid:
            storage_warnings.append("Stored execution-plan metadata is unreadable.")
        if not warnings_valid:
            storage_warnings.append("Stored job warnings are unreadable.")
        if provider is None:
            storage_warnings.append("Stored collection provider is unknown.")
        if not cursor_valid:
            storage_warnings.append("Stored provider checkpoint is unreadable.")
        if not scalar_valid or not coverage_valid or not public_records_valid:
            storage_warnings.append("Stored job scalar metadata is unreadable.")
        item["stored_metadata_valid"] = not storage_warnings
        item["warnings"] = [
            *[warning for warning in warnings if isinstance(warning, str)],
            *storage_warnings,
        ]
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

    def list_attempts(
        self,
        limit: int = 50,
        *,
        offset: int = 0,
        source_id: str | None = None,
        source_fingerprint: str | None = None,
    ) -> list[dict[str, Any]]:
        self._validate_page(limit, offset)
        clauses = []
        parameters: list[Any] = []
        for column, value in (
            ("source_id", source_id),
            ("source_fingerprint", source_fingerprint),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return [self._job_dict(row) for row in rows]

    def list_batch_jobs(
        self, batch_id: str, limit: int = 100, *, offset: int = 0
    ) -> list[dict[str, Any]]:
        batch_id = self._queue_identifier(
            batch_id, label="batch_id"
        )  # type: ignore[assignment]
        self._validate_page(limit, offset)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE batch_id = ? "
                "ORDER BY enqueue_sequence LIMIT ? OFFSET ?",
                (batch_id, limit, offset),
            ).fetchall()
        return [self._job_dict(row) for row in rows]

    def cancel_batch(self, batch_id: str) -> int:
        batch_id = self._queue_identifier(
            batch_id, label="batch_id"
        )  # type: ignore[assignment]
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE batch_id = ? "
                "AND status IN ('queued', 'running', 'waiting')",
                (batch_id,),
            ).fetchall()
        return sum(self.request_cancel(row["id"]) for row in rows)

    @staticmethod
    def _snapshot_now(now: datetime | None) -> datetime:
        current = now or datetime.now(UTC)
        if current.utcoffset() is None:
            raise ValueError("now must include a timezone.")
        return current.astimezone(UTC)

    @classmethod
    def _enrich_snapshot(
        cls, job: dict[str, Any], *, now: datetime | None = None
    ) -> dict[str, Any]:
        snapshot_at = job.get("snapshot_at")
        if snapshot_at is None:
            age_seconds = None
            freshness = "unknown"
        else:
            captured = datetime.fromisoformat(snapshot_at).astimezone(UTC)
            age_seconds = max(0, int((cls._snapshot_now(now) - captured).total_seconds()))
            freshness = (
                "fresh"
                if age_seconds <= int(job["stale_after_seconds"])
                else "stale"
            )
        usable = bool(
            job.get("status") in {JobStatus.SUCCEEDED.value, JobStatus.PARTIAL.value}
            and int(job.get("collected_count") or 0) > 0
            and job.get("stored_metadata_valid")
            and snapshot_at is not None
        )
        return {
            **job,
            "age_seconds": age_seconds,
            "freshness": freshness,
            "usable": usable,
        }

    def get_snapshot(
        self, snapshot_id: str, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        job = self.get_job(snapshot_id)
        if not job or job["status"] not in TERMINAL_STATUSES:
            return None
        return self._enrich_snapshot(job, now=now)

    def list_snapshots(
        self,
        limit: int = 50,
        *,
        offset: int = 0,
        source_id: str | None = None,
        source_fingerprint: str | None = None,
        request_fingerprint: str | None = None,
        usable: bool | None = None,
        compatible_only: bool = False,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        self._validate_page(limit, offset)
        if compatible_only and not (source_fingerprint or request_fingerprint):
            raise ValueError("compatible_only requires a source or request fingerprint.")
        clauses = ["status IN ('succeeded', 'partial', 'failed', 'cancelled', 'interrupted')"]
        parameters: list[Any] = []
        for column, value in (
            ("source_id", source_id),
            ("source_fingerprint", source_fingerprint),
            ("request_fingerprint", request_fingerprint),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        if usable is True:
            clauses.append("status IN ('succeeded', 'partial')")
            clauses.append("collected_count > 0 AND snapshot_at IS NOT NULL")
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                SELECT * FROM jobs WHERE {' AND '.join(clauses)}
                ORDER BY COALESCE(snapshot_at, finished_at, updated_at, created_at) DESC,
                         id DESC
                """,
                parameters,
            )
            if usable is None:
                rows = cursor.fetchmany(offset + limit)[offset:]
                return [
                    self._enrich_snapshot(self._job_dict(row), now=now) for row in rows
                ]
            snapshots = []
            matching = 0
            for row in cursor:
                snapshot = self._enrich_snapshot(self._job_dict(row), now=now)
                if snapshot["usable"] is not usable:
                    continue
                if matching >= offset:
                    snapshots.append(snapshot)
                    if len(snapshots) == limit:
                        break
                matching += 1
        return snapshots

    def get_latest_usable_snapshot(
        self,
        *,
        source_id: str | None = None,
        source_fingerprint: str | None = None,
        request_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        rows = self.list_snapshots(
            1,
            source_id=source_id,
            source_fingerprint=source_fingerprint,
            request_fingerprint=request_fingerprint,
            usable=True,
            now=now,
        )
        return rows[0] if rows else None

    def list_queued_jobs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT id, queue_priority, enqueue_sequence, source_id,
                           source_fingerprint, auth_state_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY queue_priority,
                                   COALESCE(source_id, source_fingerprint, id)
                               ORDER BY enqueue_sequence
                           ) AS source_turn,
                           MIN(enqueue_sequence) OVER (
                               PARTITION BY queue_priority,
                                   COALESCE(source_id, source_fingerprint, id)
                           ) AS source_first
                    FROM jobs
                    WHERE status = 'queued' AND cancel_requested = 0
                )
                SELECT id, queue_priority, enqueue_sequence,
                       COALESCE(source_id, source_fingerprint, id) AS source_id,
                       auth_state_id
                FROM ranked
                ORDER BY queue_priority DESC, source_turn, source_first, enqueue_sequence
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "priority": row["queue_priority"],
                "enqueue_sequence": row["enqueue_sequence"],
                "source_id": row["source_id"],
                "auth_state_id": row["auth_state_id"],
            }
            for row in rows
        ]

    def lease_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_expires_at: str | datetime,
    ) -> dict[str, Any] | None:
        worker_id = self._queue_identifier(
            worker_id, label="worker_id"
        )  # type: ignore[assignment]
        lease_expiry = self._deadline(lease_expires_at)
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row or row["status"] != JobStatus.QUEUED.value:
                return None
            if row["cancel_requested"]:
                return None
            try:
                deadline = datetime.fromisoformat(row["deadline_at"]).astimezone(UTC)
            except (TypeError, ValueError):
                return None
            current = datetime.fromisoformat(now)
            if deadline <= current or datetime.fromisoformat(lease_expiry) > deadline:
                return None
            conflict = connection.execute(
                """
                SELECT 1 FROM jobs AS active
                WHERE active.id != ? AND active.status = 'running'
                  AND active.lease_owner IS NOT NULL
                  AND julianday(active.lease_expires_at) > julianday(?)
                  AND (
                      active.auth_state_id = ?
                      OR (? IS NOT NULL AND active.source_id = ?)
                      OR (? IS NULL AND active.source_id IS NULL
                          AND active.source_fingerprint = ?)
                  )
                LIMIT 1
                """,
                (
                    job_id,
                    now,
                    row["auth_state_id"],
                    row["source_id"],
                    row["source_id"],
                    row["source_id"],
                    row["source_fingerprint"],
                ),
            ).fetchone()
            if conflict:
                return None
            changed = connection.execute(
                """
                UPDATE jobs SET status = 'running',
                    started_at = COALESCE(started_at, ?), finished_at = NULL,
                    retry_at = NULL, error_code = NULL, error_message = NULL,
                    error_retryable = 0, completion_reason = NULL,
                    attempt_number = attempt_number + 1,
                    capture_segment = COALESCE(capture_segment, -1) + 1,
                    lease_owner = ?, leased_at = ?, lease_heartbeat_at = ?,
                    lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued' AND cancel_requested = 0
                """,
                (
                    now,
                    worker_id,
                    now,
                    now,
                    lease_expiry,
                    now,
                    job_id,
                ),
            ).rowcount
            if not changed:
                return None
            if row["source_id"] is not None:
                connection.execute(
                    "UPDATE sources SET last_status = 'running' WHERE id = ?",
                    (row["source_id"],),
                )
            leased = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._job_dict(leased)

    def heartbeat_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_expires_at: str | datetime,
    ) -> bool:
        worker_id = self._queue_identifier(
            worker_id, label="worker_id"
        )  # type: ignore[assignment]
        expiry = self._deadline(lease_expires_at)
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT deadline_at FROM jobs WHERE id = ? AND status = 'running' "
                "AND lease_owner = ? AND cancel_requested = 0",
                (job_id, worker_id),
            ).fetchone()
            if not row:
                return False
            try:
                if datetime.fromisoformat(expiry) > datetime.fromisoformat(
                    row["deadline_at"]
                ):
                    return False
            except (TypeError, ValueError):
                return False
            changed = connection.execute(
                """
                UPDATE jobs SET lease_heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                  AND cancel_requested = 0
                """,
                (now, expiry, now, job_id, worker_id),
            ).rowcount
        return bool(changed)

    def queue_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
                )
            }
            leased = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'running' "
                "AND lease_owner IS NOT NULL"
            ).fetchone()[0]
        queued = int(counts.get(JobStatus.QUEUED.value, 0))
        running = int(counts.get(JobStatus.RUNNING.value, 0))
        waiting = int(counts.get(JobStatus.WAITING.value, 0))
        return {
            "queued": queued,
            "running": running,
            "waiting": waiting,
            "leased": int(leased),
            "active": queued + running + waiting,
        }

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
                    attempt_number = attempt_number + 1,
                    capture_segment = COALESCE(capture_segment, -1) + 1, updated_at = ?
                WHERE id = ? AND status = 'queued' AND cancel_requested = 0
                  AND julianday(deadline_at) > julianday(?)
                """,
                (now, now, job_id, now),
            ).rowcount
            row = (
                connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if changed
                else None
            )
            if changed:
                connection.execute(
                    """
                    UPDATE sources SET last_status = 'running'
                    WHERE id = (SELECT source_id FROM jobs WHERE id = ?)
                    """,
                    (job_id,),
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
                    connection.execute(
                        "INSERT INTO post_observations_fts(text, job_id, post_id) "
                        "VALUES (?, ?, ?)",
                        (post.text or "", job_id, post.post_id),
                    )
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
        worker_id: str | None = None,
    ) -> str | None:
        return self._transition_job(
            job_id,
            allowed={JobStatus.RUNNING.value},
            target=JobStatus.PARTIAL if partial else JobStatus.SUCCEEDED,
            warnings=warnings,
            completion_reason=completion_reason,
            worker_id=worker_id,
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
        worker_id: str | None = None,
    ) -> str | None:
        if worker_id is not None:
            worker_id = self._queue_identifier(
                worker_id, label="worker_id"
            )  # type: ignore[assignment]
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, cancel_requested, collected_count, cursor, source_id, "
                "lease_owner FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if (
                not row
                or row["status"] not in allowed
                or worker_id is not None
                and row["lease_owner"] != worker_id
            ):
                return None
            if row["cancel_requested"]:
                target = JobStatus.CANCELLED
                completion_reason = error_code = "cancelled"
                error_message = "Collection cancelled."
                retryable = True
                warnings = None
            elif target is JobStatus.WAITING:
                target = (
                    JobStatus.PARTIAL
                    if isinstance(row["collected_count"], int) and row["collected_count"] > 0
                    else JobStatus.FAILED
                )
                error_code = completion_reason = "rate_limited"
                retryable = False
            _, checkpoint_metadata = self._decode_provider_state(row["cursor"])
            coverage = checkpoint_metadata.get("fieldCoverage")
            coverage = coverage if isinstance(coverage, dict) else {}
            explicit_truncated = checkpoint_metadata.get("truncated")
            truncated = (
                explicit_truncated
                if isinstance(explicit_truncated, bool)
                else completion_reason
                in {"target_reached", "post_resource_limit_reached"}
            )
            collected = (
                row["collected_count"]
                if isinstance(row["collected_count"], int)
                and not isinstance(row["collected_count"], bool)
                and row["collected_count"] >= 0
                else 0
            )
            snapshot_partial = target is JobStatus.PARTIAL or (
                target is not JobStatus.SUCCEEDED and collected > 0
            )
            changed = connection.execute(
                """
                UPDATE jobs SET status = ?,
                    warnings_json = COALESCE(?, warnings_json),
                    error_code = ?, error_message = ?, error_retryable = ?,
                    completion_reason = ?, retry_at = NULL,
                    rate_limit_remaining = COALESCE(?, rate_limit_remaining),
                    rate_limit_reset = COALESCE(?, rate_limit_reset),
                    cancel_requested = CASE WHEN ? = 'cancelled' THEN 1
                                            ELSE cancel_requested END,
                    snapshot_at = COALESCE(snapshot_at, ?),
                    snapshot_partial = COALESCE(snapshot_partial, ?),
                    coverage_json = COALESCE(coverage_json, ?),
                    truncated = COALESCE(truncated, ?),
                    lease_owner = NULL, leased_at = NULL,
                    lease_heartbeat_at = NULL, lease_expires_at = NULL,
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
                    target.value,
                    now,
                    int(snapshot_partial),
                    json.dumps(coverage, separators=(",", ":"), sort_keys=True),
                    int(truncated),
                    now,
                    now,
                    job_id,
                    row["status"],
                ),
            ).rowcount
            if changed and row["source_id"] is not None:
                connection.execute(
                    "UPDATE sources SET last_status = ? WHERE id = ?",
                    (target.value, row["source_id"]),
                )
        return target.value if changed else None

    def fail_job(
        self,
        job_id: str,
        status: JobStatus,
        code: str,
        message: str,
        retryable: bool,
        *,
        worker_id: str | None = None,
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
            worker_id=worker_id,
        )

    def wait_job(
        self,
        job_id: str,
        retry_at: str,
        remaining: int | None,
        reset: int | None,
        message: str,
        *,
        worker_id: str | None = None,
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
            worker_id=worker_id,
        )

    def request_cancel(self, job_id: str) -> bool:
        return bool(
            self._transition_job(
                job_id,
                allowed={
                    JobStatus.QUEUED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.WAITING.value,
                },
                target=JobStatus.CANCELLED,
                completion_reason="cancelled",
                error_code="cancelled",
                error_message="Collection cancelled.",
                retryable=True,
            )
        )

    def delete_job(self, job_id: str) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            terminal = connection.execute(
                "SELECT 1 FROM jobs WHERE id = ? "
                "AND status NOT IN ('queued', 'running', 'waiting')",
                (job_id,),
            ).fetchone()
            if not terminal:
                return False
            connection.execute(
                "DELETE FROM post_observations_fts WHERE job_id = ?", (job_id,)
            )
            changed = connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,)).rowcount
        return bool(changed)

    def cancel_requested(self, job_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and row[0])

    def resume_job(self, job_id: str) -> bool:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE jobs SET status = 'queued', cancel_requested = 0,
                    error_code = NULL, error_message = NULL, error_retryable = 0,
                    completion_reason = NULL, finished_at = NULL, retry_at = NULL,
                    snapshot_at = NULL, snapshot_partial = NULL,
                    coverage_json = NULL, truncated = NULL,
                    lease_owner = NULL, leased_at = NULL,
                    lease_heartbeat_at = NULL, lease_expires_at = NULL,
                    enqueue_sequence = (SELECT MAX(enqueue_sequence) + 1 FROM jobs),
                    updated_at = ?
                WHERE id = ? AND (
                    status IN ('cancelled', 'interrupted', 'partial')
                    OR (status = 'failed' AND error_retryable = 1)
                )
                AND COALESCE(error_code, '') NOT IN (
                    'manual_action_required', 'session_expired',
                    'rate_limited', 'browser_rate_limited'
                )
                AND julianday(deadline_at) > julianday(?)
                """,
                (now, job_id, now),
            ).rowcount
        return bool(changed)

    def recover_jobs(self, now: datetime | str | None = None) -> list[str]:
        now_value = (
            datetime.now(UTC).isoformat()
            if now is None
            else self._deadline(now, require_future=False)
        )
        with self.connect() as connection:
            active = connection.execute(
                "SELECT id, status, cancel_requested, collected_count, error_code, "
                "deadline_at FROM jobs WHERE status IN ('queued', 'running', 'waiting')"
            ).fetchall()
        for row in active:
            allowed = {row["status"]}
            if row["cancel_requested"]:
                self._transition_job(
                    row["id"],
                    allowed=allowed,
                    target=JobStatus.CANCELLED,
                    completion_reason="cancelled",
                    error_code="cancelled",
                    error_message="Collection cancellation was preserved across restart.",
                    retryable=True,
                )
                continue
            if row["status"] == JobStatus.WAITING.value:
                self._transition_job(
                    row["id"],
                    allowed=allowed,
                    target=JobStatus.WAITING,
                    completion_reason="rate_limited",
                    error_code="rate_limited",
                    error_message="Rate-limited collection cannot be resumed automatically.",
                )
                continue
            try:
                deadline_expired = datetime.fromisoformat(
                    row["deadline_at"]
                ) <= datetime.fromisoformat(now_value)
            except (TypeError, ValueError):
                deadline_expired = True
            blocked_code = row["error_code"] in {
                "manual_action_required",
                "session_expired",
                "rate_limited",
                "browser_rate_limited",
                "login_required",
                "challenge_detected",
            }
            if deadline_expired or blocked_code:
                collected = row["collected_count"]
                target = (
                    JobStatus.PARTIAL
                    if isinstance(collected, int) and collected > 0
                    else JobStatus.FAILED
                )
                code = (
                    row["error_code"]
                    if blocked_code
                    else "deadline_exceeded"
                )
                self._transition_job(
                    row["id"],
                    allowed=allowed,
                    target=target,
                    completion_reason=code,
                    error_code=code,
                    error_message="Collection cannot be resumed automatically.",
                )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE jobs SET status = 'queued', error_code = 'interrupted',
                    error_message = 'Server restarted; job queued for automatic resume.',
                    error_retryable = 1, cancel_requested = 0,
                    completion_reason = 'interrupted',
                    lease_owner = NULL, leased_at = NULL,
                    lease_heartbeat_at = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE status = 'running' AND cancel_requested = 0
                  AND julianday(deadline_at) > julianday(?)
                  AND (
                      lease_owner IS NULL OR lease_expires_at IS NULL
                      OR julianday(lease_expires_at) <= julianday(?)
                  )
                """,
                (now_value, now_value, now_value),
            )
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = 'queued' AND cancel_requested = 0 "
                "AND julianday(deadline_at) > julianday(?) "
                "ORDER BY queue_priority DESC, enqueue_sequence",
                (now_value,),
            ).fetchall()
        return [row[0] for row in rows]

    def requeue_due_jobs(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = 'waiting'"
            ).fetchall()
        for row in rows:
            self._transition_job(
                row["id"],
                allowed={JobStatus.WAITING.value},
                target=JobStatus.WAITING,
                completion_reason="rate_limited",
                error_code="rate_limited",
                error_message="Rate-limited collection cannot be resumed automatically.",
            )
        return []

    def get_job_posts(
        self, job_id: str, *, limit: int = 500, offset: int = 0
    ) -> list[dict[str, Any]]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 10_000
        ):
            raise ValueError("Post pagination requires limit 1–500 and offset 0–10000.")
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

    @staticmethod
    def _time_filter(value: datetime | str | None, *, label: str) -> str | None:
        if value is None:
            return None
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be an ISO-8601 timestamp.") from exc
        if parsed.utcoffset() is None:
            raise ValueError(f"{label} must include a timezone.")
        return parsed.astimezone(UTC).isoformat()

    @staticmethod
    def _fts_query(value: str) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ValueError("query must contain 1–256 characters.")
        if any(not character.isprintable() and not character.isspace() for character in value):
            raise ValueError("query contains unsupported control characters.")
        if any(character in "\r\n\t" for character in value):
            raise ValueError("query contains unsupported control characters.")
        tokens = re.findall(r"\w+", value, flags=re.UNICODE)
        if not tokens:
            raise ValueError("query must contain at least one searchable token.")
        return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)

    def search_post_evidence(
        self,
        query: str,
        *,
        source_ids: list[str] | None = None,
        snapshot_ids: list[str] | None = None,
        start_time: datetime | str | None = None,
        end_time: datetime | str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._validate_page(limit, offset)
        match = self._fts_query(query)
        start = self._time_filter(start_time, label="start_time")
        end = self._time_filter(end_time, label="end_time")
        if start and end and datetime.fromisoformat(start) > datetime.fromisoformat(end):
            raise ValueError("start_time cannot be after end_time.")
        sources = list(dict.fromkeys(source_ids or []))
        snapshots = list(dict.fromkeys(snapshot_ids or []))
        if len(sources) > 100 or len(snapshots) > 100:
            raise ValueError("At most 100 source or snapshot ids may be selected.")
        for label, values in (("source_id", sources), ("snapshot_id", snapshots)):
            for value in values:
                self._queue_identifier(value, label=label)
        clauses = [
            "post_observations_fts MATCH ?",
            "j.status IN ('succeeded', 'partial', 'failed', 'cancelled', 'interrupted')",
        ]
        parameters: list[Any] = [match]
        if sources:
            clauses.append(f"j.source_id IN ({', '.join('?' for _ in sources)})")
            parameters.extend(sources)
        if snapshots:
            clauses.append(f"j.id IN ({', '.join('?' for _ in snapshots)})")
            parameters.extend(snapshots)
        if start:
            clauses.append(
                "julianday(COALESCE(j.snapshot_at, o.observed_at)) >= julianday(?)"
            )
            parameters.append(start)
        if end:
            clauses.append(
                "julianday(COALESCE(j.snapshot_at, o.observed_at)) <= julianday(?)"
            )
            parameters.append(end)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT o.*, j.id AS snapshot_id, j.source_id AS evidence_source_id,
                       j.source_fingerprint AS evidence_source_fingerprint,
                       j.request_fingerprint AS evidence_request_fingerprint,
                       j.parser_version AS evidence_parser_version,
                       j.snapshot_at AS evidence_snapshot_at,
                       j.snapshot_partial AS evidence_snapshot_partial,
                       j.coverage_json AS evidence_coverage_json,
                       j.truncated AS evidence_truncated,
                       j.reuse_eligible AS evidence_reuse_eligible
                FROM post_observations_fts
                JOIN post_observations AS o
                  ON o.job_id = post_observations_fts.job_id
                 AND o.post_id = post_observations_fts.post_id
                JOIN jobs AS j ON j.id = o.job_id
                WHERE {' AND '.join(clauses)}
                ORDER BY bm25(post_observations_fts),
                         COALESCE(j.snapshot_at, j.finished_at, j.updated_at) DESC,
                         o.snapshot_position, j.id, o.post_id
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
        evidence = []
        metadata_keys = {
            "snapshot_id",
            "evidence_source_id",
            "evidence_source_fingerprint",
            "evidence_request_fingerprint",
            "evidence_parser_version",
            "evidence_snapshot_at",
            "evidence_snapshot_partial",
            "evidence_coverage_json",
            "evidence_truncated",
            "evidence_reuse_eligible",
        }
        for row in rows:
            raw = dict(row)
            observation = {key: value for key, value in raw.items() if key not in metadata_keys}
            job_id = observation.pop("job_id")
            media = observation.pop("media_json")
            if media is None:
                observation["media"] = None
            else:
                observation["media"], valid_media = self._decode_json(media, list)
                if not valid_media:
                    observation["media"] = None
                    observation["storage_warnings"] = [
                        "Stored media metadata is unreadable."
                    ]
            for name in ("is_reply", "is_repost", "is_quote", "has_media"):
                if observation[name] is not None:
                    observation[name] = bool(observation[name])
            observation["source_position"] = observation["dom_position"]
            coverage, coverage_valid = self._decode_json(
                raw["evidence_coverage_json"], dict
            )
            observation.update(
                {
                    "evidence_id": f"{job_id}:{observation['post_id']}",
                    "snapshot_id": raw["snapshot_id"],
                    "source_id": raw["evidence_source_id"],
                    "source_fingerprint": raw["evidence_source_fingerprint"],
                    "request_fingerprint": raw["evidence_request_fingerprint"],
                    "parser_version": raw["evidence_parser_version"],
                    "snapshot_at": raw["evidence_snapshot_at"],
                    "snapshot_partial": bool(raw["evidence_snapshot_partial"]),
                    "coverage": coverage if coverage_valid else {},
                    "truncated": bool(raw["evidence_truncated"]),
                    "reuse_eligible": bool(raw["evidence_reuse_eligible"]),
                    "untrusted_external_content": True,
                }
            )
            evidence.append(observation)
        return evidence

    def purge_snapshots(
        self,
        *,
        keep_per_source: int,
        older_than: datetime | str | None = None,
    ) -> int:
        if (
            isinstance(keep_per_source, bool)
            or not isinstance(keep_per_source, int)
            or not 0 <= keep_per_source <= 10_000
        ):
            raise ValueError("keep_per_source must be between 0 and 10000.")
        cutoff = self._time_filter(older_than, label="older_than")
        clauses = ["retention_rank > ?"]
        parameters: list[Any] = [keep_per_source]
        if cutoff is not None:
            clauses.append(
                "julianday(COALESCE(snapshot_at, finished_at, updated_at, created_at)) "
                "< julianday(?)"
            )
            parameters.append(cutoff)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                WITH terminal AS (
                    SELECT id, snapshot_at, finished_at, updated_at, created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(source_id, source_fingerprint, id)
                               ORDER BY COALESCE(
                                   snapshot_at, finished_at, updated_at, created_at
                               ) DESC, id DESC
                           ) AS retention_rank
                    FROM jobs
                    WHERE status IN (
                        'succeeded', 'partial', 'failed', 'cancelled', 'interrupted'
                    )
                )
                SELECT id FROM terminal WHERE {' AND '.join(clauses)}
                """,
                parameters,
            ).fetchall()
            job_ids = [row["id"] for row in rows]
            if not job_ids:
                return 0
            changed = 0
            for start in range(0, len(job_ids), 500):
                batch = job_ids[start : start + 500]
                placeholders = ", ".join("?" for _ in batch)
                connection.execute(
                    f"DELETE FROM post_observations_fts "
                    f"WHERE job_id IN ({placeholders})",
                    batch,
                )
                changed += connection.execute(
                    f"DELETE FROM jobs WHERE id IN ({placeholders})", batch
                ).rowcount
        return changed
