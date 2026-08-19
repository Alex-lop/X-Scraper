import json
import os
import sqlite3
import stat

import pytest

from xworkbench.cli import (
    EXIT_BROWSER,
    EXIT_CONFIG,
    EXIT_PRECONDITION,
    _database_ready,
    _doctor,
    _setup,
    build_parser,
    main,
)
from xworkbench.config import MAX_TOKEN_LENGTH, Settings, SettingsError
from xworkbench.playwright_browser import BrowserUnavailableError
from xworkbench.storage import SCHEMA_FAMILY, Storage


def protected_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def test_parser_keeps_aliases_and_adds_minimal_first_run_commands():
    parser = build_parser()
    command_action = next(action for action in parser._actions if action.choices)

    assert {"setup", "start", "serve", "config"} <= set(command_action.choices)
    assert parser.parse_args(["config", "validate"]).config_command == "validate"
    assert parser.parse_args(["start", "--port", "0"]).port == 0


def test_malformed_settings_have_stable_error_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XWORKBENCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XWORKBENCH_JOB_TIMEOUT_SECONDS", "forever")

    assert main(["config", "show"]) == EXIT_CONFIG
    error = capsys.readouterr().err
    assert "job_timeout_seconds must be an integer" in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    "content, expected",
    [
        ("not-json", "valid UTF-8 JSON"),
        ('{"surprise": true}', "Unknown config key"),
        ('{"job_timeout_seconds": 0}', "from 1 to 3600"),
        ('{"max_workers": 3}', "from 1 to 2"),
    ],
)
def test_config_rejects_malformed_unknown_and_unsafe_values(
    tmp_path, monkeypatch, content, expected
):
    config = tmp_path / "config.json"
    protected_write(config, content)
    monkeypatch.setenv("XWORKBENCH_RUNTIME_DIR", str(tmp_path))

    with pytest.raises(SettingsError, match=expected):
        Settings.from_env()


def test_config_and_token_reject_world_readable_files_and_symlinks(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    config.chmod(0o644)
    monkeypatch.setenv("XWORKBENCH_RUNTIME_DIR", str(tmp_path))
    with pytest.raises(SettingsError, match="0600"):
        Settings.from_env()

    config.chmod(0o600)
    token_target = tmp_path / "actual-token"
    token_target.write_text("secret\n", encoding="utf-8")
    token_target.chmod(0o644)
    with pytest.raises(SettingsError, match="0600"):
        Settings(tmp_path / "db", token_target).bearer_token()

    token_target.chmod(0o600)
    token_link = tmp_path / "token-link"
    token_link.symlink_to(token_target)
    configured = Settings(tmp_path / "db", token_link)
    with pytest.raises(SettingsError, match="not a symlink"):
        configured.bearer_token()


def test_browser_state_path_must_remain_app_owned(tmp_path):
    with pytest.raises(SettingsError, match="app-owned auth directory"):
        Settings(
            tmp_path / "runtime" / "db",
            tmp_path / "runtime" / "auth" / "token",
            storage_state_path=tmp_path / "unrelated-profile" / "state.json",
        )


@pytest.mark.parametrize(
    "token",
    ["two\nlines", "control\x00character", "x" * (MAX_TOKEN_LENGTH + 1)],
)
def test_token_is_bounded_printable_and_single_line(tmp_path, token):
    path = tmp_path / "token"
    protected_write(path, token)
    with pytest.raises(SettingsError, match="one printable line"):
        Settings(tmp_path / "db", path).bearer_token()


def test_setup_is_idempotent_and_ignores_permissive_umask(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "runtime" / "db", tmp_path / "runtime" / "auth" / "token")
    monkeypatch.setattr("xworkbench.cli._doctor", lambda *_args, **_kwargs: 0)
    previous_umask = os.umask(0)
    try:
        assert _setup(settings) == 0
        assert _setup(settings) == 0
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE((tmp_path / "runtime").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "runtime" / "auth").stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.database_path.stat().st_mode) == 0o600


def test_setup_keeps_the_offline_demo_available_without_chromium(
    tmp_path, monkeypatch, capsys
):
    settings = Settings(
        tmp_path / "runtime" / "db",
        tmp_path / "runtime" / "auth" / "token",
    )
    monkeypatch.setattr(
        "xworkbench.cli._chromium_available",
        lambda: (False, "Chromium is missing; run: python -m playwright install chromium"),
    )

    assert _setup(settings) == 0
    output = capsys.readouterr().out
    assert "WARN  chromium" in output
    assert "python -m playwright install chromium" in output
    assert "READY WITH WARNINGS" in output


