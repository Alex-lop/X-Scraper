import json
import sqlite3
import stat
from datetime import UTC, datetime, timedelta

import pytest

from xworkbench.models import CollectionRequest, JobStatus, Post, SourceDefinition
from xworkbench.storage import SCHEMA_FAMILY, SCHEMA_TABLES, SCHEMA_VERSION, Storage
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
    with storage.connect() as connection:
        capture_segment = "capture_segment INTEGER," if version == 3 else ""
        connection.executescript(
            f"""
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta(key, value) VALUES
                ('schema_family', 'x_collection_workbench'),
                ('schema_version', '{version}');
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                request_json TEXT NOT NULL,
                compiled_request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                collected_count INTEGER NOT NULL DEFAULT 0,
                cursor TEXT,
                warnings_json TEXT NOT NULL DEFAULT '[]',
                error_code TEXT,
                error_message TEXT,
                error_retryable INTEGER NOT NULL DEFAULT 0,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                completion_reason TEXT,
                retry_at TEXT,
                rate_limit_remaining INTEGER,
                rate_limit_reset INTEGER,
                post_resource_count INTEGER NOT NULL DEFAULT 0,
                user_resource_count INTEGER NOT NULL DEFAULT 0,
                media_resource_count INTEGER NOT NULL DEFAULT 0,
                {capture_segment}
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX idx_jobs_status_retry ON jobs(status, retry_at);
            """
        )
        if version == 3:
            Storage._create_observations(connection)
            connection.execute(
                "CREATE INDEX idx_observations_position "
                "ON post_observations(job_id, snapshot_position)"
            )
            return
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


def _create_job(storage):
    request = collection()
    return storage.create_job(request, compile_request(request))


def test_clean_v4_schema_is_secure_and_idempotent(tmp_path):
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

    assert tables == SCHEMA_TABLES
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


@pytest.mark.parametrize("version", [1, 2, 3])
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
        position = "snapshot_position" if version == 3 else "position"
        connection.execute(
            f"""
            INSERT INTO post_observations (
                job_id, post_id, {position}, text, url, observed_at
            ) VALUES ('legacy', '1', 0, 'preserved',
                      'https://x.com/i/web/status/1', '2026-08-05T00:00:00+00:00')
            """
        )

    storage.initialize()

    backup = tmp_path / f"legacy-v{version}.db.pre-v{version}-to-v4.bak"
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
    assert (tmp_path / "legacy.db.pre-v2-to-v4.bak").exists()


@pytest.mark.parametrize("contents", [b"", b"stale partial backup"])
def test_migration_fails_closed_when_backup_target_already_exists(tmp_path, contents):
    storage = Storage(tmp_path / "legacy.db")
    _make_legacy(storage, 2)
    backup = tmp_path / "legacy.db.pre-v2-to-v4.bak"
    backup.write_bytes(contents)

    with pytest.raises(RuntimeError, match="backup already exists"):
        storage.initialize()

    assert backup.read_bytes() == contents
    with storage.connect() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "2"


