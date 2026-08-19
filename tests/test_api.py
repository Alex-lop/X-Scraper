import csv
import io
import json
from datetime import UTC, datetime, timedelta

from xworkbench.api import create_app
from xworkbench.config import Settings
from xworkbench.errors import CredentialError, InvalidRequestError
from xworkbench.models import CollectionSummary, Post, ProviderType
from xworkbench.providers import ProviderRegistry
from xworkbench.storage import Storage
from xworkbench.x_api import (
    ARCHIVE_ENDPOINT,
    UNIT_PRICES_USD,
    compile_request,
    validate_compiled_request,
)


class BrowserProvider:
    provider_id = ProviderType.PLAYWRIGHT_BROWSER
    provider_version = 1

    def __init__(self, *, partial=False):
        self.partial = partial

    def capabilities(self):
        return {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            "sourceKinds": ["home"],
            "minimumPosts": 1,
            "defaultPosts": 5,
            "maximumPosts": 25,
        }

    def connection_status(self):
        return {"status": "ready", "ready": True, "message": "Browser session ready."}

    def prepare(self, request, supplied_plan=None):
        plan = {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            "sourceKind": "home",
            "sourceUrl": "https://x.com/home",
            "targetPosts": request.max_posts,
        }
        if supplied_plan is not None and supplied_plan != plan:
            raise InvalidRequestError("Browser execution plan does not match this request.")
        return plan

    def collect(
        self, request, *, execution_plan, checkpoint, on_batch, should_cancel
    ):
        on_batch(
            [
                Post(
                    post_id="99",
                    text="=SUM(1,1) exact visible text\n",
                    author_username=None,
                    author_id=None,
                    url="https://x.com/i/web/status/99",
                    created_at=None,
                    language=None,
                    media=None,
                    source_position=0,
                )
            ],
            {
                "seenPostIds": ["99"],
                "scanIterations": 2,
                "scrollIterations": 1,
                "noProgressIterations": 0,
            },
            {
                "browserVersion": "test-chromium",
                "sourceKind": "home",
                "sourceUrl": "https://x.com/home",
                "scanIterations": 2,
                "scrollIterations": 1,
                "observedAt": "2026-08-05T12:00:00+00:00",
            },
        )
        return CollectionSummary(
            warnings=["Synthetic browser provider."],
            completion_reason="no_progress" if self.partial else "target_reached",
            partial=self.partial,
        )


class OfficialProvider:
    provider_id = ProviderType.OFFICIAL_X_API
    provider_version = 2

    def __init__(self, settings):
        self.settings = settings

    def capabilities(self):
        return {
            "provider": self.provider_id.value,
            "providerVersion": self.provider_version,
            "sourceKinds": ["profile", "search"],
            "minimumPosts": 10,
            "maximumPosts": 500,
        }

    def connection_status(self):
        status = self.settings.connection_status()
        return {**status, "ready": bool(status["valid"])}

    def prepare(self, request, supplied_plan=None):
        if not self.settings.bearer_token():
            raise CredentialError("No X API Bearer Token. Run: xworkbench configure")
        if supplied_plan is None:
            return compile_request(request)
        validate_compiled_request(request, supplied_plan)
        return supplied_plan

    def collect(
        self, request, *, execution_plan, checkpoint, on_batch, should_cancel
    ):
        on_batch(
            [
                Post(
                    post_id="42",
                    text="Official API text",
                    author_username="tester",
                    author_id="7",
                    url="https://x.com/tester/status/42",
                    created_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                    language="en",
                    like_count=5,
                    repost_count=2,
                    media=[],
                    has_media=False,
                )
            ],
            None,
            {
                "resourcesReturned": {"posts": 1, "users": 1, "media": 0},
                "rateLimitRemaining": 44,
            },
        )
        return CollectionSummary(completion_reason="recent_search_exhausted")


def make_client(
    tmp_path, *, token=False, partial=False, providers=None, queue_capacity=100
):
    token_path = tmp_path / "auth" / "token"
    token_path.parent.mkdir(parents=True, mode=0o700)
    if token:
        token_path.write_text("secret")
        token_path.chmod(0o600)
    settings = Settings(
        tmp_path / "test.db", token_path, queue_capacity=queue_capacity
    )
    storage = Storage(settings.database_path)
    registry = ProviderRegistry(
        providers or [BrowserProvider(partial=partial), OfficialProvider(settings)]
    )
    app = create_app(settings, storage=storage, registry=registry, start_worker=False)
    app.config.update(TESTING=True)
    return app.test_client(), app


