import os
import stat

import pytest

from xworkbench.cli import (
    EXIT_PRECONDITION,
    _configure,
    _doctor,
    _run_demo,
    build_parser,
    main,
)
from xworkbench.config import Settings


def test_cli_exposes_recovered_commands_and_bounded_live_gate():
    parser = build_parser()
    command_action = next(action for action in parser._actions if action.choices)

    assert set(command_action.choices) == {
        "configure",
        "setup",
        "auth",
        "doctor",
        "start",
        "serve",
        "config",
        "demo",
        "mcp",
        "live-smoke",
    }
    assert parser.parse_args(["serve", "--port", "0"]).port == 0
    assert parser.parse_args(["mcp"]).url is None
    assert parser.parse_args(["doctor", "--require-token"]).require_token is True
    assert parser.parse_args(["live-smoke", "--confirm-live-x"]).confirm_live_x is True


def test_settings_include_app_owned_browser_state_and_headed_defaults(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("XWORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XWORKBENCH_X_BEARER_TOKEN", "environment-token")

    settings = Settings.from_env()

    assert settings.database_path == runtime / "x_collection_workbench.db"
    assert settings.bearer_token_path == runtime / "auth" / "x_bearer_token"
    assert settings.storage_state_path == runtime / "auth" / "playwright_state.json"
    assert settings.browser_headless is False
    assert settings.job_timeout_seconds == 120
    assert settings.page_timeout_ms == 30_000
    assert settings.no_progress_limit == 3
    assert settings.bearer_token() == "environment-token"


def test_configure_saves_owner_only_optional_token(tmp_path, monkeypatch):
    token_path = tmp_path / "auth" / "token"
    settings = Settings(tmp_path / "db", token_path)
    monkeypatch.setattr("xworkbench.cli.getpass.getpass", lambda _: "file-token")

    assert _configure(settings) == 0
    assert token_path.read_text().strip() == "file-token"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_doctor_checks_browser_chromium_session_database_and_port_without_token(
    tmp_path, capsys, monkeypatch
):
    class FreePort:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def bind(self, address):
            return None

    class Browser:
        def __init__(self, settings):
            self.settings = settings

        def connection_status(self):
            return {"status": "ready", "ready": True, "message": "ready"}

    monkeypatch.setattr("xworkbench.cli.socket.socket", FreePort)
    monkeypatch.setattr("xworkbench.cli.importlib.util.find_spec", lambda _: object())
    monkeypatch.setattr(
        "xworkbench.cli._chromium_available", lambda: (True, "Playwright Chromium is installed")
    )
    monkeypatch.setattr("xworkbench.cli.PlaywrightBrowserProvider", Browser)
    settings = Settings(tmp_path / "db", tmp_path / "token")
    assert settings.storage_state_path is not None
    settings.ensure_runtime_dirs()
    settings.storage_state_path.write_text("{}")
    settings.storage_state_path.chmod(0o600)

    assert _doctor(settings, require_token=False, port=0) == 0
    output = capsys.readouterr().out
    assert "Official X API token missing" in output
    assert "READY" in output

    assert _doctor(settings, require_token=True, port=0) == EXIT_PRECONDITION


def test_auth_dispatches_to_manual_browser_helper_without_credentials(
    tmp_path, monkeypatch, capsys
):
    settings = Settings(tmp_path / "db", tmp_path / "token")
    assert settings.storage_state_path is not None
    seen = []
    monkeypatch.setattr("xworkbench.cli.Settings.from_env", lambda: settings)
    monkeypatch.setattr(
        "xworkbench.cli.authenticate_interactively",
        lambda supplied: seen.append(supplied) or supplied.storage_state_path,
    )

    assert main(["auth"]) == 0
    assert seen == [settings]
    assert "Saved protected browser session" in capsys.readouterr().out


def test_live_smoke_requires_explicit_confirmation_before_provider_use(monkeypatch, capsys):
    class ShouldNotConstruct:
        def __init__(self, settings):
            raise AssertionError("provider must not be constructed")

    monkeypatch.setattr("xworkbench.cli.PlaywrightBrowserProvider", ShouldNotConstruct)
    assert main(["live-smoke"]) == EXIT_PRECONDITION
    assert "--confirm-live-x" in capsys.readouterr().err


def test_live_smoke_rejects_legacy_ready_without_verified_live(tmp_path, monkeypatch, capsys):
    class LegacyReady:
        def __init__(self, settings):
            self.settings = settings

        def connection_status(self):
            return {"status": "ready", "ready": True}

    settings = Settings(tmp_path / "db", tmp_path / "token")
    monkeypatch.setattr("xworkbench.cli.Settings.from_env", lambda: settings)
    monkeypatch.setattr("xworkbench.cli.PlaywrightBrowserProvider", LegacyReady)

    assert main(["live-smoke", "--confirm-live-x"]) == EXIT_PRECONDITION
    assert "browser session ready" in capsys.readouterr().err


def test_serve_refuses_non_loopback_host():
    with pytest.raises(SystemExit, match="local-only"):
        main(["serve", "--host", "0.0.0.0", "--no-open"])


def test_offline_demo_is_preseeded_isolated_and_blocks_live_collection(monkeypatch):
    captured = {}
    monkeypatch.setenv("XWORKBENCH_X_BEARER_TOKEN", "must-not-reach-demo")

    def inspect(app, host, port, *, open_browser):
        service = app.extensions["xworkbench_jobs"]
        captured["path"] = service.storage.path
        client = app.test_client()
        connection = client.get("/api/connection").get_json()
        assert connection["demoMode"] == "offline"
        jobs = client.get("/api/jobs").get_json()["jobs"]
        assert len(jobs) == 2
        assert jobs[0]["provider"] == "playwright_browser"
        assert "cost" not in jobs[0] and "resourcesReturned" not in jobs[0]
        posts = client.get(f"/api/jobs/{jobs[0]['id']}/posts").get_json()["posts"]
        assert len(posts) == 25
        assert all(post["text"].startswith("[FICTIONAL DEMO]") for post in posts)
        assert all(post["url"].startswith("offline://") for post in posts)
        assert client.post(
            "/api/collections/preview",
            json={"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1},
        ).status_code == 409
        exported = client.get(f"/api/jobs/{jobs[0]['id']}/export?format=json").get_json()
        assert set(exported) == {"schemaVersion", "job", "posts"}
        service.shutdown()
        return 0

    monkeypatch.setattr("xworkbench.cli._run_server", inspect)
    assert _run_demo(port=0, open_browser=False) == 0
    assert os.environ["XWORKBENCH_X_BEARER_TOKEN"] == "must-not-reach-demo"
    assert not captured["path"].exists()