@pytest.mark.parametrize(
    "drift",
    ["primary_key", "unique", "foreign_key", "index", "partial_index", "view"],
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
        elif drift == "partial_index":
            connection.execute("DROP INDEX idx_jobs_idempotency")
            connection.execute(
                "CREATE UNIQUE INDEX idx_jobs_idempotency ON jobs(idempotency_key)"
            )
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
        assert not storage._schema_is_compatible(connection, version=4)


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
    assert job["provider"] == "unknown"
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

    with pytest.raises(RuntimeError, match="not a .* v4"):
        storage.initialize()

    assert storage.path.read_bytes() == before


def source_definition(source_id="source-a", value="tester"):
    return SourceDefinition.from_dict(
        {
            "id": source_id,
            "displayName": source_id,
            "provider": "official_x_api",
            "surface": "profile",
            "value": value,
            "createdAt": "2026-08-19T05:00:00+00:00",
        }
    )


def batch_items(count, *, prefix="batch", deadline=None):
    deadline = deadline or datetime.now(UTC) + timedelta(minutes=10)
    items = []
    for index in range(count):
        handle = f"{prefix.replace('-', '_')}{index}"[:15]
        request = CollectionRequest.from_dict(
            {
                "sourceType": "profile",
                "sourceValue": handle,
                "maxPosts": 10,
            }
        )
        items.append(
            {
                "request": request,
                "plan": compile_request(request),
                "priority": index % 3,
                "source_id": None,
                "auth_state_id": f"auth-{index}",
                "idempotency_key": f"{prefix}-key-{index}",
                "limits": {
                    "maxPosts": 10,
                    "deadlineSeconds": 600,
                    "routeAlias": "direct",
                    "maxConcurrency": 2,
                },
                "deadline_at": deadline,
            }
        )
    return items


def batch_manifest(storage, items, batch_id):
    return {
        "approvedAt": datetime.now(UTC).isoformat(),
        "confirmation": True,
        "previewFingerprint": storage.batch_preview_fingerprint(items, batch_id),
        "batchId": batch_id,
    }


def completed_snapshot(storage, request=None, *, source_id=None, text="alpha evidence"):
    request = request or collection()
    plan = (
        compile_request(request)
        if request.provider.value == "official_x_api"
        else {"provider": "playwright_browser", "providerVersion": 1}
    )
    job_id = storage.create_job(request, plan, source_id=source_id)
    storage.claim_job(job_id)
    item = post(job_id)
    item.text = text
    storage.add_posts(
        job_id,
        [item],
        None,
        {"fieldCoverage": {"text": 1.0}, "truncated": False},
    )
    storage.finish_job(job_id, [], completion_reason="target_reached")
    return job_id


def test_saved_sources_are_immutable_bounded_and_match_jobs(tmp_path):
    storage = Storage(tmp_path / "sources.db")
    storage.initialize()
    source = source_definition()

    saved = storage.save_source(source)
    assert storage.save_source(source) == saved
    assert storage.list_sources(limit=1, offset=0) == [saved]
    job_id = storage.create_job(
        collection(), compile_request(collection()), source_id=source.source_id
    )
    assert storage.get_job(job_id)["source_id"] == source.source_id
    assert storage.get_source(source.source_id)["last_status"] == "queued"

    changed = SourceDefinition.from_dict({**source.to_dict(), "displayName": "changed"})
    with pytest.raises(ValueError, match="immutable"):
        storage.save_source(changed)
    mismatch = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "different", "maxPosts": 10}
    )
    with pytest.raises(ValueError, match="does not match"):
        storage.create_job(mismatch, compile_request(mismatch), source_id=source.source_id)


def test_snapshot_metadata_is_immutable_home_is_not_reusable_and_corruption_is_skipped(
    tmp_path,
):
    storage = Storage(tmp_path / "snapshots.db")
    storage.initialize()
    older = completed_snapshot(storage)
    newer = completed_snapshot(storage, text="newer evidence")
    original = storage.get_snapshot(older)
    assert original["usable"] is True
    assert original["coverage"] == {"text": 1.0}
    assert original["snapshot_partial"] is False
    assert storage.finish_job(older, ["late"], completion_reason="late") is None
    assert storage.get_snapshot(older)["snapshot_at"] == original["snapshot_at"]

    with storage.connect() as connection:
        connection.execute(
            "UPDATE jobs SET collected_count = 'Bearer SECRET' WHERE id = ?", (newer,)
        )
    assert storage.get_latest_usable_snapshot()["id"] == older
    assert storage.list_snapshots(1, usable=True)[0]["id"] == older
    corrupt = storage.get_snapshot(newer)
    assert corrupt["stored_metadata_valid"] is False
    assert "Bearer SECRET" not in json.dumps(corrupt)

    captured = datetime.fromisoformat(original["snapshot_at"])
    stale = storage.get_snapshot(
        older, now=captured + timedelta(seconds=original["stale_after_seconds"] + 1)
    )
    assert stale["freshness"] == "stale"
    home = CollectionRequest.from_dict(
        {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1}
    )
    home_snapshot = storage.get_snapshot(completed_snapshot(storage, home))
    assert home_snapshot["usable"] is True
    assert home_snapshot["reuse_eligible"] is False


