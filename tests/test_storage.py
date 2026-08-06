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


def test_clean_v1_schema_is_small_and_idempotent(tmp_path):
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

    with pytest.raises(RuntimeError, match="not compatible|v1"):
        storage.initialize()

    assert storage.path.read_bytes() == before
