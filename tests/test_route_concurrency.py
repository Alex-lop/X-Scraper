from __future__ import annotations

import hashlib
import threading

import pytest

import xworkbench.api as api_module
from xworkbench.config import Settings
from xworkbench.jobs import JobService
from xworkbench.models import (
    CollectionRequest,
    CollectionSummary,
    Post,
    ProviderType,
    SourceDefinition,
    utc_now,
)
from xworkbench.providers import ProviderRegistry
from xworkbench.storage import Storage


class _Tracker:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.first_entered = threading.Event()
        self.both_entered = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.first_entered.set()
            if self.active == 2:
                self.both_entered.set()

    def leave(self) -> None:
        with self.lock:
            self.active -= 1


class _BlockingProvider:
    provider_version = 1

    def __init__(self, provider_id: ProviderType, tracker: _Tracker) -> None:
        self.provider_id = provider_id
        self.tracker = tracker

    def capabilities(self):
        return {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            "sourceKinds": (
                ["home", "profile", "search"]
                if self.provider_id is ProviderType.PLAYWRIGHT_BROWSER
                else ["profile", "search"]
            ),
            "minimumPosts": 1 if self.provider_id is ProviderType.PLAYWRIGHT_BROWSER else 10,
            "maximumPosts": 25 if self.provider_id is ProviderType.PLAYWRIGHT_BROWSER else 500,
        }

    def connection_status(self):
        return {"status": "ready", "ready": True, "message": "Local fixture ready."}

    def prepare(self, request, supplied_plan=None):
        plan = {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            "sourceKind": request.source_type.value,
            "sourceUrl": f"fixture://{self.provider_id.value}/{request.source_value}",
            "targetPosts": request.max_posts,
        }
        if supplied_plan is not None and supplied_plan != plan:
            raise ValueError("Execution plan changed.")
        return plan

    def collect(self, request, *, execution_plan, checkpoint, on_batch, should_cancel):
        self.tracker.enter()
        try:
            if not self.tracker.release.wait(5):
                raise RuntimeError("Route-concurrency fixture was not released.")
            assert not should_cancel()
            post_id = hashlib.sha256(
                f"{self.provider_id.value}:{request.source_value}".encode()
            ).hexdigest()[:16]
            on_batch(
                [Post(post_id, "local fixture", "fixture", f"fixture://post/{post_id}", None)],
                None,
                {"sourceKind": request.source_type.value},
            )
            return CollectionSummary(completion_reason="timeline_exhausted")
        finally:
            self.tracker.leave()


def _source(provider: str, value: str) -> dict[str, object]:
    return {
        "displayName": f"{provider} {value}",
        "provider": provider,
        "surface": "profile",
        "value": value,
    }


def test_requeue_routing_reuses_the_durable_source_and_auth_keys(tmp_path):
    tracker = _Tracker()
    provider = _BlockingProvider(ProviderType.OFFICIAL_X_API, tracker)
    storage = Storage(tmp_path / "resume-routing.db")
    storage.initialize()
    service = JobService(storage, provider, start_worker=False)
    request = CollectionRequest.from_dict(
        {
            "provider": "official_x_api",
            "sourceType": "profile",
            "sourceValue": "routealpha",
            "maxPosts": 10,
        }
    )
    storage.save_source(
        SourceDefinition.from_dict(
            {
                "id": "saved-source",
                "displayName": "Saved source",
                "provider": "official_x_api",
                "surface": "profile",
                "value": "routealpha",
                "createdAt": utc_now(),
            }
        )
    )
    job_id = service.submit(request, provider.prepare(request), source_id="saved-source")

    assert service._job_routing(job_id) == (
        "saved-source",
        "provider:official_x_api",
    )
    service.shutdown()


