from xscraper.api import create_app
from xscraper.config import Settings
from xscraper.models import CollectionSummary, Tweet
from xscraper.storage import Storage


class FakeProvider:
    def session_status(self):
        return {"status": "valid", "valid": True, "message": "ready"}

    def collect(self, request, *, cursor, cursor_context, on_batch, should_cancel):
        on_batch(
            [
                Tweet(
                    tweet_id="99",
                    text="=SUM(1,1)",
                    author_username="tester",
                    url="https://x.com/tester/status/99",
                    created_at="2026-06-02T00:00:00+00:00",
                    like_count=9,
                )
            ],
            "done",
            {
                "provider": "fake",
                "version": 1,
                "operation": "fake",
                "requestFingerprint": "fake",
                "sort": "live",
            },
            1,
        )
        return CollectionSummary(completion_reason="target_reached")


def make_client(tmp_path):
    settings = Settings(
        runtime_dir=tmp_path,
        database_path=tmp_path / "test.db",
        storage_state_path=tmp_path / "state.json",
        artifacts_dir=tmp_path / "artifacts",
    )
    storage = Storage(settings.database_path)
    app = create_app(settings, storage=storage, provider=FakeProvider(), start_worker=False)
    app.config.update(TESTING=True)
    return app.test_client(), app


def test_job_lifecycle_and_exports(tmp_path):
    client, app = make_client(tmp_path)
    response = client.post(
        "/api/jobs",
        json={"sourceType": "profile", "sourceValue": "tester", "maxTweets": 1},
    )
    assert response.status_code == 202
    job_id = response.get_json()["jobId"]
    app.extensions["xscraper_jobs"].run_once(job_id)
    job = client.get(f"/api/jobs/{job_id}").get_json()
    assert job["status"] == "succeeded"
    tweets = client.get(f"/api/jobs/{job_id}/tweets").get_json()["tweets"]
    assert tweets[0]["like_count"] == 9
    assert client.get(f"/api/jobs/{job_id}/export?format=json").status_code == 200
    csv_response = client.get(f"/api/jobs/{job_id}/export?format=csv")
    assert csv_response.status_code == 200
    assert b"tweet_id,text" in csv_response.data
    assert b"'=SUM(1,1)" in csv_response.data


def test_api_rejects_invalid_request(tmp_path):
    client, _ = make_client(tmp_path)
    response = client.post(
        "/api/jobs", json={"sourceType": "profile", "sourceValue": "bad handle!"}
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"


def test_api_rejects_bad_types_and_large_bodies(tmp_path):
    client, _ = make_client(tmp_path)
    response = client.post("/api/jobs", json=["not-an-object"])
    assert response.status_code == 400
    assert response.is_json

    response = client.post(
        "/api/jobs",
        json={"sourceType": "search", "sourceValue": "python", "mediaOnly": "false"},
    )
    assert response.status_code == 400

    response = client.post(
        "/api/jobs",
        json={"sourceType": "search", "sourceValue": "python", "unknown": True},
    )
    assert response.status_code == 400

    response = client.post(
        "/api/jobs",
        json={"sourceType": "search", "sourceValue": "python", "ignored": "x" * 40_000},
    )
    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "request_too_large"


def test_only_explicit_assets_are_served(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.get("/").status_code == 200
    assert client.get("/app.js").status_code == 200
    assert client.get("/styles.css").status_code == 200
    for path in (
        "/README.md",
        "/requirements.lock",
        "/xscraper/api.py",
        "/var/auth/storage_state.json",
        "/var/twitter_scraper.db",
    ):
        assert client.get(path).status_code == 404

    response = client.get("/api/health", headers={"Host": "example.com"})
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "local_only"


def test_tweet_pagination_and_export_metadata(tmp_path):
    client, app = make_client(tmp_path)
    response = client.post(
        "/api/jobs",
        json={"sourceType": "profile", "sourceValue": "tester", "maxTweets": 1},
    )
    job_id = response.get_json()["jobId"]
    app.extensions["xscraper_jobs"].run_once(job_id)

    data = client.get(f"/api/jobs/{job_id}/tweets?limit=1").get_json()
    assert data["pagination"] == {
        "limit": 1,
        "offset": 0,
        "count": 1,
        "total": 1,
        "nextOffset": None,
    }
    exported = client.get(f"/api/jobs/{job_id}/export?format=json")
    assert exported.headers["X-Collection-Status"] == "succeeded"
    assert exported.headers["X-Completion-Reason"] == "target_reached"
    assert exported.get_json()["schemaVersion"] == 1
    assert exported.get_json()["job"]["completionReason"] == "target_reached"
    assert len(exported.get_json()["tweets"]) == 1
