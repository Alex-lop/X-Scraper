from __future__ import annotations

import argparse
import getpass
import importlib.metadata
import importlib.util
import ipaddress
import json
import logging
import os
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path

from werkzeug.serving import make_server

from .api import BROWSER_PROVIDER, create_app
from .config import Settings, SettingsError, validate_token
from .errors import CollectionError
from .jobs import JobService
from .models import CollectionRequest, Post
from .playwright_browser import PlaywrightBrowserProvider, authenticate_interactively
from .providers import ProviderRegistry
from .storage import SCHEMA_FAMILY, SCHEMA_VERSION, Storage

EXIT_PRECONDITION = 2
EXIT_CONFIG = 3
EXIT_BROWSER = 4


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local feed-to-context snapshot bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Initialize protected local files and run doctor")
    commands.add_parser("configure", help="Save an optional X API Bearer Token locally")
    commands.add_parser("auth", help="Open headed Chromium for manual X sign-in")

    doctor = commands.add_parser("doctor", help="Check local browser and storage prerequisites")
    doctor.add_argument("--require-token", action="store_true", help="Require an official X token")
    doctor.add_argument("--port", type=_port, default=0, help="Check a port; 0 means any free port")

    for name, help_text in (
        ("start", "Run the loopback dashboard and worker"),
        ("serve", "Alias for start"),
    ):
        server = commands.add_parser(name, help=help_text)
        server.add_argument("--host", default="127.0.0.1")
        server.add_argument("--port", type=_port, default=5000)
        server.add_argument("--no-open", action="store_true", help="Do not open the dashboard")

    config = commands.add_parser("config", help="Inspect or validate local configuration")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("show", help="Show resolved non-secret settings")
    config_commands.add_parser("validate", help="Validate settings and protected files")

    demo = commands.add_parser("demo", help="Run an isolated synthetic-data demo")
    demo.add_argument("--port", type=_port, default=0, help="Use 0 to select a free port")
    demo.add_argument("--no-open", action="store_true", help="Do not open the dashboard")

    mcp = commands.add_parser("mcp", help="Expose terminal local snapshots over MCP stdio")
    mcp.add_argument(
        "--url",
        default=None,
        help="Use the legacy loopback REST adapter instead of direct read-only SQLite",
    )

    smoke = commands.add_parser("live-smoke", help="Capture at most two live Home-feed Posts")
    smoke.add_argument("--confirm-live-x", action="store_true")
    return parser


def _configure(settings: Settings) -> int:
    try:
        token = validate_token(getpass.getpass("X API Bearer Token (input hidden): ").strip())
    except SettingsError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return EXIT_PRECONDITION
    settings.ensure_runtime_dirs()
    settings.validate_local_files()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{settings.bearer_token_path.name}.",
        dir=settings.bearer_token_path.parent,
        text=True,
    )
    temporary = settings.bearer_token_path.with_name(os.path.basename(temporary_name))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(token + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(settings.bearer_token_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Saved Bearer Token to {settings.bearer_token_path}")
    return 0


def _database_ready(path: Path) -> tuple[bool, str]:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return True, f"new database will be created at {path}"
    except OSError as exc:
        return False, f"database path cannot be inspected: {exc}"
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        return False, f"database must be a regular file, not a symlink: {path}"
    if details.st_size == 0:
        return True, f"new database will be created at {path}"
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))
            version = metadata.get("schema_version")
            compatible = False
            if metadata.get("schema_family") == SCHEMA_FAMILY and version in {
                "1",
                "2",
                SCHEMA_VERSION,
            }:
                checker = Storage(path)._schema_is_compatible
                try:
                    compatible = checker(connection, version=int(version))
                except TypeError:
                    compatible = checker(connection)
    except sqlite3.DatabaseError:
        return False, f"database at {path} is unreadable or incompatible"
    if compatible and version != SCHEMA_VERSION:
        return True, f"database v{version} ready for protected migration at {path}"
    return compatible, (
        f"database ready at {path}" if compatible else f"database at {path} is incompatible"
    )


def _chromium_available() -> tuple[bool, str]:
    try:
        package = importlib.util.find_spec("playwright.sync_api") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        package = False
    if not package:
        return False, 'Playwright is missing; run: python -m pip install -e ".[browser]"'
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.is_file():
                return False, "Chromium is missing; run: python -m playwright install chromium"
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        return False, (
            f"Chromium cannot launch ({type(exc).__name__}); "
            "run: python -m playwright install chromium"
        )
    revision = next(
        (
            part
            for part in executable.parts
            if part.startswith(("chromium-", "chromium_headless_shell-"))
        ),
        executable.parent.name,
    )
    return True, f"Playwright Chromium is installed and launches ({revision})"