@pytest.mark.parametrize(
    ("providers", "expected_peak"),
    [
        (("playwright_browser", "playwright_browser"), 1),
        (("official_x_api", "official_x_api"), 1),
        (("playwright_browser", "official_x_api"), 2),
    ],
)
def test_production_routes_enforce_provider_auth_concurrency(
    tmp_path, monkeypatch, providers, expected_peak
):
    tracker = _Tracker()

    def registry_factory(_settings):
        return ProviderRegistry(
            [
                _BlockingProvider(ProviderType.PLAYWRIGHT_BROWSER, tracker),
                _BlockingProvider(ProviderType.OFFICIAL_X_API, tracker),
            ]
        )

    monkeypatch.setattr(api_module, "_default_registry", registry_factory)
    settings = Settings(
        tmp_path / "workbench.db",
        tmp_path / "auth" / "token",
        max_workers=2,
        queue_capacity=2,
    )
    app = api_module.create_app(settings)
    app.config.update(TESTING=True)
    client = app.test_client()
    service = app.extensions["xworkbench_jobs"]
    storage = service.storage

    try:
        for provider, value in zip(providers, ("routealpha", "routebeta"), strict=True):
            created = client.post("/api/sources", json=_source(provider, value))
            assert created.status_code == 201
        listed = client.get("/api/sources?limit=25").get_json()["sources"]
        sources = {source["query"]: source for source in listed}

        items = []
        for provider, value in zip(providers, ("routealpha", "routebeta"), strict=True):
            items.append(
                {
                    "sourceId": sources[value]["sourceId"],
                    "maxPosts": 1 if provider == "playwright_browser" else 10,
                    "priority": 0,
                }
            )
        preview_response = client.post(
            "/api/batches/preview",
            json={
                "items": items,
                "deadlineSeconds": 600,
                "freshnessChoice": "capture_fresh",
            },
        )
        assert preview_response.status_code == 200
        preview = preview_response.get_json()
        manifest = preview["manifest"]
        assert manifest["maxConcurrency"] == 2
        assert manifest["perSourceConcurrency"] == 1
        assert manifest["perAuthStateConcurrency"] == 1
        assert manifest["queueCapacity"] == 2
        assert all("authStateId" not in item for item in manifest["items"])

        confirmed = client.post(
            "/api/batches/confirm",
            json={
                "confirm": True,
                "manifest": manifest,
                "approvalDigest": preview["approvalDigest"],
            },
        )
        assert confirmed.status_code == 202
        job_ids = confirmed.get_json()["jobIds"]
        assert tracker.first_entered.wait(5)

        if expected_peak == 2:
            assert tracker.both_entered.wait(5)
            assert service.metrics()["queueDepth"] == 0
        else:
            with service._condition:
                assert service._condition.wait_for(
                    lambda: len(service._active_jobs) == 1 and len(service._pending_jobs) == 1,
                    timeout=5,
                )
                active = next(iter(service._active_jobs.values()))
                queued = next(iter(service._pending_jobs.values()))
                assert active.auth_id == queued.auth_id == f"provider:{providers[0]}"
            queued_job = storage.get_job(queued.job_id)
            assert queued_job["status"] == "queued"
            assert queued_job["lease_owner"] is None
            assert storage.queue_counts() == {
                "queued": 1,
                "running": 1,
                "waiting": 0,
                "leased": 1,
                "active": 2,
            }

        assert tracker.peak == expected_peak
        stored = [storage.get_job(job_id) for job_id in job_ids]
        assert [job["source_id"] for job in stored] == [item["sourceId"] for item in items]
        assert [job["auth_state_id"] for job in stored] == [
            f"provider:{provider}" for provider in providers
        ]

        tracker.release.set()
        with service._condition:
            assert service._condition.wait_for(lambda: service._finished == 2, timeout=5)
        assert all(storage.get_job(job_id)["status"] == "succeeded" for job_id in job_ids)
        assert all(storage.count_job_posts(job_id) == 1 for job_id in job_ids)
        assert storage.queue_counts() == {
            "queued": 0,
            "running": 0,
            "waiting": 0,
            "leased": 0,
            "active": 0,
        }
        metrics = service.metrics()
        assert metrics["queueDepth"] == metrics["activeWorkers"] == 0
        assert metrics["started"] == metrics["finished"] == 2
        assert metrics["maxPersistenceBacklog"] <= 2
    finally:
        tracker.release.set()
        service.shutdown()

    assert not service._lock_path.exists()
    assert not any(thread.is_alive() for thread in service._threads)
