import csv
import io
import json

from xworkbench.api import (
    POST_FIELDS,
    PUBLIC_MEDIA_LIMIT,
    PUBLIC_STRING_LIMIT,
    _spreadsheet_safe,
    create_app,
)
from xworkbench.config import Settings
from xworkbench.models import CollectionRequest, Post
from xworkbench.providers import ProviderRegistry
from xworkbench.storage import Storage


def _completed_snapshot(tmp_path):
    settings = Settings(tmp_path / "workbench.db", tmp_path / "token")
    storage = Storage(settings.database_path)
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1}
    )
    plan = {
        "provider": "playwright_browser",
        "providerVersion": 1,
        "sourceKind": "home",
        "sourceUrl": "https://x.com/home",
        "targetPosts": 1,
    }
    job_id = storage.create_job(request, plan)
    assert storage.claim_job(job_id)
    assert (
        storage.add_posts(
            job_id,
            [
                Post(
                    post_id="1",
                    text="synthetic evidence",
                    author_username="fixture",
                    url="https://x.com/fixture/status/1",
                    created_at=None,
                    media=[],
                )
            ],
            None,
            {},
        )
        == 1
    )
    assert storage.finish_job(job_id, [], completion_reason="target_reached") == "succeeded"
    app = create_app(
        settings,
        storage=storage,
        registry=ProviderRegistry([]),
        start_worker=False,
        collection_enabled=False,
    )
    app.config.update(TESTING=True)
    return app.test_client(), storage, job_id


def test_public_api_recursively_allowlists_storage_values(tmp_path, monkeypatch):
    client, storage, job_id = _completed_snapshot(tmp_path)
    secret = "SENTINEL-MUST-NOT-CROSS-PUBLIC-BOUNDARY"
    job = storage.get_job(job_id)
    post = storage.get_job_posts(job_id)[0]
    assert job is not None

    job["request"].update(authorization=secret, nested={"token": secret})
    job["execution_plan"].update(
        sourceUrl={"token": secret},
        providerVersion={"token": secret},
    )
    job["checkpoint"]["metadata"]["browserVersion"] = {"token": secret}
    job.update(
        warnings=[{"token": secret}],
        error_code=secret,
        error_message={"detail": secret},
        completion_reason={"detail": secret},
    )
    post.update(
        text="x" * (PUBLIC_STRING_LIMIT + 1),
        author_id={"token": secret},
        view_count=42,
        private=secret,
        media=[
            {
                "type": "photo",
                "url": "https://example.invalid/synthetic.jpg",
                "altText": {"token": secret},
                "authorization": secret,
                "variants": [{"token": secret}],
            },
            *(
                {"type": "photo", "url": "https://example.invalid/extra.jpg"}
                for _ in range(30)
            ),
        ],
    )

    monkeypatch.setattr(storage, "get_job", lambda _job_id: job)
    monkeypatch.setattr(storage, "list_jobs", lambda _limit=50: [job])
    monkeypatch.setattr(
        storage,
        "get_job_posts",
        lambda _job_id, *, limit=500, offset=0: [post][offset : offset + limit],
    )

    surfaces = [
        client.get("/api/jobs").get_json(),
        client.get(f"/api/jobs/{job_id}").get_json(),
        client.get(f"/api/jobs/{job_id}/posts").get_json(),
        client.get(f"/api/jobs/{job_id}/export?format=json").get_json(),
    ]
    csv_export = client.get(f"/api/jobs/{job_id}/export?format=csv").get_data(as_text=True)
    public_job = surfaces[1]
    public_post = surfaces[2]["posts"][0]

    assert public_job["request"] == {
        "provider": "playwright_browser",
        "sourceType": "home",
        "sourceValue": "home",
        "maxPosts": 1,
    }
    assert public_job["error"] == {
        "code": "provider_error",
        "message": "The collection provider failed.",
        "retryable": False,
    }
    assert public_job["completionReason"] is None
    assert set(public_post) == set(POST_FIELDS)
    assert public_post["author_id"] is None
    assert public_post["view_count"] == 42
    assert len(public_post["text"]) == PUBLIC_STRING_LIMIT
    assert len(public_post["media"]) == PUBLIC_MEDIA_LIMIT
    assert public_post["media"][0] == {
        "type": "photo",
        "url": "https://example.invalid/synthetic.jpg",
    }
    assert all(secret not in json.dumps(surface) for surface in surfaces)
    assert secret not in csv_export
    assert next(csv.DictReader(io.StringIO(csv_export)))["view_count"] == "42"


def test_loopback_boundaries_and_csv_control_prefixes(tmp_path):
    client, _, job_id = _completed_snapshot(tmp_path)

    for request_options in (
        {"headers": {"Host": ""}},
        {"headers": {"Host": "user@localhost"}},
        {"headers": {"Host": "example.invalid"}},
        {"environ_overrides": {"REMOTE_ADDR": ""}},
        {"environ_overrides": {"REMOTE_ADDR": "192.0.2.1"}},
    ):
        assert client.get("/api/health", **request_options).status_code == 403

    for origin in (
        "https://attacker.invalid",
        "https://localhost",
        "http://localhost:81",
        "http://user@localhost",
        "http://[::1",
        "null",
    ):
        response = client.post(
            f"/api/jobs/{job_id}/cancel",
            json={},
            headers={"Origin": origin},
        )
        assert response.status_code == 403
        assert response.get_json()["error"]["code"] == "local_origin_required"

    assert client.post("/api/collections/preview", json={}).status_code == 409
    assert (
        client.post(
            "/api/collections/preview",
            json={},
            headers={"Origin": "http://LOCALHOST:80"},
        ).status_code
        == 409
    )

    for prefix in ("\ufeff", "\u200b", "\x00", "\t", "\r", "\n", " "):
        for trigger in "=+-@":
            assert _spreadsheet_safe(f"{prefix}{trigger}1").startswith("'")
    assert _spreadsheet_safe("ordinary evidence") == "ordinary evidence"