def test_fts_search_is_literal_isolated_time_bounded_and_purged_with_retention(tmp_path):
    storage = Storage(tmp_path / "evidence.db")
    storage.initialize()
    source_a = source_definition("source-a", "tester")
    source_b = source_definition("source-b", "different")
    storage.save_source(source_a)
    storage.save_source(source_b)
    first = completed_snapshot(
        storage, source_id=source_a.source_id, text="alpha near quoted evidence"
    )
    second = completed_snapshot(
        storage, source_id=source_a.source_id, text="alpha newer evidence"
    )
    foreign_request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "different", "maxPosts": 10}
    )
    completed_snapshot(
        storage,
        foreign_request,
        source_id=source_b.source_id,
        text="alpha foreign evidence",
    )

    rows = storage.search_post_evidence("alpha", source_ids=[source_a.source_id])
    assert {row["snapshot_id"] for row in rows} == {first, second}
    assert all(row["source_id"] == source_a.source_id for row in rows)
    assert all(row["url"].startswith("https://x.com/") for row in rows)
    assert storage.search_post_evidence('"alpha" NEAR evidence', snapshot_ids=[first])
    assert storage.search_post_evidence("alpha*", snapshot_ids=[second])
    for invalid in ("*", "\0", "alpha\nforeign"):
        with pytest.raises(ValueError):
            storage.search_post_evidence(invalid)
    after = datetime.fromisoformat(storage.get_snapshot(second)["snapshot_at"]) + timedelta(
        seconds=1
    )
    assert storage.search_post_evidence("alpha", start_time=after) == []

    assert storage.purge_snapshots(keep_per_source=1) == 1
    assert storage.get_snapshot(first) is None
    retained = storage.search_post_evidence("alpha", source_ids=[source_a.source_id])
    assert [row["snapshot_id"] for row in retained] == [second]


def test_batch_preview_is_order_sensitive_and_dict_key_canonical(tmp_path):
    storage = Storage(tmp_path / "batch-preview.db")
    storage.initialize()
    items = batch_items(2)
    original = storage.batch_preview_fingerprint(items, "batch-preview")
    reordered_keys = [dict(reversed(list(item.items()))) for item in items]
    reordered_keys[0]["plan"] = dict(reversed(list(items[0]["plan"].items())))
    reordered_keys[0]["limits"] = dict(reversed(list(items[0]["limits"].items())))

    assert storage.batch_preview_fingerprint(reordered_keys, "batch-preview") == original
    assert storage.batch_preview_fingerprint(list(reversed(items)), "batch-preview") != original
    manifest = batch_manifest(storage, items, "batch-preview")
    for change in (
        {"confirmation": False},
        {"approvedAt": "not-a-time"},
        {"batchId": "wrong"},
        {"previewFingerprint": "v1:" + "0" * 64},
    ):
        with pytest.raises(ValueError):
            storage.admit_batch(
                items,
                queue_capacity=2,
                batch_id="batch-preview",
                approval_manifest={**manifest, **change},
            )
    assert storage.list_batch_jobs("batch-preview") == []


def test_batch_admission_is_atomic_for_twenty_and_retry_idempotent(tmp_path):
    storage = Storage(tmp_path / "batch.db")
    storage.initialize()
    items = batch_items(20)
    manifest = batch_manifest(storage, items, "batch-20")

    admitted = storage.admit_batch(
        items,
        queue_capacity=20,
        batch_id="batch-20",
        approval_manifest=manifest,
    )
    assert admitted["result"] == "admitted"
    assert len(admitted["jobs"]) == 20
    assert {job["result"] for job in admitted["jobs"]} == {"created"}
    assert [job["enqueue_sequence"] for job in admitted["jobs"]] == list(range(1, 21))
    assert all(job["status"] == "queued" and job["source_id"] for job in admitted["jobs"])

    retry_manifest = {
        **manifest,
        "approvedAt": (
            datetime.fromisoformat(manifest["approvedAt"]) + timedelta(seconds=1)
        ).isoformat(),
    }
    retried = storage.admit_batch(
        items,
        queue_capacity=0,
        batch_id="batch-20",
        approval_manifest=retry_manifest,
    )
    assert [job["job_id"] for job in retried["jobs"]] == [
        job["job_id"] for job in admitted["jobs"]
    ]
    assert {job["result"] for job in retried["jobs"]} == {"existing"}

    rejected_items = batch_items(20, prefix="rejected")
    rejected = storage.admit_batch(
        rejected_items,
        queue_capacity=39,
        batch_id="batch-rejected",
        approval_manifest=batch_manifest(storage, rejected_items, "batch-rejected"),
    )
    assert rejected == {"result": "queue_full", "jobs": []}
    assert storage.list_batch_jobs("batch-rejected") == []