def _doctor(settings: Settings, *, require_token: bool, port: int) -> int:
    failures: list[str] = []
    warnings: list[str] = []

    def result(level: str, check: str, message: str, remediation: str | None = None) -> None:
        print(f"{level:<4}  {check:<18} {message}")
        if remediation and level != "PASS":
            print(f"      Fix: {remediation}")
        if level == "FAIL":
            failures.append(check)
        elif level == "WARN":
            warnings.append(check)

    result(
        "PASS" if sys.version_info >= (3, 11) else "FAIL",
        "python",
        sys.version.split()[0],
        "Install Python 3.11 or newer.",
    )
    try:
        playwright_version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError:
        playwright_version = None
    result(
        "PASS" if playwright_version else "FAIL",
        "playwright package",
        playwright_version or "missing",
        'Run: python -m pip install -e ".[browser]"',
    )
    chromium_ready, chromium_message = _chromium_available()
    result(
        "PASS" if chromium_ready else "FAIL",
        "chromium",
        chromium_message,
        None if chromium_ready else "Run: python -m playwright install chromium",
    )

    runtime = settings.database_path.parent
    try:
        details = runtime.lstat()
        runtime_ok = stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode)
        private = os.name == "nt" or stat.S_IMODE(details.st_mode) == 0o700
        runtime_message = (
            f"protected directory at {runtime}"
            if runtime_ok and private
            else f"unsafe at {runtime}"
        )
        result(
            "PASS" if runtime_ok and private else "FAIL",
            "runtime path",
            runtime_message,
            "Run: xworkbench setup",
        )
    except FileNotFoundError:
        result("FAIL", "runtime path", f"missing at {runtime}", "Run: xworkbench setup")
    except OSError as exc:
        result("FAIL", "runtime path", str(exc), "Check the configured path.")

    database_exists = settings.database_path.exists()
    ready, message = _database_ready(settings.database_path)
    result(
        "PASS" if ready and database_exists else "WARN" if ready else "FAIL",
        "database",
        message,
        None if ready and database_exists else "Run: xworkbench setup",
    )

    browser_status = PlaywrightBrowserProvider(settings).connection_status()
    browser_state = str(browser_status.get("status", "unavailable"))
    local_valid = bool(
        browser_status.get(
            "localStateValid",
            browser_state in {"ready", "verified_live", "present_unverified", "expired"},
        )
    )
    local_message = {
        "missing": "missing",
        "invalid_local_state": "invalid local Playwright JSON or permissions",
    }.get(browser_state, "valid local Playwright JSON (contents hidden)")
    auth_level = "PASS" if local_valid else "WARN" if browser_state == "missing" else "FAIL"
    result(
        auth_level,
        "local auth state",
        local_message,
        None if local_valid else "Run: xworkbench auth",
    )
    verified_at = browser_status.get("verifiedAt")
    if isinstance(verified_at, str):
        detail = (
            f"verified live at {verified_at}"
            if browser_state == "verified_live"
            else f"last verified live at {verified_at}; current status is {browser_state}"
        )
        result(
            "PASS" if browser_state == "verified_live" else "WARN",
            "last live verification",
            detail,
            None if browser_state == "verified_live" else "Run: xworkbench auth",
        )
    else:
        verification_message = {
            "ready": "legacy verification marker has no timestamp",
            "present_unverified": "local state has not been live-verified",
            "expired": "last live check found an expired session",
            "manual_action_required": "last live check requires manual action",
            "unavailable": "last live check was unavailable",
            "invalid_local_state": "cannot verify invalid local state",
            "missing": "never live-verified",
        }.get(browser_state, "no valid live-verification status")
        result(
            "WARN",
            "last live verification",
            verification_message,
            "Run: xworkbench auth",
        )

    try:
        token = settings.bearer_token()
    except SettingsError as exc:
        result("FAIL", "official token", str(exc), "Run: xworkbench configure")
    else:
        result(
            "PASS" if token else "FAIL" if require_token else "WARN",
            "official token",
            "configured"
            if token
            else "Official X API token missing; browser capture remains available",
            None if token else "Run: xworkbench configure",
        )

    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        message = "free loopback port available" if port == 0 else f"port {port} is free"
        result("PASS", "loopback port", message)
    except OSError:
        result("FAIL", "loopback port", f"port {port} unavailable", "Use: --port 0")

    if failures:
        print(f"NOT READY ({len(failures)} failed, {len(warnings)} warning(s))")
    elif warnings:
        print(f"READY WITH WARNINGS ({len(warnings)})")
    else:
        print("READY")
    return 0 if not failures else EXIT_PRECONDITION