def official_body(**changes):
    today = datetime.now(UTC).date()
    request = {
        "provider": "official_x_api",
        "sourceType": "search",
        "sourceValue": "python",
        "searchMode": "recent",
        "maxPosts": 10,
        "startDate": (today - timedelta(days=3)).isoformat(),
        "endDate": (today - timedelta(days=2)).isoformat(),
    }
    request.update(changes)
    return request


def run_collection(client, app, request):
    preview = client.post("/api/collections/preview", json=request).get_json()
    confirmation = (
        {"confirmBrowserCapture": True}
        if preview["provider"] == "playwright_browser"
        else {"confirmPaidRead": True}
    )
    created = client.post(
        "/api/jobs",
        json={
            **preview["request"],
            "executionPlan": preview["executionPlan"],
            **confirmation,
        },
    )
    assert created.status_code == 202
    job_id = created.get_json()["jobId"]
    app.extensions["xworkbench_jobs"].run_once(job_id)
    return preview, job_id


def saved_batch_preview(client):
    sources = []
    for body in (
        {
            "displayName": "Saved Home",
            "provider": "playwright_browser",
            "surface": "home",
            "value": "home",
        },
        {
            "displayName": "Saved profile",
            "provider": "official_x_api",
            "surface": "profile",
            "value": "OpenAI",
        },
    ):
        response = client.post("/api/sources", json=body)
        assert response.status_code == 201
        sources.append(response.get_json()["source"])
    response = client.post(
        "/api/batches/preview",
        json={
            "items": [
                {"sourceId": sources[0]["sourceId"], "maxPosts": 5, "priority": 1},
                {"sourceId": sources[1]["sourceId"], "maxPosts": 10, "priority": 2},
            ],
            "deadlineSeconds": 600,
            "freshnessChoice": "capture_fresh",
        },
    )
    assert response.status_code == 200
    return sources, response.get_json()