def test_setup_does_not_chmod_an_unsafe_existing_parent(tmp_path):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    settings = Settings(parent / "db", parent / "token")

    with pytest.raises(SettingsError, match="permissions 0700"):
        settings.ensure_runtime_dirs()

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755


@pytest.mark.parametrize("version", ["1", "2", "3"])
def test_doctor_reports_each_legacy_database_as_ready_for_v4_migration(
    tmp_path, monkeypatch, version
):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.executemany(
            "INSERT INTO schema_meta VALUES (?, ?)",
            (("schema_family", SCHEMA_FAMILY), ("schema_version", version)),
        )
    monkeypatch.setattr(Storage, "_schema_is_compatible", lambda *_args, **_kwargs: True)

    ready, message = _database_ready(database)

    assert ready is True
    assert f"v{version} ready for protected migration" in message


def test_repeated_database_readiness_checks_close_connections(tmp_path):
    resource = pytest.importorskip("resource")
    database = tmp_path / "current.db"
    Storage(database).initialize()
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    lowered = min(soft, 64)
    if lowered < 32:
        pytest.skip("The process file-descriptor limit is already too low for this test.")
    resource.setrlimit(resource.RLIMIT_NOFILE, (lowered, hard))
    try:
        for _ in range(256):
            assert _database_ready(database)[0] is True
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_doctor_is_read_only_and_does_not_expose_invalid_state(
    tmp_path, monkeypatch, capsys
):
    runtime = tmp_path / "missing-runtime"
    settings = Settings(runtime / "db", runtime / "auth" / "token")
    monkeypatch.setattr(
        "xworkbench.cli._chromium_available", lambda: (True, "Chromium launches")
    )

    assert _doctor(settings, require_token=False, port=0) == EXIT_PRECONDITION
    assert not runtime.exists()

    settings.ensure_runtime_dirs()
    secret = "do-not-print-this-state"
    protected_write(settings.storage_state_path, secret)
    assert _doctor(settings, require_token=False, port=0) == EXIT_PRECONDITION
    output = capsys.readouterr().out
    assert secret not in output
    assert "invalid local Playwright JSON" in output


def test_doctor_keeps_last_live_verification_visible_after_expiry(
    tmp_path, monkeypatch, capsys
):
    settings = Settings(tmp_path / "db", tmp_path / "token")
    Storage(settings.database_path).initialize()
    monkeypatch.setattr(
        "xworkbench.cli._chromium_available", lambda: (True, "Chromium launches")
    )
    monkeypatch.setattr(
        "xworkbench.cli.PlaywrightBrowserProvider.connection_status",
        lambda _provider: {
            "status": "expired",
            "localStateValid": True,
            "verifiedAt": "2026-08-19T04:00:00+00:00",
        },
    )

    assert _doctor(settings, require_token=False, port=0) == 0
    output = capsys.readouterr().out
    assert "last verified live at 2026-08-19T04:00:00+00:00" in output
    assert "current status is expired" in output


def test_doctor_treats_missing_auth_as_offline_ready_warning(
    tmp_path, monkeypatch, capsys
):
    settings = Settings(tmp_path / "runtime" / "db", tmp_path / "runtime" / "token")
    settings.ensure_runtime_dirs()
    Storage(settings.database_path).initialize()
    monkeypatch.setattr(
        "xworkbench.cli._chromium_available", lambda: (True, "Chromium launches")
    )
    monkeypatch.setattr(
        "xworkbench.cli.PlaywrightBrowserProvider.connection_status",
        lambda _provider: {"status": "missing", "localStateValid": False},
    )

    assert _doctor(settings, require_token=False, port=0) == 0
    output = capsys.readouterr().out
    assert "WARN  local auth state" in output
    assert "READY WITH WARNINGS" in output


def test_config_show_redacts_environment_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XWORKBENCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XWORKBENCH_X_BEARER_TOKEN", "never-print-me")

    assert main(["config", "show"]) == 0
    shown = capsys.readouterr().out
    assert "never-print-me" not in shown
    assert json.loads(shown)["database_path"].endswith("x_collection_workbench.db")
    resolved = json.loads(shown)
    assert resolved["max_workers"] == resolved["per_auth_state_concurrency"] == 1
    assert resolved["hard_worker_maximum"] == 4
    assert resolved["route_mode"] == "direct"


def test_expected_browser_failure_has_stable_exit_without_traceback(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XWORKBENCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        "xworkbench.cli.authenticate_interactively",
        lambda _settings: (_ for _ in ()).throw(BrowserUnavailableError("no GUI")),
    )

    assert main(["auth"]) == EXIT_BROWSER
    error = capsys.readouterr().err
    assert "BROWSER ERROR [browser_unavailable]: no GUI" in error
    assert "Traceback" not in error
