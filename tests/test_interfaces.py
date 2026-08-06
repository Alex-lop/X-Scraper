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


def test_cli_exposes_only_the_four_supported_commands():
    parser = build_parser()
    command_action = next(action for action in parser._actions if action.choices)

    assert set(command_action.choices) == {"configure", "doctor", "serve", "demo"}
    assert parser.parse_args(["serve", "--port", "0"]).port == 0
    assert parser.parse_args(["doctor", "--require-token"]).require_token is True


def test_settings_use_xworkbench_environment_names(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("XWORKBENCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XWORKBENCH_X_BEARER_TOKEN", "environment-token")

    settings = Settings.from_env()

    assert settings.database_path == runtime / "x_collection_workbench.db"
    assert settings.bearer_token_path == runtime / "auth" / "x_bearer_token"
    assert settings.bearer_token() == "environment-token"
    assert settings.connection_status()["source"] == "environment"


def test_configure_saves_owner_only_token_and_environment_overrides(tmp_path, monkeypatch):
    token_path = tmp_path / "auth" / "token"
    settings = Settings(tmp_path / "db", token_path)
    monkeypatch.setattr("xworkbench.cli.getpass.getpass", lambda _: "file-token")

    assert _configure(settings) == 0
    assert token_path.read_text().strip() == "file-token"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600

    monkeypatch.setenv("XWORKBENCH_X_BEARER_TOKEN", "environment-token")
    assert settings.bearer_token() == "environment-token"


def test_doctor_checks_required_token_permissions_and_ports(tmp_path, capsys, monkeypatch):
    class FreePort:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def bind(self, address):
            return None

    monkeypatch.setattr("xworkbench.cli.socket.socket", FreePort)
    token_path = tmp_path / "auth" / "token"
    settings = Settings(tmp_path / "db", token_path)

    assert _doctor(settings, require_token=False, port=0) == 0
    assert _doctor(settings, require_token=True, port=0) == EXIT_PRECONDITION
    token_path.write_text("secret")
    token_path.chmod(0o600)
    assert _doctor(settings, require_token=True, port=0) == 0

    class BusyPort(FreePort):
        def bind(self, address):
            raise OSError("busy")

    monkeypatch.setattr("xworkbench.cli.socket.socket", BusyPort)
    assert _doctor(settings, require_token=False, port=5000) == EXIT_PRECONDITION
    assert "spending limit" in capsys.readouterr().out


def test_serve_refuses_non_loopback_host():
    with pytest.raises(SystemExit, match="local-only"):
        main(["serve", "--host", "0.0.0.0", "--no-open"])


def test_offline_demo_is_preseeded_isolated_and_blocks_live_reads(monkeypatch):
    captured = {}
    monkeypatch.setenv("XWORKBENCH_X_BEARER_TOKEN", "must-not-reach-demo")

    def inspect(app, host, port, *, open_browser):
        service = app.extensions["xworkbench_jobs"]
        captured["path"] = service.storage.path
        client = app.test_client()
        jobs = client.get("/api/jobs").get_json()["jobs"]
        assert jobs[0]["status"] == "succeeded"
        posts = client.get(f"/api/jobs/{jobs[0]['id']}/posts").get_json()["posts"]
        assert len(posts) >= 3
        assert all(post["text"].startswith("[DEMO DATA]") for post in posts)

        exported = client.get(f"/api/jobs/{jobs[0]['id']}/export?format=json").get_json()
        assert len(exported["posts"]) == len(posts)
        assert set(exported) == {"schemaVersion", "job", "posts"}

        blocked = client.post(
            "/api/collections/preview",
            json={"sourceType": "search", "sourceValue": "demo", "maxPosts": 10},
        )
        assert blocked.status_code == 409
        service.shutdown()
        return 0

    monkeypatch.setattr("xworkbench.cli._run_server", inspect)
    assert _run_demo(port=0, open_browser=False) == 0
    assert os.environ["XWORKBENCH_X_BEARER_TOKEN"] == "must-not-reach-demo"
    assert not captured["path"].exists()
