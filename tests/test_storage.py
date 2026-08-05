from datetime import UTC, datetime, timedelta

import pytest

from xscraper.models import CollectionRequest, JobStatus, Post
from xscraper.storage import Storage
from xscraper.x_api import compile_request


def request():
    return CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )


def compiled(scope_token="token"):
    return compile_request(request(), scope_token)


def post(post_id="1"):
    return Post(post_id, "hello", "tester", f"https://x.com/tester/status/{post_id}", None)


def test_snapshots_deduplicate_checkpoint_and_store_context(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    with storage.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == {"schema_meta", "jobs", "post_observations"}
    job_id = storage.create_job(request(), compiled())
    storage.claim_job(job_id)
    assert (
        storage.add_posts(
            job_id,
            [post(), post()],
            "opaque",
            {"billableReads": 2, "rateLimitRemaining": 99, "rateLimitReset": 123},
        )
        == 1
    )
    job = storage.get_job(job_id)
    assert job["collected_count"] == 1 and job["cursor"] == "opaque"
    assert job["compiled_request"]["query"] == "from:tester -is:reply"
    assert job["billable_read_count"] == 2 and job["rate_limit_remaining"] == 99
    assert storage.get_job_posts(job_id)[0]["tweet_id"] == "1"


def test_waiting_jobs_requeue_after_persisted_reset(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    job_id = storage.create_job(request(), compiled())
    storage.claim_job(job_id)
    storage.wait_job(job_id, (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), 0, 123, "wait")
    assert storage.get_job(job_id)["status"] == JobStatus.WAITING
    assert storage.requeue_due_jobs() == [job_id]
    assert storage.get_job(job_id)["status"] == JobStatus.QUEUED


def test_cache_requires_exact_complete_same_account_and_recent_finish(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    context = compiled()
    job_id = storage.create_job(request(), context)
    storage.claim_job(job_id)
    storage.finish_job(job_id, [], completion_reason="recent_search_exhausted")
    cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    assert (
        storage.find_cached_job(
            context["requestFingerprint"], context["accountScope"], cutoff=cutoff
        )["id"]
        == job_id
    )
    assert storage.find_cached_job(context["requestFingerprint"], "other", cutoff=cutoff) is None


def test_retired_scraper_database_is_rejected_without_modification(tmp_path):
    storage = Storage(tmp_path / "test.db")
    with storage.connect() as connection:
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')")
    before = storage.path.read_bytes()
    with pytest.raises(RuntimeError, match="retired scraper schema"):
        storage.initialize()
    assert storage.path.read_bytes() == before
    with storage.connect() as connection:
        assert connection.execute("SELECT value FROM schema_meta").fetchone()[0] == "2"
