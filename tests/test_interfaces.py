import os
import stat
import threading

import pytest

from xworkbench.api import create_app
from xworkbench.cli import (
    EXIT_PRECONDITION,
    _configure,
    _doctor,
    _run_demo,
    _run_server,
    build_parser,
    main,
)
from xworkbench.config import Settings, SettingsError, save_bearer_token
from xworkbench.local_client import LocalJsonClient
from xworkbench.providers import ProviderRegistry
from xworkbench.storage import Storage


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
        "tui",
        "monitor",
        "live-smoke",
    }
    assert parser.parse_args(["serve", "--port", "0"]).port == 0
    assert parser.parse_args(["mcp"]).url is None
    assert parser.parse_args(["tui", "--port", "0"]).port == 0
    assert parser.parse_args(["monitor"]).url == "http://127.0.0.1:5000"
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


def test_protected_token_writer_rejects_unsafe_values_and_preserves_existing_file(
    tmp_path, monkeypatch
):
    token_path = tmp_path / "auth" / "token"
    settings = Settings(tmp_path / "db", token_path)
    assert save_bearer_token(settings, "first-token") == token_path

    for invalid in ("line\nbreak", "x" * 4097):
        with pytest.raises(SettingsError, match="printable line"):
            save_bearer_token(settings, invalid)
    assert token_path.read_text(encoding="utf-8") == "first-token\n"

    original_replace = type(token_path).replace
    monkeypatch.setattr(
        type(token_path),
        "replace",
        lambda self, target: (
            (_ for _ in ()).throw(OSError("replace failed"))
            if target == token_path
            else original_replace(self, target)
        ),
    )
    with pytest.raises(OSError, match="replace failed"):
        save_bearer_token(settings, "second-token")
    assert token_path.read_text(encoding="utf-8") == "first-token\n"
    assert list(token_path.parent.glob(".token.*")) == []


def test_protected_token_writer_rejects_symlink(tmp_path):
    settings = Settings(tmp_path / "db", tmp_path / "auth" / "token")
    settings.ensure_runtime_dirs()
    target = tmp_path / "target"
    target.write_text("prior\n", encoding="utf-8")
    target.chmod(0o600)
    settings.bearer_token_path.symlink_to(target)

    with pytest.raises(SettingsError, match="not a symlink"):
        save_bearer_token(settings, "new-token")
    assert target.read_text(encoding="utf-8") == "prior\n"


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


@pytest.mark.parametrize("command", [["tui"], ["monitor"]])
def test_terminal_missing_extra_fails_before_runtime_or_worker(
    command, tmp_path, monkeypatch, capsys
):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("XWORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr("xworkbench.cli.importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr(
        "xworkbench.cli.create_app",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("worker must not be created")
        ),
    )

    assert main(command) == EXIT_PRECONDITION
    assert not runtime.exists()
    assert "install with" in capsys.readouterr().err


def test_monitor_dispatch_never_constructs_settings_or_runtime(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "xworkbench.cli._terminal_entrypoints",
        lambda: (None, lambda url: seen.append(url) or 0),
    )
    monkeypatch.setattr(
        "xworkbench.cli.Settings.from_env",
        lambda: (_ for _ in ()).throw(AssertionError("monitor must not load settings")),
    )

    assert main(["monitor", "--url", "http://127.0.0.1:6123"]) == 0
    assert seen == ["http://127.0.0.1:6123"]


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


def test_owner_server_resolves_port_runs_frontend_on_main_and_cleans_up(tmp_path):
    settings = Settings(tmp_path / "owner.db", tmp_path / "auth" / "token")
    storage = Storage(settings.database_path)
    app = create_app(settings, storage=storage, registry=ProviderRegistry([]))
    service = app.extensions["xworkbench_jobs"]
    caller = threading.get_ident()
    seen = {}

    def frontend(url):
        seen["thread"] = threading.get_ident()
        seen["url"] = url
        seen["health"] = LocalJsonClient(url).get("/api/health")
        assert any(
            thread.name == "xworkbench-http" and thread.is_alive()
            for thread in threading.enumerate()
        )
        return 7

    assert _run_server(app, "127.0.0.1", 0, open_browser=False, frontend=frontend) == 7
    assert seen["thread"] == caller
    assert seen["url"].startswith("http://127.0.0.1:")
    assert seen["url"] != "http://127.0.0.1:0"
    assert seen["health"]["status"] == "ok"
    assert not any(thread.is_alive() for thread in service._threads)
    assert not service._lock_path.exists()
    assert not any(thread.name == "xworkbench-http" for thread in threading.enumerate())


def test_owner_server_cleans_up_after_frontend_error(tmp_path):
    settings = Settings(tmp_path / "owner-error.db", tmp_path / "auth" / "token")
    storage = Storage(settings.database_path)
    app = create_app(settings, storage=storage, registry=ProviderRegistry([]))
    service = app.extensions["xworkbench_jobs"]

    with pytest.raises(RuntimeError, match="frontend failed"):
        _run_server(
            app,
            "127.0.0.1",
            0,
            open_browser=False,
            frontend=lambda _url: (_ for _ in ()).throw(RuntimeError("frontend failed")),
        )

    assert not any(thread.is_alive() for thread in service._threads)
    assert not service._lock_path.exists()


def test_owner_server_bind_failure_still_stops_worker(monkeypatch):
    class Service:
        stopped = False

        def shutdown(self):
            self.stopped = True

    service = Service()
    app = type("App", (), {"extensions": {"xworkbench_jobs": service}})()
    monkeypatch.setattr(
        "xworkbench.cli.make_server",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("busy")),
    )

    assert _run_server(app, "127.0.0.1", 5000, open_browser=False) == EXIT_PRECONDITION
    assert service.stopped is True


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
        assert (
            client.post(
                "/api/collections/preview",
                json={"provider": "playwright_browser", "sourceType": "home", "maxPosts": 1},
            ).status_code
            == 409
        )
        exported = client.get(f"/api/jobs/{jobs[0]['id']}/export?format=json").get_json()
        assert set(exported) == {"schemaVersion", "job", "posts"}
        service.shutdown()
        return 0

    monkeypatch.setattr("xworkbench.cli._run_server", inspect)
    assert _run_demo(port=0, open_browser=False) == 0
    assert os.environ["XWORKBENCH_X_BEARER_TOKEN"] == "must-not-reach-demo"
    assert not captured["path"].exists()
