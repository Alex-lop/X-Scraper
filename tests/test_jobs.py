from datetime import UTC, datetime, timedelta

import pytest

from xworkbench.errors import RateLimitWaiting, SchemaDriftError
from xworkbench.jobs import JobService
from xworkbench.models import CollectionRequest, CollectionSummary, Post
from xworkbench.storage import Storage
from xworkbench.x_api import compile_request


class Provider:
    def __init__(self, mode):
        self.mode = mode

    def collect(
        self,
        request,
        *,
        compiled_request,
        cursor,
        collected_count,
        returned_post_count,
        on_batch,
        should_cancel,
    ):
        on_batch(
            [Post("1", "  persisted page\n", "tester", "https://x.com/tester/status/1", None)],
            "next",
            {
                "resourcesReturned": {"posts": 1, "users": 0, "media": 0},
                "warnings": ["page warning"],
            },
        )
        if self.mode == "wait":
            raise RateLimitWaiting(
                "try later",
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                0,
                123,
            )
        if self.mode == "fail":
            raise SchemaDriftError("schema changed")
        return CollectionSummary(
            warnings=["summary warning"],
            completion_reason="search_exhausted",
            partial=self.mode == "partial",
        )


def run(tmp_path, mode):
    storage = Storage(tmp_path / f"{mode}.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}
    )
    job_id = storage.create_job(request, compile_request(request))
    JobService(storage, Provider(mode), start_worker=False).run_once(job_id)
    return storage, job_id


@pytest.mark.parametrize(("mode", "status"), [("ok", "succeeded"), ("partial", "partial")])
def test_completion_persists_status_warnings_resources_and_page(tmp_path, mode, status):
    storage, job_id = run(tmp_path, mode)
    job = storage.get_job(job_id)

    assert job["status"] == status
    assert job["completion_reason"] == "search_exhausted"
    assert job["warnings"] == ["page warning", "summary warning"]
    assert job["collected_count"] == job["post_resource_count"] == 1
    assert storage.get_job_posts(job_id)[0]["text"] == "  persisted page\n"


def test_rate_limit_wait_is_persisted_and_due_job_requeues_with_page_intact(tmp_path):
    storage, job_id = run(tmp_path, "wait")
    job = storage.get_job(job_id)

    assert job["status"] == "waiting"
    assert job["error_code"] == "rate_limited" and job["retry_at"]
    assert job["rate_limit_remaining"] == 0 and job["rate_limit_reset"] == 123
    assert job["collected_count"] == 1 and storage.count_job_posts(job_id) == 1
    assert storage.requeue_due_jobs() == [job_id]
    assert storage.get_job(job_id)["status"] == "queued"


def test_provider_failure_preserves_completed_page(tmp_path):
    storage, job_id = run(tmp_path, "fail")
    job = storage.get_job(job_id)

    assert job["status"] == "failed" and job["error_code"] == "schema_mismatch"
    assert job["collected_count"] == job["post_resource_count"] == 1
    assert job["warnings"] == ["page warning"]
    assert storage.get_job_posts(job_id)[0]["post_id"] == "1"
