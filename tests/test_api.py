from xscraper.api import create_app
from xscraper.config import Settings
from xscraper.models import CollectionSummary, Post
from xscraper.storage import Storage


class Provider:
    def collect(
        self, request, *, compiled_request, cursor, collected_count, on_batch, should_cancel
    ):
        on_batch(
            [Post("99", "=SUM(1,1)", "tester", "https://x.com/tester/status/99", None)],
            None,
            {"billableReads": 1, "rateLimitRemaining": 44},
        )
        return CollectionSummary(completion_reason="recent_search_exhausted")


def make_client(tmp_path, *, demo_mode=None):
    token = tmp_path / "auth" / "token"
    token.parent.mkdir(parents=True)
    token.write_text("secret")
    settings = Settings(tmp_path / "test.db", token)
    storage = Storage(settings.database_path)
    app = create_app(
        settings,
        storage=storage,
        provider=Provider(),
        start_worker=False,
        demo_mode=demo_mode,
    )
    app.config.update(TESTING=True)
    return app.test_client(), app


def body():
    return {"sourceType": "profile", "sourceValue": "tester", "maxPosts": 10}


def test_preview_confirmation_lifecycle_posts_and_exports(tmp_path):
    client, app = make_client(tmp_path)
    preview = client.post("/api/collections/preview", json=body()).get_json()
    assert preview["compiledIntent"]["query"] == "from:tester -is:reply"
    assert preview["maximumPostReads"] == 10
    assert preview["estimatedPostReadUsd"] == 0.05
    assert preview["estimateScope"] == "posts_only"
    rejected = client.post(
        "/api/jobs", json={**body(), "compiledRequest": preview["compiledRequest"]}
    )
    assert rejected.status_code == 400
    created = client.post(
        "/api/jobs",
        json={**body(), "compiledRequest": preview["compiledRequest"], "confirmPaidRead": True},
    )
    assert created.status_code == 202 and not created.get_json()["cacheHit"]
    job_id = created.get_json()["jobId"]
    app.extensions["xscraper_jobs"].run_once(job_id)
    job = client.get(f"/api/jobs/{job_id}").get_json()
    assert job["status"] == "succeeded" and job["readCount"] == 1
    assert not {"provider", "pagesScanned", "cacheSourceJobId"}.intersection(job)
    result = client.get(f"/api/jobs/{job_id}/posts?limit=50").get_json()
    assert result["posts"][0]["like_count"] == 0
    assert client.get(f"/api/jobs/{job_id}/tweets").status_code == 404
    exported = client.get(f"/api/jobs/{job_id}/export?format=json").get_json()
    assert "posts" in exported and "tweets" not in exported
    csv = client.get(f"/api/jobs/{job_id}/export?format=csv")
    assert b"'=SUM(1,1)" in csv.data


def test_exact_complete_request_returns_cache_hit_unless_forced(tmp_path):
    client, app = make_client(tmp_path)
    preview = client.post("/api/collections/preview", json=body()).get_json()
    request = {**body(), "compiledRequest": preview["compiledRequest"], "confirmPaidRead": True}
    first = client.post("/api/jobs", json=request).get_json()
    app.extensions["xscraper_jobs"].run_once(first["jobId"])
    second = client.post("/api/jobs", json=request)
    assert second.status_code == 200 and second.get_json() == {
        "jobId": first["jobId"],
        "status": "succeeded",
        "cacheHit": True,
    }
    forced = client.post("/api/jobs", json={**request, "forceRefresh": True})
    assert forced.status_code == 202 and forced.get_json()["jobId"] != first["jobId"]


def test_connection_and_packaged_assets_are_local_only(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.get("/api/connection").get_json()["valid"]
    assert client.get("/").status_code == client.get("/app.js").status_code == 200
    assert client.get("/README.md").status_code == 404
    assert client.get("/api/health", headers={"Host": "example.com"}).status_code == 403


def test_demo_modes_disable_offline_reads_and_cap_live_reads(tmp_path):
    offline, _ = make_client(tmp_path / "offline", demo_mode="offline")
    connection = offline.get("/api/connection").get_json()
    assert connection["demoMode"] == "offline" and not connection["valid"]
    blocked = offline.post("/api/collections/preview", json=body())
    assert blocked.status_code == 409
    assert blocked.get_json()["error"]["code"] == "offline_demo_read_disabled"

    live, _ = make_client(tmp_path / "live", demo_mode="live")
    too_large = live.post("/api/collections/preview", json={**body(), "maxPosts": 25}).get_json()
    assert too_large["error"]["code"] == "demo_post_limit"
    preview = live.post("/api/collections/preview", json=body()).get_json()
    forced = live.post(
        "/api/jobs",
        json={
            **body(),
            "compiledRequest": preview["compiledRequest"],
            "confirmPaidRead": True,
            "forceRefresh": True,
        },
    )
    assert forced.status_code == 400
    assert forced.get_json()["error"]["code"] == "demo_force_refresh_disabled"
