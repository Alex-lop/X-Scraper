import json
import sqlite3
import stat

import pytest

from xworkbench.models import CollectionRequest, JobStatus, Post
from xworkbench.storage import SCHEMA_FAMILY, SCHEMA_VERSION, Storage
from xworkbench.x_api import compile_request


def collection():
    return CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )


def post(post_id="1", *, source_position=None, url=None):
    return Post(
        post_id=post_id,
        text="  exact text\n",
        author_username=None,
        author_id="7",
        url=url if url is not None else f"https://x.com/i/web/status/{post_id}",
        created_at="2026-08-05T12:00:00Z",
        language="en",
        like_count=None,
        reply_count=0,
        view_count=123,
        is_reply=True,
        media=[{"id": "media-1", "type": "photo"}],
        source_position=source_position,
    )


def _make_legacy(storage, version):
    storage.initialize()
    with storage.connect() as connection:
        connection.execute("DROP INDEX idx_observations_position")
        connection.execute("DROP TABLE post_observations")
        connection.execute("ALTER TABLE jobs DROP COLUMN capture_segment")
        null = "" if version == 2 else " NOT NULL"
        defaults = "" if version == 2 else " DEFAULT 0"
        media_default = "" if version == 2 else " DEFAULT '[]'"
        connection.executescript(
            f"""
            CREATE TABLE post_observations (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                post_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                text TEXT{null},
                author_id TEXT,
                author_username TEXT,
                url TEXT NOT NULL,
                created_at TEXT,
                observed_at TEXT NOT NULL,
                language TEXT,
                conversation_id TEXT,
                in_reply_to_post_id TEXT,
                like_count INTEGER,
                reply_count INTEGER,
                repost_count INTEGER,
                quote_count INTEGER,
                bookmark_count INTEGER,
                is_reply INTEGER{null}{defaults},
                is_repost INTEGER{null}{defaults},
                is_quote INTEGER{null}{defaults},
                has_media INTEGER{null}{defaults},
                media_json TEXT{null}{media_default},
                PRIMARY KEY (job_id, post_id),
                UNIQUE (job_id, position)
            );
            CREATE INDEX idx_observations_position
                ON post_observations(job_id, position);
            """
        )
        connection.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", (str(version),)
        )


def _create_job(storage):
    request = collection()
    return storage.create_job(request, compile_request(request))


def test_clean_v3_schema_is_secure_and_idempotent(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    storage.initialize()

    with storage.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))

    assert tables == {"schema_meta", "jobs", "post_observations"}
    assert metadata == {"schema_family": SCHEMA_FAMILY, "schema_version": SCHEMA_VERSION}
    assert stat.S_IMODE(storage.path.stat().st_mode) == 0o600


