import csv
import io
from datetime import UTC, datetime, timedelta

from xworkbench.api import create_app
from xworkbench.config import Settings
from xworkbench.models import CollectionSummary, Post
from xworkbench.storage import Storage
from xworkbench.x_api import ARCHIVE_ENDPOINT


class Provider:
    def __init__(self, *, partial=False):
        self.partial = partial
        self.calls = []

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
        self.calls.append(compiled_request["endpoint"])
        on_batch(
            [
                Post(
                    post_id="99",
                    text="=SUM(1,1) exact text\n",
                    author_username="tester",
                    author_id="7",
                    url="https://x.com/tester/status/99",
                    created_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                    language="en",
                    like_count=5,
                    repost_count=2,
                    media=[{"id": "media-1", "type": "photo"}],
                    has_media=True,
                )
            ],
            None,
            {
                "resourcesReturned": {"posts": 1, "users": 1, "media": 2},
                "warnings": ["X API partial response: expansion unavailable"],
                "rateLimitRemaining": 44,
            },
        )
        return CollectionSummary(
            warnings=["X API partial response: expansion unavailable"],
            completion_reason="search_exhausted",
            partial=self.partial,
        )


def make_client(tmp_path, *, partial=False):
    token_path = tmp_path / "auth" / "token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("secret")
    settings = Settings(tmp_path / "test.db", token_path)
    storage = Storage(settings.database_path)
    provider = Provider(partial=partial)
    app = create_app(settings, storage=storage, provider=provider, start_worker=False)
    app.config.update(TESTING=True)
    return app.test_client(), app, provider


def body(**changes):
    today = datetime.now(UTC).date()
    request = {
        "sourceType": "search",
        "sourceValue": "python",
        "searchMode": "recent",
        "maxPosts": 10,
        "startDate": (today - timedelta(days=3)).isoformat(),
        "endDate": (today - timedelta(days=2)).isoformat(),
    }
    request.update(changes)
    return request


def run_collection(client, app, request=None):
    request = request or body()
    preview = client.post("/api/collections/preview", json=request).get_json()
    created = client.post(
        "/api/jobs",
        json={
            **request,
            "compiledRequest": preview["compiledRequest"],
            "confirmPaidRead": True,
        },
    )
    assert created.status_code == 202
    job_id = created.get_json()["jobId"]
    app.extensions["xworkbench_jobs"].run_once(job_id)
    return preview, job_id


def test_preview_requires_confirmation_and_reports_separate_resource_prices(tmp_path):
    client, _, _ = make_client(tmp_path)
    request = body()
    response = client.post("/api/collections/preview", json=request)
    preview = response.get_json()

    assert response.status_code == 200
    assert preview["compiledIntent"]["query"] == "(python) -is:reply"
    assert preview["request"]["searchMode"] == "recent"
    expires = datetime.fromisoformat(preview["compiledRequest"]["expiresAt"].replace("Z", "+00:00"))
    compiled = datetime.fromisoformat(
        preview["compiledRequest"]["compiledAt"].replace("Z", "+00:00")
    )
    assert expires - compiled == timedelta(minutes=5)
    estimate = preview["costEstimate"]
    assert estimate["maximumPostResources"] == 10
    assert estimate["maximumPostListPriceUsd"] == 0.05
    assert estimate["unitPricesUsd"] == {"post": 0.005, "user": 0.01, "media": 0.005}
    assert "not an invoice" in str(estimate).casefold()

    rejected = client.post(
        "/api/jobs", json={**request, "compiledRequest": preview["compiledRequest"]}
    )
    assert rejected.status_code == 400
    assert "confirmPaidRead" in rejected.get_json()["error"]["message"]


def test_completed_job_exposes_cost_provenance_and_raw_json_csv_exports(tmp_path):
    client, app, provider = make_client(tmp_path)
    preview, job_id = run_collection(client, app)

    job = client.get(f"/api/jobs/{job_id}").get_json()
    assert job["status"] == "succeeded" and job["collectedCount"] == 1
    assert job["resourcesReturned"] == {"posts": 1, "users": 1, "media": 2}
    assert job["warnings"] == ["X API partial response: expansion unavailable"]
    assert job["provenance"]["query"] == preview["compiledIntent"]["query"]
    assert job["provenance"]["provider"] == "x_api_search"
    assert job["cost"]["maximumPostListPriceUsd"] == 0.05
    assert job["cost"]["returnedListPriceEstimateUsd"] == 0.025
    assert "not an invoice" in str(job["cost"]).casefold()
    assert provider.calls == [preview["compiledIntent"]["endpoint"]]

    exported = client.get(f"/api/jobs/{job_id}/export?format=json").get_json()
    assert set(exported) == {"schemaVersion", "job", "posts"}
    assert exported["posts"][0]["text"] == "=SUM(1,1) exact text\n"
    assert exported["posts"][0]["post_id"] == "99"

    csv_response = client.get(f"/api/jobs/{job_id}/export?format=csv")
    csv_row = next(csv.DictReader(io.StringIO(csv_response.get_data(as_text=True))))
    assert csv_row["post_id"] == "99"
    assert csv_row["text"].startswith("'=SUM(1,1)")
    assert client.get(f"/api/jobs/{job_id}/export?format=markdown").status_code == 400


def test_full_archive_preview_is_supported(tmp_path):
    client, _, _ = make_client(tmp_path)
    response = client.post(
        "/api/collections/preview",
        json=body(
            searchMode="fullArchive",
            startDate="2020-01-01",
            endDate="2020-01-02",
        ),
    )

    assert response.status_code == 200
    preview = response.get_json()
    assert preview["compiledIntent"]["endpoint"] == ARCHIVE_ENDPOINT
    assert preview["compiledIntent"]["endTime"] == "2020-01-03T00:00:00Z"


def test_cancel_and_terminal_delete_are_separate_json_mutations(tmp_path):
    client, _, _ = make_client(tmp_path)
    request = body()
    preview = client.post("/api/collections/preview", json=request).get_json()
    job_id = client.post(
        "/api/jobs",
        json={
            **request,
            "compiledRequest": preview["compiledRequest"],
            "confirmPaidRead": True,
        },
    ).get_json()["jobId"]

    assert client.delete(f"/api/jobs/{job_id}").status_code == 415
    assert (
        client.delete(
            f"/api/jobs/{job_id}", json={}
        ).status_code
        == 409
    )
    assert client.post(f"/api/jobs/{job_id}/cancel", json={}).status_code == 202
    assert (
        client.delete(
            f"/api/jobs/{job_id}", json={}
        ).status_code
        == 204
    )
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_partial_snapshot_is_marked_and_can_be_deleted(tmp_path):
    client, app, _ = make_client(tmp_path, partial=True)
    _, job_id = run_collection(client, app)

    job = client.get(f"/api/jobs/{job_id}").get_json()
    assert job["status"] == "partial" and job["isPartial"] is True
    assert (
        client.delete(f"/api/jobs/{job_id}", json={}).status_code
        == 204
    )


def test_api_rejects_non_json_mutations_and_non_loopback_hosts(tmp_path):
    client, _, _ = make_client(tmp_path)

    assert client.post("/api/collections/preview", data="{}").status_code == 415
    forbidden = client.get("/api/health", headers={"Host": "example.com"})
    assert forbidden.status_code == 403
    assert forbidden.get_json()["error"]["code"] in {"local_only", "loopback_only"}
