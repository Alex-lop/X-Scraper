import queue
from datetime import UTC, datetime, timedelta

import pytest

from xworkbench.errors import InvalidRequestError, RateLimitWaiting, SchemaDriftError
from xworkbench.jobs import JobService
from xworkbench.models import CollectionRequest, CollectionSummary, Post, ProviderType
from xworkbench.providers import ProviderRegistry
from xworkbench.storage import Storage
from xworkbench.x_api import compile_request


class Provider:
    provider_id = ProviderType.OFFICIAL_X_API
    provider_version = 1

    def __init__(self, mode):
        self.mode = mode

    def collect(
        self,
        request,
        *,
        execution_plan,
        checkpoint,
        on_batch,
        should_cancel,
    ):
        assert execution_plan["provider"] == "official_x_api"
        assert checkpoint["storedCount"] == 0
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
            completion_reason=(
                "target_reached" if self.mode == "false_target" else "search_exhausted"
            ),
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

    assert job["status"] == "partial" and job["error_code"] == "schema_mismatch"
    assert job["collected_count"] == job["post_resource_count"] == 1
    assert job["warnings"] == ["page warning"]
    assert storage.get_job_posts(job_id)[0]["post_id"] == "1"


def test_false_target_reached_is_corrected_after_deduplication(tmp_path):
    storage, job_id = run(tmp_path, "false_target")
    job = storage.get_job(job_id)

    assert job["status"] == "partial"
    assert job["completion_reason"] == "target_not_reached"
    assert job["collected_count"] == 1
    assert "unique posts" in job["warnings"][-1]


class BrowserProvider:
    provider_id = ProviderType.PLAYWRIGHT_BROWSER
    provider_version = 1

    def capabilities(self):
        return {"sources": ["home"]}

    def connection_status(self):
        return {"status": "ready"}

    def prepare(self, request, supplied_plan=None):
        return supplied_plan or {"provider": self.provider_id.value, "providerVersion": 1}

    def collect(
        self, request, *, execution_plan, checkpoint, on_batch, should_cancel
    ):
        assert "maximumPostResources" not in execution_plan
        assert checkpoint == {"providerState": None, "storedCount": 0, "metadata": {}}
        on_batch(
            [Post("2", "visible", "tester", "https://x.com/tester/status/2", None)],
            {"scanIterations": 1},
            {"observationTime": "2026-08-05T00:00:00Z"},
        )
        return CollectionSummary(completion_reason="target_reached")


def test_registry_dispatches_browser_without_api_plan_fields(tmp_path):
    storage = Storage(tmp_path / "browser.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1}
    )
    plan = {"provider": "playwright_browser", "providerVersion": 1}
    job_id = storage.create_job(request, plan)
    registry = ProviderRegistry([Provider("unused"), BrowserProvider()])

    JobService(storage, registry, start_worker=False).run_once(job_id)

    job = storage.get_job(job_id)
    assert job["status"] == "succeeded" and job["provider"] == "playwright_browser"
    assert job["checkpoint"]["providerState"] == {"scanIterations": 1}
    assert job["checkpoint"]["metadata"]["observationTime"].endswith("Z")
    assert registry.capabilities("playwright_browser") == {"sources": ["home"]}
    with pytest.raises(InvalidRequestError, match="Unknown collection provider"):
        registry.get("not_real")
    with pytest.raises(ValueError, match="Duplicate"):
        ProviderRegistry([BrowserProvider(), BrowserProvider()])


def test_worker_does_not_poll_storage_after_shutdown_begins(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "shutdown.db")
    storage.initialize()
    service = JobService(storage, BrowserProvider(), start_worker=False)

    class StoppingQueue:
        def get(self, timeout):
            service._stop_event.set()
            raise queue.Empty

    service._queue = StoppingQueue()
    monkeypatch.setattr(
        storage,
        "requeue_due_jobs",
        lambda: (_ for _ in ()).throw(AssertionError("storage polled after shutdown")),
    )

    service._worker()
