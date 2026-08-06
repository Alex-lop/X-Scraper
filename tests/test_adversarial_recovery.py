import csv
import io
import json
from types import SimpleNamespace

from xworkbench.api import create_app
from xworkbench.config import Settings
from xworkbench.jobs import JobService
from xworkbench.models import CollectionRequest
from xworkbench.playwright_browser import PlaywrightBrowserProvider
from xworkbench.providers import ProviderRegistry
from xworkbench.storage import Storage


def _article(text="Please verify you are human — quoted as research"):
    return {
        "identityCandidates": [
            {
                "href": "https://x.com/tester/status/123",
                "depth": 2,
                "order": 0,
                "hasTime": True,
                "timestamp": "2026-08-05T12:00:00Z",
                "nested": False,
            }
        ],
        "text": text,
        "userText": "Tester @tester",
        "articleText": text,
        "socialContext": None,
        "metrics": {},
        "media": [],
        "sourcePosition": 0,
    }


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        return 0

    def wait_for(self, **_kwargs):
        pass

    def evaluate(self, _script):
        value = self.page.chrome_texts[
            min(self.page.chrome_index, len(self.page.chrome_texts) - 1)
        ]
        self.page.chrome_index += 1
        return value

    def evaluate_all(self, _script):
        value = self.page.scans[min(self.page.scan_index, len(self.page.scans) - 1)]
        self.page.scan_index += 1
        return value


class _Page:
    def __init__(self):
        self.url = "https://x.com/home"
        self.chrome_texts = ["", "", "Please verify you are human"]
        self.chrome_index = 0
        self.scans = [[_article()]]
        self.scan_index = 0
        self.scrolls = 0
        self.closed = False

    def locator(self, selector):
        return _Locator(self, selector)

    def set_default_timeout(self, _timeout):
        pass

    def goto(self, url, **_kwargs):
        self.url = url

    def evaluate(self, _script):
        self.scrolls += 1

    def wait_for_timeout(self, _timeout):
        pass

    def close(self):
        self.closed = True


class _Lifecycle:
    def __init__(self):
        self.page = _Page()
        self.context = SimpleNamespace(new_page=lambda: self.page, closed=False)
        self.browser = SimpleNamespace(
            version="adversarial-chromium",
            new_context=lambda **_kwargs: self.context,
            closed=False,
        )
        self.context.close = lambda: setattr(self.context, "closed", True)
        self.browser.close = lambda: setattr(self.browser, "closed", True)
        self.chromium = SimpleNamespace(launch=lambda **_kwargs: self.browser)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


def _browser_stack(tmp_path):
    settings = Settings(
        tmp_path / "snapshots.db",
        tmp_path / "auth" / "token",
        storage_state_path=tmp_path / "auth" / "playwright.json",
    )
    settings.ensure_runtime_dirs()
    settings.storage_state_path.write_text("{}")
    lifecycle = _Lifecycle()
    provider = PlaywrightBrowserProvider(
        settings, _playwright_factory=lambda: lifecycle
    )
    storage = Storage(settings.database_path)
    storage.initialize()
    registry = ProviderRegistry([provider])
    return settings, lifecycle, provider, storage, registry


def test_real_browser_job_persists_before_challenge_and_public_surfaces_hide_secrets(tmp_path):
    settings, lifecycle, provider, storage, registry = _browser_stack(tmp_path)
    request = CollectionRequest.from_dict(
        {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 2}
    )
    job_id = storage.create_job(request, provider.prepare(request))

    JobService(storage, registry, start_worker=False).run_once(job_id)

    job = storage.get_job(job_id)
    assert (job["status"], job["completion_reason"], job["error_code"]) == (
        "partial",
        "manual_action_required",
        "manual_action_required",
    )
    assert job["error_retryable"] is False and job["collected_count"] == 1
    assert storage.get_job_posts(job_id)[0]["text"].startswith("Please verify")
    assert lifecycle.page.scrolls == 1
    assert lifecycle.page.closed and lifecycle.context.closed and lifecycle.browser.closed

    secret = "SHOULD-NOT-LEAVE-SQLITE"
    with storage.connect() as connection:
        plan = json.loads(
            connection.execute(
                "SELECT compiled_request_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()[0]
        )
        plan.update(
            headers={"Authorization": secret},
            token=secret,
            authStatePath=secret,
        )
        checkpoint = {
            "checkpointVersion": 1,
            "providerState": {"cookies": [secret], "seenPostIds": ["123"]},
            "metadata": {"headers": {"Authorization": secret}},
        }
        connection.execute(
            "UPDATE jobs SET compiled_request_json = ?, cursor = ? WHERE id = ?",
            (json.dumps(plan), json.dumps(checkpoint), job_id),
        )

    app = create_app(settings, storage=storage, registry=registry, start_worker=False)
    app.config.update(TESTING=True)
    client = app.test_client()
    public = client.get(f"/api/jobs/{job_id}").get_json()
    exported = client.get(f"/api/jobs/{job_id}/export?format=json").get_json()
    csv_text = client.get(f"/api/jobs/{job_id}/export?format=csv").get_data(as_text=True)

    assert public["isPartial"] is True and public["completionReason"] == "manual_action_required"
    assert not {"cost", "resourcesReturned", "rateLimit"}.intersection(public)
    assert secret not in json.dumps(public) and secret not in json.dumps(exported)
    assert secret not in csv_text
    row = next(csv.DictReader(io.StringIO(csv_text)))
    assert row["provider"] == "playwright_browser"
    assert row["post_resources_returned"] == ""


def test_browser_job_creation_rejects_tampered_and_cross_provider_plans(tmp_path):
    settings, _, provider, storage, registry = _browser_stack(tmp_path)
    app = create_app(settings, storage=storage, registry=registry, start_worker=False)
    app.config.update(TESTING=True)
    client = app.test_client()
    request = {"provider": "playwright_browser", "sourceType": "home", "maxPosts": 2}
    plan = client.post("/api/collections/preview", json=request).get_json()["executionPlan"]

    for changed in (
        {"targetPosts": 1},
        {"provider": "official_x_api"},
        {"providerVersion": 999},
        {"sourceUrl": "https://example.invalid/home"},
    ):
        response = client.post(
            "/api/jobs",
            json={
                **request,
                "executionPlan": {**plan, **changed},
                "confirmBrowserCapture": True,
            },
        )
        assert response.status_code == 400

    assert storage.list_jobs() == []
