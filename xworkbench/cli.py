from __future__ import annotations

import argparse
import getpass
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
from .config import Settings
from .jobs import JobService
from .models import CollectionRequest, Post
from .playwright_browser import PlaywrightBrowserProvider, authenticate_interactively
from .providers import ProviderRegistry
from .storage import SCHEMA_FAMILY, SCHEMA_VERSION, Storage

EXIT_PRECONDITION = 2


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local feed-to-context snapshot bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure", help="Save an optional X API Bearer Token locally")
    commands.add_parser("auth", help="Open headed Chromium for manual X sign-in")

    doctor = commands.add_parser("doctor", help="Check local browser and storage prerequisites")
    doctor.add_argument("--require-token", action="store_true", help="Require an official X token")
    doctor.add_argument("--port", type=_port, default=0, help="Check a port; 0 means any free port")

    serve = commands.add_parser("serve", help="Run the loopback dashboard and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=_port, default=5000)
    serve.add_argument("--no-open", action="store_true", help="Do not open the dashboard")

    demo = commands.add_parser("demo", help="Run an isolated synthetic-data demo")
    demo.add_argument("--port", type=_port, default=0, help="Use 0 to select a free port")
    demo.add_argument("--no-open", action="store_true", help="Do not open the dashboard")

    mcp = commands.add_parser("mcp", help="Expose terminal local snapshots over MCP stdio")
    mcp.add_argument("--url", default="http://127.0.0.1:5000")

    smoke = commands.add_parser("live-smoke", help="Capture at most two live Home-feed Posts")
    smoke.add_argument("--confirm-live-x", action="store_true")
    return parser


def _configure(settings: Settings) -> int:
    token = getpass.getpass("X API Bearer Token (input hidden): ").strip()
    if not token:
        print("Bearer Token was empty; nothing saved.", file=sys.stderr)
        return EXIT_PRECONDITION
    settings.ensure_runtime_dirs()
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
    if not path.exists() or path.stat().st_size == 0:
        return True, f"new database will be created at {path}"
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))
            version = metadata.get("schema_version")
            compatible = False
            if metadata.get("schema_family") == SCHEMA_FAMILY and version in {
                "1",
                SCHEMA_VERSION,
            }:
                checker = Storage(path)._schema_is_compatible
                try:
                    compatible = checker(connection, version=int(version))
                except TypeError:
                    compatible = checker(connection)
    except sqlite3.DatabaseError:
        return False, f"database at {path} is unreadable or incompatible"
    if compatible and version == "1" and SCHEMA_VERSION != "1":
        return True, f"database v1 ready for protected migration at {path}"
    return compatible, (
        f"database ready at {path}" if compatible else f"database at {path} is incompatible"
    )


def _chromium_available() -> tuple[bool, str]:
    if importlib.util.find_spec("playwright") is None:
        return False, 'Playwright is missing; install with: pip install -e ".[browser]"'
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            available = Path(playwright.chromium.executable_path).is_file()
    except Exception as exc:
        return False, f"Playwright Chromium check failed: {type(exc).__name__}"
    return (
        (True, "Playwright Chromium is installed")
        if available
        else (False, "Chromium is missing; run: playwright install chromium")
    )


def _doctor(settings: Settings, *, require_token: bool, port: int) -> int:
    failures = []

    def result(ok: bool, message: str) -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {message}")
        if not ok:
            failures.append(message)

    result(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}")
    try:
        settings.ensure_runtime_dirs()
        with tempfile.NamedTemporaryFile(dir=settings.database_path.parent):
            pass
        result(True, f"runtime directory writable at {settings.database_path.parent}")
    except OSError as exc:
        result(False, f"runtime directory is not writable: {exc}")

    ready, message = _database_ready(settings.database_path)
    result(ready, message)
    playwright_ready = importlib.util.find_spec("playwright") is not None
    result(
        playwright_ready,
        "Playwright is installed" if playwright_ready else "install .[browser]",
    )
    chromium_ready, chromium_message = _chromium_available()
    result(chromium_ready, chromium_message)

    browser_status = PlaywrightBrowserProvider(settings).connection_status()
    session_ready = browser_status.get("status") == "ready"
    result(session_ready, f"browser session {browser_status.get('status', 'unavailable')}")
    if session_ready and settings.storage_state_path is not None:
        try:
            mode = stat.S_IMODE(settings.storage_state_path.stat().st_mode)
            result(mode == 0o600, "browser auth state permissions are 0600")
        except OSError as exc:
            result(False, f"cannot inspect browser auth state permissions: {exc}")

    token = settings.bearer_token()
    if require_token:
        result(bool(token), "Bearer Token configured" if token else "run: xworkbench configure")
        if token and not os.environ.get("XWORKBENCH_X_BEARER_TOKEN", "").strip():
            try:
                mode = stat.S_IMODE(settings.bearer_token_path.stat().st_mode)
                result(mode == 0o600, "token file permissions are 0600")
            except OSError as exc:
                result(False, f"cannot inspect token file permissions: {exc}")
    elif not token:
        print("INFO  Official X API token missing; Browser capture remains available.")

    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        result(True, "a free loopback port is available" if port == 0 else f"port {port} is free")
    except OSError:
        result(False, f"port {port} is unavailable; use --port 0")

    print("READY" if not failures else f"NOT READY ({len(failures)} failed check(s))")
    return 0 if not failures else EXIT_PRECONDITION


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
    if status.get("status") != "ready":
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
    settings = Settings.from_env()
    if args.command == "configure":
        return _configure(settings)
    if args.command == "auth":
        settings.ensure_runtime_dirs()
        path = authenticate_interactively(settings)
        print(f"Saved protected browser session to {path}")
        return 0
    if args.command == "doctor":
        return _doctor(settings, require_token=args.require_token, port=args.port)
    if args.command == "demo":
        return _run_demo(port=args.port, open_browser=not args.no_open)
    if args.command == "mcp":
        from .mcp_server import run_mcp

        run_mcp(args.url)
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