def _setup(settings: Settings) -> int:
    settings.ensure_runtime_dirs()
    settings.ensure_config_file()
    settings.validate_local_files()
    print(f"PASS  config             protected at {settings.config_path}")

    lock_path = Path(__file__).resolve().parent.parent / "requirements.lock"
    if lock_path.is_file() and lock_path.stat().st_size:
        print(f"PASS  dependency lock    found at {lock_path}")
    else:
        print("WARN  dependency lock    not present in this installation")
        print(
            "      Fix: From a checkout, install with: "
            "python -m pip install -r requirements.lock"
        )

    storage = Storage(settings.database_path)
    storage.initialize()
    try:
        with sqlite3.connect(settings.database_path, timeout=0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Database is locked at {settings.database_path}; stop the other writer and retry."
        ) from exc
    settings.database_path.chmod(0o600)
    print(f"PASS  database           initialized at {settings.database_path}")

    print("INFO  setup never signs in or contacts X; use xworkbench auth when ready.")
    return _doctor(settings, require_token=False, port=0)


def _config(settings: Settings, command: str) -> int:
    if command == "show":
        print(json.dumps(settings.public_dict(), indent=2, sort_keys=True))
        return 0
    settings.validate_local_files()
    settings.bearer_token()
    browser_status = PlaywrightBrowserProvider(settings).connection_status()
    if browser_status.get("status") == "invalid_local_state":
        raise SettingsError("Browser auth state is invalid. Run: xworkbench auth")
    print("Configuration is valid. Secret values were not read aloud.")
    return 0


def _seed_offline_demo(storage: Storage) -> str:
    storage.initialize()
    request = CollectionRequest.from_dict(
        {
            "provider": BROWSER_PROVIDER,
            "sourceType": "home",
            "maxPosts": 3,
        }
    )
    now = datetime.now(UTC).replace(microsecond=0)
    plan = {
        "provider": BROWSER_PROVIDER,
        "providerVersion": PlaywrightBrowserProvider.provider_version,
        "sourceKind": "home",
        "sourceUrl": "offline://synthetic-home",
        "targetPosts": 3,
        "preparedAt": now.isoformat(),
        "browserHeadless": True,
        "jobTimeoutSeconds": 0,
        "pageTimeoutMs": 0,
        "noProgressLimit": 0,
    }
    posts = [
        Post(
            "demo-1",
            "[DEMO DATA] A human-approved feed capture becomes a durable local snapshot.",
            "demo_analyst",
            "https://example.invalid/demo-1",
            (now - timedelta(minutes=12)).isoformat(),
            like_count=42,
            reply_count=5,
            repost_count=9,
        ),
        Post(
            "demo-2",
            "[DEMO DATA] Agents inspect completed evidence through a read-only MCP surface.",
            "demo_researcher",
            "https://example.invalid/demo-2",
            (now - timedelta(minutes=8)).isoformat(),
            quote_count=3,
            bookmark_count=7,
        ),
        Post(
            "demo-3",
            "[DEMO DATA] Partial snapshots remain useful without another X request.",
            "demo_operator",
            "https://example.invalid/demo-3",
            (now - timedelta(minutes=4)).isoformat(),
            in_reply_to_post_id="demo-1",
            is_reply=True,
            has_media=True,
        ),
    ]
    job_id = storage.create_job(request, plan)
    storage.claim_job(job_id)
    storage.add_posts(
        job_id,
        posts,
        None,
        {
            "browserVersion": "offline-demo",
            "sourceKind": "home",
            "sourceUrl": "offline://synthetic-home",
            "scanIterations": 1,
            "scrollIterations": 0,
            "observedAt": now.isoformat(),
        },
    )
    storage.finish_job(
        job_id,
        ["Synthetic offline demo; no X request was made."],
        completion_reason="offline_demo_seeded",
    )
    return job_id


