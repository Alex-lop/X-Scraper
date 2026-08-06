import json
import stat

import pytest

from xworkbench.models import CollectionRequest, Post
from xworkbench.storage import SCHEMA_FAMILY, SCHEMA_VERSION, Storage
from xworkbench.x_api import compile_request


def collection():
    return CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )


def post():
    return Post(
        post_id="1",
        text="  exact text\n",
        author_username=None,
        author_id="7",
        url="https://x.com/i/web/status/1",
        created_at="2026-08-05T12:00:00Z",
        language="en",
        like_count=None,
        reply_count=0,
        is_reply=True,
        media=[{"id": "media-1", "type": "photo"}],
    )


def test_clean_v2_schema_is_small_and_idempotent(tmp_path):
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


def test_job_page_persists_exact_posts_warnings_resources_and_rate_limit(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    request = collection()
    job_id = storage.create_job(request, compile_request(request))
    assert storage.claim_job(job_id)["status"] == "running"

    added = storage.add_posts(
        job_id,
        [post(), post()],
        "next-page",
        {
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
    assert job["rate_limit_remaining"] == 44 and job["rate_limit_reset"] == 123

    stored = storage.get_job_posts(job_id)[0]
    assert stored["post_id"] == "1" and stored["author_id"] == "7"
    assert stored["author_username"] is None and stored["text"] == "  exact text\n"
    assert stored["like_count"] is None and stored["reply_count"] == 0
    assert stored["is_reply"] is True and stored["media"][0]["id"] == "media-1"
    assert stored["source_position"] == 0


def test_exact_v1_database_is_backed_up_and_migrated_without_rewriting_legacy_json(tmp_path):
    storage = Storage(tmp_path / "legacy.db")
    storage.initialize()
    request = collection()
    request_json = request.to_dict()
    request_json.pop("provider")
    plan = compile_request(request)
    plan["provider"] = "x_api_search"
    with storage.connect() as connection:
        connection.execute("DROP INDEX idx_observations_position")
        connection.execute("DROP TABLE post_observations")
        connection.executescript(
            """
            CREATE TABLE post_observations (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                post_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                text TEXT NOT NULL,
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
                is_reply INTEGER NOT NULL DEFAULT 0,
                is_repost INTEGER NOT NULL DEFAULT 0,
                is_quote INTEGER NOT NULL DEFAULT 0,
                has_media INTEGER NOT NULL DEFAULT 0,
                media_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (job_id, post_id),
                UNIQUE (job_id, position)
            );
            CREATE INDEX idx_observations_position
                ON post_observations(job_id, position);
            """
        )
        connection.execute(
            "UPDATE schema_meta SET value = '1' WHERE key = 'schema_version'"
        )
        connection.execute(
            """
            INSERT INTO jobs (
                id, request_json, compiled_request_json, status,
                collected_count, created_at, updated_at
            ) VALUES ('legacy', ?, ?, 'succeeded', 1, ?, ?)
            """,
            (
                json.dumps(request_json),
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

    backup = tmp_path / "legacy.db.pre-v1-to-v2.bak"
    assert backup.exists() and stat.S_IMODE(backup.stat().st_mode) == 0o600
    with storage.connect() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        columns = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(post_observations)")
        }
        raw_request = connection.execute(
            "SELECT request_json FROM jobs WHERE id = 'legacy'"
        ).fetchone()[0]
    assert version == "2"
    assert all(
        columns[name]["notnull"] == 0
        for name in ("text", "is_reply", "is_repost", "is_quote", "has_media", "media_json")
    )
    assert "provider" not in json.loads(raw_request)
    job = storage.get_job("legacy")
    assert job["provider"] == "official_x_api"
    assert job["request"]["provider"] == "official_x_api"
    assert job["execution_plan"]["provider"] == "x_api_search"
    assert storage.get_job_posts("legacy")[0]["text"] == "preserved"


def test_nullable_post_and_browser_checkpoint_round_trip_without_json_null(tmp_path):
    storage = Storage(tmp_path / "browser.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1}
    )
    job_id = storage.create_job(
        request,
        {
            "provider": "playwright_browser",
            "providerVersion": 1,
            "sourceKind": "home",
            "sourceUrl": "https://x.com/home",
        },
    )
    storage.claim_job(job_id)
    added = storage.add_posts(
        job_id,
        [Post("42", None, None, "https://x.com/i/web/status/42", None)],
        {"scanIterations": 1},
        {"warnings": ["missing optional DOM fields"], "observationTime": "now"},
    )

    assert added == 1
    job = storage.get_job(job_id)
    assert job["checkpoint"] == {
        "providerState": {"scanIterations": 1},
        "storedCount": 1,
        "metadata": {"observationTime": "now"},
    }
    assert job["warnings"] == ["missing optional DOM fields"]
    post_row = storage.get_job_posts(job_id)[0]
    assert post_row["text"] is None and post_row["media"] is None
    assert post_row["is_reply"] is post_row["has_media"] is None
    assert post_row["source_position"] == 0
    with storage.connect() as connection:
        raw = connection.execute(
            "SELECT text, media_json, is_reply, has_media FROM post_observations"
        ).fetchone()
    assert tuple(raw) == (None, None, None, None)


def test_unknown_persisted_provider_is_not_silently_treated_as_official(tmp_path):
    storage = Storage(tmp_path / "unknown.db")
    storage.initialize()
    request = collection()
    job_id = storage.create_job(request, compile_request(request))
    with storage.connect() as connection:
        body = request.to_dict()
        body["provider"] = "future_provider"
        connection.execute(
            "UPDATE jobs SET request_json = ? WHERE id = ?", (json.dumps(body), job_id)
        )

    with pytest.raises(RuntimeError, match="unknown collection provider"):
        storage.get_job(job_id)


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

    with pytest.raises(RuntimeError, match="not a .* v2"):
        storage.initialize()

    assert storage.path.read_bytes() == before