def test_browser_is_default_and_does_not_require_api_token(tmp_path):
    client, _ = make_client(tmp_path)

    connection = client.get("/api/connection").get_json()
    assert connection["defaultProvider"] == "playwright_browser"
    assert connection["providers"]["playwright_browser"]["connection"]["ready"] is True
    assert connection["providers"]["official_x_api"]["connection"]["ready"] is False

    preview = client.post(
        "/api/collections/preview",
        json={"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1},
    )
    assert preview.status_code == 200
    assert "costEstimate" not in preview.get_json()


def test_default_registry_honors_queue_config_with_isolated_workers(tmp_path):
    settings = Settings(
        tmp_path / "configured.db",
        tmp_path / "auth" / "token",
        max_workers=2,
        queue_capacity=7,
    )
    app = create_app(settings, start_worker=False)
    jobs = app.extensions["xworkbench_jobs"]
    assert jobs.max_workers == 2
    assert jobs.max_queue == 7
    assert jobs._worker_registry() is not jobs._worker_registry()

    custom_app = create_app(
        Settings(
            tmp_path / "custom" / "workbench.db",
            tmp_path / "custom" / "auth" / "token",
            max_workers=2,
            queue_capacity=9,
        ),
        registry=ProviderRegistry([BrowserProvider()]),
        start_worker=False,
    )
    custom_jobs = custom_app.extensions["xworkbench_jobs"]
    assert custom_jobs.max_workers == 1
    assert custom_jobs.max_queue == 9


def test_full_configured_queue_returns_explicit_bounded_error(tmp_path):
    settings = Settings(
        tmp_path / "queue" / "workbench.db",
        tmp_path / "queue" / "auth" / "token",
        queue_capacity=1,
    )
    app = create_app(
        settings,
        registry=ProviderRegistry([BrowserProvider()]),
        start_worker=False,
    )
    app.config.update(TESTING=True)
    client = app.test_client()

    for maximum, expected_status in ((1, 202), (2, 429)):
        request = {
            "provider": "playwright_browser",
            "sourceType": "home",
            "maxPosts": maximum,
        }
        preview = client.post("/api/collections/preview", json=request).get_json()
        response = client.post(
            "/api/jobs",
            json={
                **preview["request"],
                "executionPlan": preview["executionPlan"],
                "confirmBrowserCapture": True,
            },
        )
        assert response.status_code == expected_status
        if expected_status == 429:
            assert response.get_json()["error"]["code"] == "queue_full"


def test_create_requires_exact_preview_and_provider_confirmation(tmp_path):
    client, _ = make_client(tmp_path)
    request = {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 5}
    preview = client.post("/api/collections/preview", json=request).get_json()

    missing_plan = client.post(
        "/api/jobs", json={**request, "confirmBrowserCapture": True}
    )
    assert missing_plan.status_code == 400
    assert client.post(
        "/api/jobs", json={**request, "executionPlan": preview["executionPlan"]}
    ).status_code == 400
    assert client.post(
        "/api/jobs",
        json={
            **request,
            "executionPlan": preview["executionPlan"],
            "compiledRequest": {**preview["executionPlan"], "targetPosts": 4},
            "confirmBrowserCapture": True,
        },
    ).status_code == 400


def test_saved_source_is_server_identified_and_exactly_bound_to_job(tmp_path):
    client, app = make_client(tmp_path)
    secret = "SENTINEL-SOURCE-SECRET"

    rejected = client.post(
        "/api/sources",
        json={
            "displayName": "Home research",
            "provider": "playwright_browser",
            "surface": "home",
            "value": "home",
            "token": secret,
        },
    )
    assert rejected.status_code == 400
    assert secret not in rejected.get_data(as_text=True)
    assert client.post(
        "/api/sources",
        json={
            "displayName": "Home research",
            "provider": "playwright_browser",
            "surface": "home",
            "value": "home",
        },
        headers={"Origin": "https://attacker.invalid"},
    ).status_code == 403

    display_name = "Research <script>alert(1)</script>"
    created = client.post(
        "/api/sources",
        json={
            "displayName": display_name,
            "provider": "playwright_browser",
            "surface": "home",
            "value": "home",
        },
    )
    assert created.status_code == 201
    source = created.get_json()["source"]
    assert len(source["sourceId"]) == 32
    assert all(character in "0123456789abcdef" for character in source["sourceId"])
    assert source["displayName"] == display_name
    assert datetime.fromisoformat(source["createdAt"]).utcoffset() is not None

    listed = client.get("/api/sources?limit=1&offset=0").get_json()
    assert listed["sources"] == [source]
    assert listed["pagination"] == {
        "limit": 1,
        "offset": 0,
        "count": 1,
        "hasMore": False,
        "nextOffset": None,
    }
    assert listed["untrustedExternalContent"] is True
    assert client.get("/api/sources?limit=100").status_code == 400
    assert client.get("/api/sources?limit=1%20OR%201=1").status_code == 400

    duplicate = client.post(
        "/api/sources",
        json={
            "displayName": "Duplicate name",
            "provider": "playwright_browser",
            "surface": "home",
            "value": "home",
        },
    )
    assert duplicate.status_code == 409

    request = {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1}
    preview = client.post("/api/collections/preview", json=request).get_json()
    accepted = client.post(
        "/api/jobs",
        json={
            **preview["request"],
            "executionPlan": preview["executionPlan"],
            "confirmBrowserCapture": True,
            "sourceId": source["sourceId"],
        },
    )
    assert accepted.status_code == 202
    assert accepted.get_json()["sourceId"] == source["sourceId"]
    stored = app.extensions["xworkbench_jobs"].storage.get_job(
        accepted.get_json()["jobId"]
    )
    assert stored["source_id"] == source["sourceId"]

    assert client.post(
        "/api/jobs",
        json={
            **preview["request"],
            "executionPlan": preview["executionPlan"],
            "confirmBrowserCapture": True,
            "sourceId": "../not-a-source",
        },
    ).status_code == 400
    assert client.post(
        "/api/jobs",
        json={
            **preview["request"],
            "executionPlan": preview["executionPlan"],
            "confirmBrowserCapture": True,
            "sourceId": "valid-but-missing",
        },
    ).status_code == 400

    profile = client.post(
        "/api/sources",
        json={
            "displayName": "Other profile",
            "provider": "playwright_browser",
            "surface": "profile",
            "value": "OpenAI",
        },
    ).get_json()["source"]
    mismatch = client.post(
        "/api/jobs",
        json={
            **preview["request"],
            "executionPlan": preview["executionPlan"],
            "confirmBrowserCapture": True,
            "sourceId": profile["sourceId"],
        },
    )
    assert mismatch.status_code == 400
    assert "does not match" in mismatch.get_json()["error"]["message"]


def test_batch_preview_is_canonical_double_confirm_is_idempotent_and_cancel_is_scoped(
    tmp_path,
):
    client, app = make_client(tmp_path, token=True)
    sources, preview = saved_batch_preview(client)
    manifest = preview["manifest"]
    digest = preview["approvalDigest"]
    storage = app.extensions["xworkbench_jobs"].storage

    assert preview["requiresConfirmation"] is True
    assert len(digest) == 64
    assert manifest["routeAlias"] == "direct"
    assert manifest["freshnessChoice"] == "capture_fresh"
    assert manifest["maxConcurrency"] == 1
    assert manifest["perSourceConcurrency"] == 1
    assert manifest["perAuthStateConcurrency"] == 1
    assert manifest["queueCapacity"] == 100
    assert manifest["expectedQueueOrder"] == [
        sources[1]["sourceId"],
        sources[0]["sourceId"],
    ]
    assert storage.list_batch_jobs(manifest["batchId"]) == []
    for item in manifest["items"]:
        assert item["sourceId"] in {source["sourceId"] for source in sources}
        assert item["normalizedDestination"] in {"home", "OpenAI"}
        assert item["visibleDestination"]
        assert item["deadlineSeconds"] == 600
        assert item["freshnessChoice"] == "capture_fresh"
        assert item["routeAlias"] == "direct"
        assert 1 <= item["expectedQueueOrder"] <= 2

    assert client.post(
        "/api/batches/confirm",
        json={
            "confirm": True,
            "manifest": {**manifest, "queueCapacity": 101},
            "approvalDigest": digest,
        },
    ).status_code == 409
    assert storage.list_batch_jobs(manifest["batchId"]) == []

    assert client.post(
        "/api/batches/confirm",
        json={
            "confirm": True,
            "manifest": manifest,
            "approvalDigest": "é" * 64,
        },
    ).status_code == 400

    confirmation = {
        "confirm": True,
        "manifest": preview["manifest"],
        "approvalDigest": preview["approvalDigest"],
    }
    first = client.post("/api/batches/confirm", json=confirmation)
    second = client.post("/api/batches/confirm", json=confirmation)
    assert first.status_code == second.status_code == 202
    assert first.get_json()["jobIds"] == second.get_json()["jobIds"]
    assert len(storage.list_batch_jobs(manifest["batchId"])) == 2
    with storage.connect() as connection:
        approvals = [
            json.loads(row["approval_json"])
            for row in connection.execute(
                "SELECT approval_json FROM jobs WHERE batch_id = ?",
                (manifest["batchId"],),
            ).fetchall()
        ]
    assert all(
        approval["confirmation"] is True
        and approval["previewFingerprint"] != digest
        for approval in approvals
    )

    progress = client.get("/api/progress?after=0&limit=100")
    assert progress.status_code == 200
    assert {event["jobId"] for event in progress.get_json()["events"]} >= set(
        first.get_json()["jobIds"]
    )

    cancelled = client.post(
        f"/api/batches/{manifest['batchId']}/cancel", json={"confirm": True}
    )
    assert cancelled.status_code == 200
    assert cancelled.get_json()["cancelledCount"] == 2
    assert client.post(
        f"/api/batches/{manifest['batchId']}/cancel", json={"confirm": True}
    ).get_json()["cancelledCount"] == 0
    assert all(
        storage.get_job(job_id)["status"] == "cancelled"
        for job_id in first.get_json()["jobIds"]
    )


def test_batch_rejects_unsaved_destinations_and_queue_full_rolls_back(tmp_path):
    client, app = make_client(tmp_path, token=True, queue_capacity=2)
    sources, preview = saved_batch_preview(client)
    secret = "SENTINEL-BATCH-SECRET"
    invalid_bodies = (
        {
            "items": [
                {
                    "sourceId": sources[0]["sourceId"],
                    "maxPosts": 5,
                    "priority": 0,
                    "normalizedDestination": "https://attacker.invalid",
                },
                {"sourceId": sources[1]["sourceId"], "maxPosts": 10},
            ]
        },
        {
            "items": [
                {"sourceId": "https://attacker.invalid", "maxPosts": 5},
                {"sourceId": sources[1]["sourceId"], "maxPosts": 10},
            ]
        },
        {
            "items": [
                {"sourceId": sources[0]["sourceId"], "maxPosts": 5},
                {"sourceId": sources[0]["sourceId"], "maxPosts": 5},
            ]
        },
        {
            "items": [
                {"sourceId": sources[0]["sourceId"], "maxPosts": 5},
                {"sourceId": sources[1]["sourceId"], "maxPosts": 10},
            ],
            "freshnessChoice": "reuse_if_fresh",
        },
        {
            "items": [
                {"sourceId": sources[0]["sourceId"], "maxPosts": 5},
                {"sourceId": sources[1]["sourceId"], "maxPosts": 10},
            ],
            "token": secret,
        },
    )
    for body in invalid_bodies:
        response = client.post("/api/batches/preview", json=body)
        assert response.status_code == 400
        assert secret not in response.get_data(as_text=True)
    assert client.post(
        "/api/batches/preview",
        json={
            "items": [
                {"sourceId": sources[0]["sourceId"], "maxPosts": 5},
                {"sourceId": sources[1]["sourceId"], "maxPosts": 10},
            ]
        },
        headers={"Origin": "https://attacker.invalid"},
    ).status_code == 403

    request = {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1}
    single = client.post("/api/collections/preview", json=request).get_json()
    admitted = client.post(
        "/api/jobs",
        json={
            **single["request"],
            "executionPlan": single["executionPlan"],
            "confirmBrowserCapture": True,
        },
    )
    assert admitted.status_code == 202

    confirmation = client.post(
        "/api/batches/confirm",
        json={
            "confirm": True,
            "manifest": preview["manifest"],
            "approvalDigest": preview["approvalDigest"],
        },
    )
    assert confirmation.status_code == 429
    assert confirmation.get_json()["error"]["code"] == "queue_full"
    assert app.extensions["xworkbench_jobs"].storage.list_batch_jobs(
        preview["manifest"]["batchId"]
    ) == []


def test_browser_job_and_exports_omit_api_metadata_and_secrets(tmp_path):
    client, app = make_client(tmp_path)
    _, job_id = run_collection(
        client,
        app,
        {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1},
    )

    job = client.get(f"/api/jobs/{job_id}").get_json()
    assert job["status"] == "succeeded" and job["provider"] == "playwright_browser"
    assert job["providerDetails"]["scanIterations"] == 2
    assert job["providerDetails"]["scrollIterations"] == 1
    assert "cost" not in job and "resourcesReturned" not in job and "rateLimit" not in job
    assert not {"checkpoint", "providerState", "headers", "token", "auth"}.intersection(job)

    exported = client.get(f"/api/jobs/{job_id}/export?format=json").get_json()
    assert set(exported) == {"schemaVersion", "job", "posts"}
    assert exported["schemaVersion"] == 4
    assert exported["posts"][0]["media"] is None

    csv_response = client.get(f"/api/jobs/{job_id}/export?format=csv")
    csv_row = next(csv.DictReader(io.StringIO(csv_response.get_data(as_text=True))))
    assert csv_row["provider"] == "playwright_browser"
    assert csv_row["text"].startswith("'=SUM(1,1)")
    assert csv_row["media"] == ""
    assert csv_row["post_resources_returned"] == ""


def test_official_preview_collection_cost_and_full_archive_regression(tmp_path):
    client, app = make_client(tmp_path, token=True)
    preview, job_id = run_collection(client, app, official_body())

    assert preview["compiledIntent"]["query"] == "(python) -is:reply"
    assert preview["costEstimate"]["unitPricesUsd"] == UNIT_PRICES_USD
    job = client.get(f"/api/jobs/{job_id}").get_json()
    assert job["provider"] == "official_x_api"
    assert job["resourcesReturned"] == {"posts": 1, "users": 1, "media": 0}
    assert job["cost"]["returnedListPriceEstimateUsd"] == 0.015
    assert job["rateLimit"]["remaining"] == 44

    archive = client.post(
        "/api/collections/preview",
        json=official_body(
            searchMode="fullArchive", startDate="2020-01-01", endDate="2020-01-02"
        ),
    ).get_json()
    assert archive["compiledIntent"]["endpoint"] == ARCHIVE_ENDPOINT
    assert archive["compiledIntent"]["endTime"] == "2020-01-03T00:00:00Z"


def test_partial_browser_snapshot_is_labeled(tmp_path):
    client, app = make_client(tmp_path, partial=True)
    _, job_id = run_collection(
        client,
        app,
        {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 5},
    )
    job = client.get(f"/api/jobs/{job_id}").get_json()
    assert job["status"] == "partial"
    assert job["isPartial"] is True
    assert job["completionReason"] == "no_progress"


def test_security_headers_loopback_and_dashboard_do_not_autoload_remote_media(tmp_path):
    client, _ = make_client(tmp_path)
    response = client.get("/")
    assert "img-src 'self' data:;" in response.headers["Content-Security-Policy"]
    image_policy = response.headers["Content-Security-Policy"].split("img-src", 1)[1]
    assert "https:" not in image_policy.split(";", 1)[0]
    assert client.post("/api/collections/preview", data="{}").status_code == 415
    assert client.get("/api/health", headers={"Host": "example.com"}).status_code == 403

    script = client.get("/app.js").get_data(as_text=True)
    assert "image.src = source" not in script
    assert "Open media" in script
