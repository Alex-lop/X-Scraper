from datetime import UTC, datetime, timedelta

from xscraper.errors import RateLimitWaiting, SchemaDriftError
from xscraper.jobs import JobService
from xscraper.models import CollectionRequest, CollectionSummary, Post
from xscraper.storage import Storage
from xscraper.x_api import compile_request


class Provider:
    mode = "ok"

    def collect(
        self, request, *, compiled_request, cursor, collected_count, on_batch, should_cancel
    ):
        if self.mode == "wait":
            raise RateLimitWaiting(
                "later", (datetime.now(UTC) + timedelta(minutes=1)).isoformat(), 0, 123
            )
        on_batch(
            [Post("1", "hello", "tester", "https://x.com/tester/status/1", None)],
            "next",
            {"billableReads": 1},
        )
        if self.mode == "schema":
            raise SchemaDriftError("changed")
        return CollectionSummary(completion_reason="recent_search_exhausted")


def setup(tmp_path, mode):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    context = compile_request(request, "token")
    job_id = storage.create_job(request, context)
    provider = Provider()
    provider.mode = mode
    JobService(storage, provider, start_worker=False).run_once(job_id)
    return storage, job_id


def test_rate_limit_enters_waiting_without_losing_job(tmp_path):
    storage, job_id = setup(tmp_path, "wait")
    job = storage.get_job(job_id)
    assert job["status"] == "waiting" and job["retry_at"]


def test_schema_failure_preserves_completed_page(tmp_path):
    storage, job_id = setup(tmp_path, "schema")
    job = storage.get_job(job_id)
    assert job["status"] == "failed" and job["collected_count"] == 1
    assert storage.get_job_posts(job_id)[0]["text"] == "hello"
