from xscraper.api import create_app
from xscraper.config import Settings
from xscraper.models import CollectionSummary, Tweet
from xscraper.storage import Storage


class FakeProvider:
    def session_status(self):
        return {"status": "valid", "valid": True, "message": "ready"}

    def collect(self, request, *, cursor, on_batch, should_cancel):
        on_batch(
            [
                Tweet(
                    tweet_id="99",
                    text="Great release",
                    author_username="tester",
                    url="https://x.com/tester/status/99",
                    created_at="2026-06-02T00:00:00+00:00",
                    like_count=9,
                )
            ],
            "done",
        )
        return CollectionSummary()


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


def test_api_rejects_invalid_request(tmp_path):
    client, _ = make_client(tmp_path)
    response = client.post(
        "/api/jobs", json={"sourceType": "profile", "sourceValue": "bad handle!"}
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"