def test_batch_persists_only_duplicate_post_ids_and_truthful_positions(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    job_id = _create_job(storage)
    assert storage.claim_job(job_id)["status"] == "running"

    added = storage.add_posts(
        job_id,
        [post(source_position=7), post(source_position=8)],
        "next-page",
        {
            "scanIterations": 3,
            "resourcesReturned": {"posts": 2, "users": 1, "media": 1},
            "warnings": ["X API partial response: author unavailable"],
            "rateLimitRemaining": 44,
            "rateLimitReset": 123,
        },
    )

    assert added == 1
    job = storage.get_job(job_id)
    assert job["collected_count"] == 1 and job["cursor"] == "next-page"
    assert job["warnings"] == ["X API partial response: author unavailable"]
    assert (
        job["post_resource_count"],
        job["user_resource_count"],
        job["media_resource_count"],
    ) == (2, 1, 1)
    stored = storage.get_job_posts(job_id)[0]
    assert stored["snapshot_position"] == 0
    assert stored["capture_segment"] == 0
    assert stored["scan_ordinal"] == 3
    assert stored["dom_position"] == stored["source_position"] == 7
    assert stored["view_count"] == 123
    assert stored["media"][0]["id"] == "media-1"


def test_batch_failure_rolls_back_posts_and_checkpoint(tmp_path):
    storage = Storage(tmp_path / "rollback.db")
    storage.initialize()
    job_id = _create_job(storage)
    storage.claim_job(job_id)
    invalid = post("2")
    invalid.url = None

    with pytest.raises(sqlite3.IntegrityError):
        storage.add_posts(
            job_id,
            [post("1"), invalid],
            "must-not-persist",
            {"warnings": ["must-not-persist"], "resourcesReturned": {"posts": 2}},
        )

    job = storage.get_job(job_id)
    assert storage.count_job_posts(job_id) == 0
    assert job["collected_count"] == job["post_resource_count"] == 0
    assert job["cursor"] is None and job["warnings"] == []


def test_add_posts_requires_running_non_cancelled_job_and_terminal_is_immutable(tmp_path):
    storage = Storage(tmp_path / "states.db")
    storage.initialize()
    job_id = _create_job(storage)
    assert storage.add_posts(job_id, [post()], "no", {}) == 0
    storage.claim_job(job_id)
    assert storage.request_cancel(job_id)
    assert storage.add_posts(job_id, [post()], "no", {}) == 0
    storage.fail_job(job_id, JobStatus.FAILED, "late", "late", True)
    storage.wait_job(job_id, "later", 0, 1, "late")
    assert storage.finish_job(job_id, [], completion_reason="late") is None
    assert storage.get_job(job_id)["status"] == "cancelled"
    assert storage.count_job_posts(job_id) == 0


@pytest.mark.parametrize("transition", ["finish", "fail", "wait"])
def test_persisted_cancellation_flag_wins_terminal_transition_races(tmp_path, transition):
    storage = Storage(tmp_path / f"{transition}.db")
    storage.initialize()
    job_id = _create_job(storage)
    storage.claim_job(job_id)
    with storage.connect() as connection:
        connection.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))

    if transition == "finish":
        storage.finish_job(job_id, [], completion_reason="late_success")
    elif transition == "fail":
        storage.fail_job(job_id, JobStatus.FAILED, "late_failure", "late", True)
    else:
        storage.wait_job(job_id, "later", 0, 1, "late wait")

    job = storage.get_job(job_id)
    assert (job["status"], job["error_code"], job["completion_reason"]) == (
        "cancelled",
        "cancelled",
        "cancelled",
    )


def test_resume_starts_a_new_capture_segment(tmp_path):
    storage = Storage(tmp_path / "segments.db")
    storage.initialize()
    job_id = _create_job(storage)
    assert storage.claim_job(job_id)["capture_segment"] == 0
    storage.add_posts(
        job_id,
        [post("1", source_position=4)],
        None,
        {"scanIterations": 8, "segmentScanIterations": 1},
    )
    storage.finish_job(job_id, [], completion_reason="bounded_stop", partial=True)
    assert storage.resume_job(job_id)
    assert storage.claim_job(job_id)["capture_segment"] == 1
    storage.add_posts(
        job_id,
        [post("2", source_position=9)],
        None,
        {"scanIterations": 9, "segmentScanIterations": 1},
    )

    rows = storage.get_job_posts(job_id)
    assert [(row["snapshot_position"], row["capture_segment"]) for row in rows] == [
        (0, 0),
        (1, 1),
    ]
    assert [row["scan_ordinal"] for row in rows] == [1, 1]
    assert [row["dom_position"] for row in rows] == [4, 9]


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_database_is_atomically_backed_up_and_migrated_honestly(tmp_path, version):
    storage = Storage(tmp_path / f"legacy-v{version}.db")
    _make_legacy(storage, version)
    request = collection().to_dict()
    request.pop("provider")
    plan = compile_request(collection())
    plan["provider"] = "x_api_search"
    with storage.connect() as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                id, request_json, compiled_request_json, status,
                collected_count, created_at, updated_at
            ) VALUES ('legacy', ?, ?, 'succeeded', 1, ?, ?)
            """,
            (
                json.dumps(request),
                json.dumps(plan),
                "2026-08-05T00:00:00+00:00",
                "2026-08-05T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO post_observations (
                job_id, post_id, position, text, url, observed_at
            ) VALUES ('legacy', '1', 0, 'preserved',
                      'https://x.com/i/web/status/1', '2026-08-05T00:00:00+00:00')
            """
        )

    storage.initialize()

    backup = tmp_path / f"legacy-v{version}.db.pre-v{version}-to-v3.bak"
    assert backup.exists() and stat.S_IMODE(backup.stat().st_mode) == 0o600
    with sqlite3.connect(backup) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == str(version)
    row = storage.get_job_posts("legacy")[0]
    assert row["text"] == "preserved" and row["snapshot_position"] == 0
    assert row["capture_segment"] is row["scan_ordinal"] is row["dom_position"] is None
    assert row["view_count"] is None
    assert row["source_position"] is None
    assert storage.get_job("legacy")["provider"] == "official_x_api"