def test_batch_validation_and_database_failure_roll_back_every_item(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "batch-rollback.db")
    storage.initialize()
    saved = source_definition()
    storage.save_source(saved)
    invalid = batch_items(2, prefix="invalid")
    invalid[0]["request"] = collection()
    invalid[0]["plan"] = compile_request(collection())
    invalid[0]["source_id"] = saved.source_id
    invalid[1]["source_id"] = saved.source_id
    manifest = batch_manifest(storage, invalid, "invalid-batch")
    with pytest.raises(ValueError, match="Saved source"):
        storage.admit_batch(
            invalid,
            queue_capacity=10,
            batch_id="invalid-batch",
            approval_manifest=manifest,
        )
    assert storage.list_batch_jobs("invalid-batch") == []

    duplicates = batch_items(2, prefix="duplicate")
    duplicates[1]["idempotency_key"] = duplicates[0]["idempotency_key"]
    with pytest.raises(ValueError, match="repeated with a different"):
        storage.admit_batch(
            duplicates,
            queue_capacity=10,
            batch_id="duplicate-batch",
            approval_manifest=batch_manifest(storage, duplicates, "duplicate-batch"),
        )
    assert storage.list_batch_jobs("duplicate-batch") == []

    identical = batch_items(1, prefix="identical")
    identical.append(dict(identical[0]))
    duplicate_result = storage.admit_batch(
        identical,
        queue_capacity=1,
        batch_id="identical-batch",
        approval_manifest=batch_manifest(storage, identical, "identical-batch"),
    )
    assert [job["result"] for job in duplicate_result["jobs"]] == [
        "created",
        "existing",
    ]
    assert len({job["job_id"] for job in duplicate_result["jobs"]}) == 1

    items = batch_items(2, prefix="db-failure")
    manifest = batch_manifest(storage, items, "db-failure")
    original_insert = Storage._insert_admission
    calls = 0

    def fail_second(connection, prepared, *, job_id, sequence, now):
        nonlocal calls
        calls += 1
        original_insert(
            connection,
            prepared,
            job_id=job_id,
            sequence=sequence,
            now=now,
        )
        if calls == 2:
            raise sqlite3.OperationalError("injected batch failure")

    monkeypatch.setattr(Storage, "_insert_admission", staticmethod(fail_second))
    with pytest.raises(sqlite3.OperationalError, match="injected batch failure"):
        storage.admit_batch(
            items,
            queue_capacity=10,
            batch_id="db-failure",
            approval_manifest=manifest,
        )
    assert storage.list_batch_jobs("db-failure") == []