def _is_loopback(host: str) -> bool:
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _run_server(app, host: str, port: int, *, open_browser: bool) -> int:
    try:
        server = make_server(host, port, app, threaded=True)
    except OSError:
        print(f"Cannot bind {host}:{port}; choose another port or use --port 0.", file=sys.stderr)
        app.extensions["xworkbench_jobs"].shutdown()
        return EXIT_PRECONDITION
    display_host = f"[{host}]" if ":" in host else host
    url = f"http://{display_host}:{server.server_port}"
    print(f"X-Scraper running at {url} (Ctrl+C to stop)")
    if open_browser:
        timer = threading.Timer(0.4, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping X-Scraper.")
    finally:
        server.server_close()
        app.extensions["xworkbench_jobs"].shutdown()
    return 0


def _run_demo(*, port: int, open_browser: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="xworkbench-demo-") as temporary:
        root = Path(temporary)
        demo_settings = Settings(root / "demo.db", root / "no-token", allow_environment_token=False)
        storage = Storage(demo_settings.database_path)
        _seed_offline_demo(storage)
        print("OFFLINE DEMO: synthetic data only; no X requests.")
        app = create_app(
            demo_settings,
            storage=storage,
            collection_enabled=False,
        )
        return _run_server(app, "127.0.0.1", port, open_browser=open_browser)


def _run_live_smoke(settings: Settings, *, confirmed: bool) -> int:
    if not confirmed:
        print("Refusing live X access without --confirm-live-x.", file=sys.stderr)
        return EXIT_PRECONDITION
    provider = PlaywrightBrowserProvider(settings)
    status = provider.connection_status()
    if status.get("status") != "verified_live":
        print(
            "Live smoke precondition failed: browser session "
            f"{status.get('status', 'unavailable')}. "
            "Run: xworkbench auth",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION

    job: dict = {}
    with tempfile.TemporaryDirectory(prefix="xworkbench-live-smoke-") as temporary:
        root = Path(temporary)
        smoke_settings = Settings(
            root / "smoke.db",
            root / "no-token",
            allow_environment_token=False,
            storage_state_path=settings.storage_state_path,
            config_path=settings.config_path,
            browser_headless=False,
            job_timeout_seconds=settings.job_timeout_seconds,
            page_timeout_ms=settings.page_timeout_ms,
            no_progress_limit=settings.no_progress_limit,
        )
        storage = Storage(smoke_settings.database_path)
        storage.initialize()
        registry = ProviderRegistry([PlaywrightBrowserProvider(smoke_settings)])
        request = CollectionRequest.from_dict(
            {"provider": BROWSER_PROVIDER, "sourceType": "home", "maxPosts": 2}
        )
        plan = registry.prepare(request)
        service = JobService(storage, registry, start_worker=False)
        try:
            job_id = service.submit(request, plan)
            service.run_once(job_id)
            job = storage.get_job(job_id) or {}
        finally:
            service.shutdown()

    report = {
        "status": job.get("status", "failed"),
        "storedPosts": job.get("collected_count", 0),
        "completionReason": job.get("completion_reason"),
        "warnings": job.get("warnings") or [],
        "error": (
            {"code": job.get("error_code"), "message": job.get("error_message")}
            if job.get("error_code")
            else None
        ),
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "succeeded" and report["storedPosts"] > 0 else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        if args.command == "setup":
            return _setup(settings)
        if args.command == "configure":
            return _configure(settings)
        if args.command == "auth":
            settings.ensure_runtime_dirs()
            path = authenticate_interactively(settings)
            print(f"Saved protected browser session to {path}")
            return 0
        if args.command == "doctor":
            return _doctor(settings, require_token=args.require_token, port=args.port)
        if args.command == "config":
            return _config(settings, args.config_command)
        if args.command == "demo":
            return _run_demo(port=args.port, open_browser=not args.no_open)
        if args.command == "mcp":
            from .mcp_server import run_mcp

            run_mcp(
                args.url,
                database_path=None if args.url else settings.database_path,
            )
            return 0
        if args.command == "live-smoke":
            return _run_live_smoke(settings, confirmed=args.confirm_live_x)
        if not _is_loopback(args.host):
            raise SystemExit("Refusing a non-loopback host. This workbench is local-only.")
        return _run_server(
            create_app(settings),
            args.host,
            args.port,
            open_browser=not args.no_open,
        )
    except SettingsError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except CollectionError as exc:
        print(f"BROWSER ERROR [{exc.code}]: {exc}", file=sys.stderr)
        return EXIT_BROWSER
    except (OSError, RuntimeError) as exc:
        print(f"PRECONDITION ERROR: {exc}", file=sys.stderr)
        return EXIT_PRECONDITION