def test_failed_migration_rolls_back_database_and_leaves_recovery_backup(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "legacy.db")
    _make_legacy(storage, 2)

    def fail(_connection):
        raise sqlite3.OperationalError("injected migration failure")

    monkeypatch.setattr(Storage, "_create_observations", staticmethod(fail))
    with pytest.raises(RuntimeError, match="preserved for recovery"):
        storage.initialize()

    with storage.connect() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "2"
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        observation_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(post_observations)")
        }
    assert "capture_segment" not in columns
    assert "position" in observation_columns and "snapshot_position" not in observation_columns
    assert (tmp_path / "legacy.db.pre-v2-to-v3.bak").exists()


@pytest.mark.parametrize("contents", [b"", b"stale partial backup"])
def test_migration_fails_closed_when_backup_target_already_exists(tmp_path, contents):
    storage = Storage(tmp_path / "legacy.db")
    _make_legacy(storage, 2)
    backup = tmp_path / "legacy.db.pre-v2-to-v3.bak"
    backup.write_bytes(contents)

    with pytest.raises(RuntimeError, match="backup already exists"):
        storage.initialize()

    assert backup.read_bytes() == contents
    with storage.connect() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "2"


@pytest.mark.parametrize(
    "drift", ["primary_key", "unique", "foreign_key", "index", "view"]
)
def test_schema_validation_rejects_key_and_index_drift(tmp_path, drift):
    storage = Storage(tmp_path / f"{drift}.db")
    storage.initialize()
    with storage.connect() as connection:
        if drift == "primary_key":
            connection.execute("ALTER TABLE schema_meta RENAME TO old_schema_meta")
            connection.execute("CREATE TABLE schema_meta (key TEXT, value TEXT NOT NULL)")
            connection.execute("INSERT INTO schema_meta SELECT * FROM old_schema_meta")
            connection.execute("DROP TABLE old_schema_meta")
        elif drift == "index":
            connection.execute("DROP INDEX idx_jobs_status_retry")
        elif drift == "view":
            connection.execute("CREATE VIEW unexpected_jobs AS SELECT id FROM jobs")
        else:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'post_observations'"
            ).fetchone()[0]
            connection.execute("DROP INDEX idx_observations_position")
            connection.execute("DROP TABLE post_observations")
            if drift == "unique":
                sql = sql.replace(",\n                UNIQUE (job_id, snapshot_position)", "")
            else:
                sql = sql.replace(" REFERENCES jobs(id) ON DELETE CASCADE", "")
            connection.execute(sql)
            connection.execute(
                "CREATE INDEX idx_observations_position "
                "ON post_observations(job_id, snapshot_position)"
            )
        assert not storage._schema_is_compatible(connection, version=3)


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_connect_rejects_symlink_and_nonregular_database_paths(tmp_path, kind):
    path = tmp_path / "unsafe.db"
    if kind == "symlink":
        target = tmp_path / "target.db"
        target.write_bytes(b"untouched")
        path.symlink_to(target)
    else:
        path.mkdir()

    with pytest.raises(RuntimeError, match="not a regular file"):
        Storage(path).connect()


