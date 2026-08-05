import json
import stat
from unittest.mock import patch

import pytest

from xscraper.cli import EXIT_PRECONDITION, _configure, _doctor, _run_demo, main
from xscraper.config import Settings
from xscraper.mcp_server import RestClient, run_mcp


def test_paid_smoke_requires_confirmation(capsys):
    assert main(["smoke", "api", "--profile", "OpenAI"]) == EXIT_PRECONDITION
    assert "--confirm-paid-x" in capsys.readouterr().err


def test_configure_saves_owner_only_token_and_environment_overrides(tmp_path, monkeypatch):
    token_path = tmp_path / "auth" / "token"
    settings = Settings(tmp_path / "db", token_path)
    monkeypatch.setattr("xscraper.cli.getpass.getpass", lambda _: "file-token")
    assert _configure(settings) == 0
    assert token_path.read_text().strip() == "file-token"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    monkeypatch.setenv("XSCRAPER_X_BEARER_TOKEN", "environment-token")
    assert settings.bearer_token() == "environment-token"


def test_doctor_checks_live_token_permissions_and_ports(tmp_path, capsys, monkeypatch):
    class FreePort:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def bind(self, address):
            return None

    monkeypatch.setattr("xscraper.cli.socket.socket", FreePort)
    token_path = tmp_path / "auth" / "token"
    settings = Settings(tmp_path / "db", token_path)
    assert _doctor(settings, live=False, port=0) == 0
    settings.database_path.touch()
    assert _doctor(settings, live=False, port=0) == 0
    assert _doctor(settings, live=True, port=0) == EXIT_PRECONDITION
    token_path.write_text("secret")
    token_path.chmod(0o600)
    assert _doctor(settings, live=True, port=0) == 0

    class BusyPort(FreePort):
        def bind(self, address):
            raise OSError("busy")

    monkeypatch.setattr("xscraper.cli.socket.socket", BusyPort)
    assert _doctor(settings, live=False, port=5000) == EXIT_PRECONDITION
    assert "spending limit" in capsys.readouterr().out


def test_offline_demo_is_seeded_isolated_and_blocks_paid_reads(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "real.db", tmp_path / "real-token")
    captured = {}

    def inspect(app, host, port, *, open_browser):
        storage = app.extensions["xscraper_jobs"].storage
        captured["path"] = storage.path
        client = app.test_client()
        jobs = client.get("/api/jobs").get_json()["jobs"]
        assert jobs[0]["status"] == "succeeded"
        posts = client.get(f"/api/jobs/{jobs[0]['id']}/posts").get_json()["posts"]
        assert all(post["text"].startswith("[DEMO DATA]") for post in posts)
        assert client.post("/api/collections/preview", json={}).status_code == 409
        app.extensions["xscraper_jobs"].shutdown()
        return 0

    monkeypatch.setattr("xscraper.cli._run_server", inspect)
    assert _run_demo(settings, live=False, port=0, open_browser=False) == 0
    assert not captured["path"].exists()


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps({"jobs": []}).encode()


def test_mcp_rest_client_proxies_structured_results(monkeypatch):
    seen = {}

    def open_request(request, timeout):
        seen["url"] = request.full_url
        return Response()

    monkeypatch.setattr("xscraper.mcp_server.urlopen", open_request)
    assert RestClient("http://127.0.0.1:5000").call("GET", "/api/jobs") == {"jobs": []}
    assert seen["url"] == "http://127.0.0.1:5000/api/jobs"


def test_mcp_bridge_refuses_non_loopback_and_uses_v2_server():
    with pytest.raises(ValueError, match="loopback"):
        RestClient("https://example.com")
    pytest.importorskip("mcp")
    with patch("mcp.server.MCPServer.run") as run:
        run_mcp()
    run.assert_called_once_with()