def test_durable_admission_fair_leases_owner_scope_and_recovery(tmp_path):
    storage = Storage(tmp_path / "queue.db")
    storage.initialize()
    deadline = datetime.now(UTC) + timedelta(minutes=10)
    approval = {
        "approvedAt": datetime.now(UTC).isoformat(),
        "confirmation": "test_fixture",
    }
    limits = {
        "maxPosts": 10,
        "deadlineSeconds": 30,
        "routeAlias": "direct",
        "maxConcurrency": 2,
    }

    def admit(value, auth, *, key=None):
        request = CollectionRequest.from_dict(
            {"sourceType": "profile", "sourceValue": value, "maxPosts": 10}
        )
        return storage.admit_job(
            request,
            compile_request(request),
            queue_capacity=20,
            priority=5,
            source_id=None,
            auth_state_id=auth,
            batch_id="batch",
            idempotency_key=key,
            approval=approval,
            limits=limits,
            deadline_at=deadline,
        )

    a1 = admit("a", "auth-a", key="same")
    assert a1["result"] == "created"
    request_a = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "a", "maxPosts": 10}
    )
    same = storage.admit_job(
        request_a,
        compile_request(request_a),
        queue_capacity=20,
        priority=5,
        source_id=None,
        auth_state_id="auth-a",
        batch_id="batch",
        idempotency_key="same",
        approval=approval,
        limits=limits,
        deadline_at=deadline,
    )
    assert same == {"result": "existing", "job_id": a1["job_id"]}
    a2 = admit("a", "auth-b")["job_id"]
    b1 = admit("b", "auth-a")["job_id"]
    c1 = admit("c", "auth-c")["job_id"]
    assert [row["id"] for row in storage.list_queued_jobs()] == [
        a1["job_id"],
        b1,
        c1,
        a2,
    ]
    public = storage.get_job(a1["job_id"])
    assert public["approval_recorded"] and public["limits_recorded"]
    assert "approval_json" not in public and "limits_json" not in public

    expiry = datetime.now(UTC) + timedelta(minutes=1)
    leased = storage.lease_job(a1["job_id"], worker_id="worker-a", lease_expires_at=expiry)
    assert leased["attempt_number"] == 1 and leased["capture_segment"] == 0
    assert storage.lease_job(a2, worker_id="worker-b", lease_expires_at=expiry) is None
    assert storage.lease_job(b1, worker_id="worker-b", lease_expires_at=expiry) is None
    assert storage.lease_job(c1, worker_id="worker-c", lease_expires_at=expiry)
    assert storage.finish_job(
        a1["job_id"], [], completion_reason="done", worker_id="wrong"
    ) is None
    assert storage.heartbeat_job(
        a1["job_id"],
        worker_id="worker-a",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert storage.finish_job(
        a1["job_id"], [], completion_reason="done", worker_id="worker-a"
    ) == "succeeded"

    recovery_now = datetime.now(UTC) + timedelta(minutes=2)
    recovered = storage.recover_jobs(recovery_now)
    assert c1 in recovered and storage.get_job(c1)["status"] == "queued"
    assert storage.get_job(c1)["lease_owner"] is None
    assert len(storage.list_batch_jobs("batch")) == 4
    assert storage.cancel_batch("batch") == 3


def test_corrupt_queue_scalars_and_private_records_are_generic_and_local(tmp_path):
    storage = Storage(tmp_path / "scalar-corruption.db")
    storage.initialize()
    corrupt_id = _create_job(storage)
    healthy_id = _create_job(storage)
    with storage.connect() as connection:
        connection.execute(
            """
            UPDATE jobs SET collected_count = 'Bearer SECRET', cancel_requested = 'secret',
                created_at = 'secret', deadline_at = 'secret',
                approval_json = '{"token":"Bearer SECRET"}'
            WHERE id = ?
            """,
            (corrupt_id,),
        )

    jobs = {job["id"]: job for job in storage.list_attempts(10)}
    assert jobs[healthy_id]["stored_metadata_valid"] is True
    corrupt = jobs[corrupt_id]
    assert corrupt["stored_metadata_valid"] is False
    assert corrupt["collected_count"] == 0 and corrupt["cancel_requested"] is False
    assert corrupt["created_at"] is corrupt["deadline_at"] is None
    assert corrupt["approval_recorded"] is False
    assert "Bearer SECRET" not in json.dumps(corrupt)


def test_repeated_context_connections_close_under_a_low_fd_limit(tmp_path):
    resource = pytest.importorskip("resource")
    descriptors = type(tmp_path)("/dev/fd")
    if not descriptors.exists():
        pytest.skip("descriptor accounting is unavailable")
    storage = Storage(tmp_path / "fds.db")
    storage.initialize()
    items = batch_items(1, prefix="fd-batch")
    manifest = batch_manifest(storage, items, "fd-batch")
    storage.admit_batch(
        items,
        queue_capacity=1,
        batch_id="fd-batch",
        approval_manifest=manifest,
    )
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    baseline = len(list(descriptors.iterdir()))
    lowered = min(soft, max(64, baseline + 32))
    resource.setrlimit(resource.RLIMIT_NOFILE, (lowered, hard))
    try:
        for _ in range(100):
            _create_job(storage)
            storage.list_attempts(1)
            retried = storage.admit_batch(
                items,
                queue_capacity=0,
                batch_id="fd-batch",
                approval_manifest=manifest,
            )
            assert retried["jobs"][0]["result"] == "existing"
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

    assert len(list(descriptors.iterdir())) <= baseline + 4