def test_corrupt_json_is_generic_and_local_to_its_row(tmp_path):
    storage = Storage(tmp_path / "corrupt.db")
    storage.initialize()
    corrupt_id = _create_job(storage)
    healthy_id = _create_job(storage)
    with storage.connect() as connection:
        connection.execute(
            """
            UPDATE jobs SET request_json = ?, compiled_request_json = ?, warnings_json = ?,
                cursor = ?
            WHERE id = ?
            """,
            ("secret:not-json", "[", "{", "{", corrupt_id),
        )

    jobs = {job["id"]: job for job in storage.list_jobs()}
    assert jobs[healthy_id]["stored_metadata_valid"] is True
    corrupt = jobs[corrupt_id]
    assert corrupt["stored_metadata_valid"] is False
    assert all("secret" not in warning for warning in corrupt["warnings"])
    assert len(corrupt["warnings"]) == 4


def test_corrupt_media_json_does_not_hide_healthy_rows(tmp_path):
    storage = Storage(tmp_path / "media.db")
    storage.initialize()
    job_id = _create_job(storage)
    storage.claim_job(job_id)
    storage.add_posts(job_id, [post("1"), post("2")], None, {})
    with storage.connect() as connection:
        connection.execute(
            "UPDATE post_observations SET media_json = '[' WHERE post_id = '1'"
        )

    rows = storage.get_job_posts(job_id)
    assert rows[0]["media"] is None
    assert rows[0]["storage_warnings"] == ["Stored media metadata is unreadable."]
    assert rows[1]["media"][0]["id"] == "media-1"


def test_unknown_persisted_provider_is_flagged_without_killing_reads(tmp_path):
    storage = Storage(tmp_path / "unknown.db")
    storage.initialize()
    job_id = _create_job(storage)
    with storage.connect() as connection:
        body = collection().to_dict()
        body["provider"] = "future_provider"
        connection.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?", (json.dumps(body), job_id)
        )

    job = storage.get_job(job_id)
    assert job["provider"] == "future_provider"
    assert job["stored_metadata_valid"] is False
    assert "unknown" in job["warnings"][-1]


@pytest.mark.parametrize(
    "code", ["manual_action_required", "session_expired", "rate_limited", "browser_rate_limited"]
)
def test_resume_rejects_states_requiring_a_new_approved_job(tmp_path, code):
    storage = Storage(tmp_path / f"{code}.db")
    storage.initialize()
    job_id = _create_job(storage)
    storage.claim_job(job_id)
    storage.fail_job(job_id, JobStatus.PARTIAL, code, code, True)
    assert storage.resume_job(job_id) is False


def test_legacy_waiting_jobs_are_terminalized_and_never_requeued(tmp_path):
    storage = Storage(tmp_path / "waiting.db")
    storage.initialize()
    job_id = _create_job(storage)
    with storage.connect() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'waiting', error_code = 'rate_limited', "
            "error_retryable = 1, retry_at = '2000-01-01' WHERE id = ?",
            (job_id,),
        )

    assert storage.recover_jobs() == []
    job = storage.get_job(job_id)
    assert job["status"] == "failed" and job["error_retryable"] is False
    assert job["retry_at"] is None and storage.requeue_due_jobs() == []


def test_incompatible_database_is_rejected_without_modification(tmp_path):
    storage = Storage(tmp_path / "old.db")
    with storage.connect() as connection:
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (("schema_family", "official_x_api_mvp"), ("schema_version", "1")),
        )
        connection.execute("CREATE TABLE legacy_posts (id TEXT)")
    before = storage.path.read_bytes()

    with pytest.raises(RuntimeError, match="not a .* v3"):
        storage.initialize()

    assert storage.path.read_bytes() == before
