from __future__ import annotations

import argparse
import getpass
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

from .api import create_app
from .config import Settings
from .errors import CollectionError, InvalidRequestError
from .models import CollectionRequest, Post, normalize_profile
from .storage import SCHEMA_FAMILY, SCHEMA_VERSION, Storage
from .x_api import XApiProvider, compile_request

EXIT_PRECONDITION = 2
MAX_SMOKE_POST_READS = 10
SMOKE_POST_READ_ESTIMATE_USD = 0.05


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local official-X-API analyst workbench")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure", help="Save an X API Bearer Token locally")

    doctor = commands.add_parser("doctor", help="Check demo prerequisites without paid reads")
    doctor.add_argument("--live", action="store_true", help="Require a configured X token")
    doctor.add_argument("--port", type=_port, default=0, help="Check a port; 0 means any free port")

    serve = commands.add_parser("serve", help="Run the loopback dashboard and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=_port, default=5000)
    serve.add_argument("--no-open", action="store_true", help="Do not open the dashboard")

    demo = commands.add_parser("demo", help="Run an isolated, guarded demo")
    demo.add_argument("--live", action="store_true", help="Enable guarded official X API reads")
    demo.add_argument("--port", type=_port, default=0, help="Use 0 to select a free port")
    demo.add_argument("--no-open", action="store_true", help="Do not open the dashboard")

    mcp = commands.add_parser("mcp", help="Run the optional MCP-to-REST bridge over stdio")
    mcp.add_argument("--url", default="http://127.0.0.1:5000")

    smoke = commands.add_parser("smoke", help="Run an opt-in paid API smoke check")
    smoke_commands = smoke.add_subparsers(dest="smoke_command", required=True)
    api = smoke_commands.add_parser("api", help="Read at most 10 Posts through recent search")
    api.add_argument("--profile", required=True)
    api.add_argument("--confirm-paid-x", action="store_true")
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


def _run_api_smoke(settings: Settings, profile: str) -> dict[str, object]:
    token = settings.bearer_token()
    if not token:
        raise RuntimeError("No X API Bearer Token. Run: xscraper configure")
    collection = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": profile, "maxPosts": MAX_SMOKE_POST_READS}
    )
    posts = []

    def save(batch, _cursor, _stats):
        posts.extend(batch)
        return len(batch)

    summary = XApiProvider(settings).collect(
        collection,
        compiled_request=compile_request(collection, token),
        cursor=None,
        collected_count=0,
        on_batch=save,
        should_cancel=lambda: False,
    )
    return {
        "status": "passed",
        "posts": len(posts),
        "maximumPostReads": MAX_SMOKE_POST_READS,
        "estimatedPostReadUsd": SMOKE_POST_READ_ESTIMATE_USD,
        "estimateScope": "posts_only",
        "completionReason": summary.completion_reason,
    }


def _database_ready(path: Path) -> tuple[bool, str]:
    if not path.exists() or path.stat().st_size == 0:
        return True, f"new database will be created at {path}"
    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))
    except sqlite3.DatabaseError:
        return False, f"database at {path} is unreadable or incompatible"
    valid = (
        metadata.get("schema_family") == SCHEMA_FAMILY
        and metadata.get("schema_version") == SCHEMA_VERSION
    )
    return valid, (f"database ready at {path}" if valid else f"database at {path} is incompatible")


def _doctor(settings: Settings, *, live: bool, port: int) -> int:
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

    if live:
        token = settings.bearer_token()
        result(bool(token), "Bearer Token configured" if token else "run: xscraper configure")
        if token and not os.environ.get("XSCRAPER_X_BEARER_TOKEN", "").strip():
            try:
                mode = stat.S_IMODE(settings.bearer_token_path.stat().st_mode)
                result(mode == 0o600, "token file permissions are 0600")
            except OSError as exc:
                result(False, f"cannot inspect token file permissions: {exc}")

    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        result(True, "a free loopback port is available" if port == 0 else f"port {port} is free")
    except OSError:
        result(False, f"port {port} is unavailable; use --port 0")

    if live:
        print(
            "WARN  Token validity and credits are not checked without a paid read. "
            "Verify credits and a spending limit in the X Developer Console."
        )
    print("READY" if not failures else f"NOT READY ({len(failures)} failed check(s))")
    return 0 if not failures else EXIT_PRECONDITION


def _seed_offline_demo(storage: Storage) -> str:
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "search", "sourceValue": "OFFLINE DEMO DATA", "maxPosts": 10}
    )
    compiled = compile_request(request, "offline-demo-token")
    compiled.update(
        provider="offline_demo",
        maxBillableReads=0,
        estimatedPostReadUsd=0.0,
        pricePerPostUsd=0.0,
        pricingAsOf="not applicable",
    )
    now = datetime.now(UTC).replace(microsecond=0)
    posts = [
        Post(
            "demo-1",
            "[DEMO DATA] Teams can preview a bounded X collection before approving a read.",
            "demo_analyst",
            "https://example.invalid/demo-1",
            (now - timedelta(minutes=12)).isoformat(),
            like_count=42,
            reply_count=5,
            repost_count=9,
        ),
        Post(
            "demo-2",
            "[DEMO DATA] Every result is stored as an immutable observation for later export.",
            "demo_researcher",
            "https://example.invalid/demo-2",
            (now - timedelta(minutes=8)).isoformat(),
            quote_count=3,
            bookmark_count=7,
        ),
        Post(
            "demo-3",
            "[DEMO DATA] Rate-limit waits, cancellation, and completed pages survive restarts.",
            "demo_operator",
            "https://example.invalid/demo-3",
            (now - timedelta(minutes=4)).isoformat(),
            in_reply_to_post_id="demo-1",
            is_reply=True,
            has_media=True,
        ),
    ]
    job_id = storage.create_job(request, compiled)
    storage.claim_job(job_id)
    storage.add_posts(job_id, posts, None, {"billableReads": 0})
    storage.finish_job(
        job_id,
        ["Synthetic offline demo; no X API request was made."],
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
        app.extensions["xscraper_jobs"].shutdown()
        return EXIT_PRECONDITION
    display_host = f"[{host}]" if ":" in host else host
    url = f"http://{display_host}:{server.server_port}"
    print(f"X API Analyst running at {url} (Ctrl+C to stop)")
    if open_browser:
        timer = threading.Timer(0.4, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping X API Analyst.")
    finally:
        server.server_close()
        app.extensions["xscraper_jobs"].shutdown()
    return 0


def _run_demo(settings: Settings, *, live: bool, port: int, open_browser: bool) -> int:
    with tempfile.TemporaryDirectory(prefix="xscraper-demo-") as temporary:
        root = Path(temporary)
        demo_settings = Settings(
            root / "demo.db",
            settings.bearer_token_path if live else root / "no-token",
        )
        if live and _doctor(demo_settings, live=True, port=port) != 0:
            return EXIT_PRECONDITION
        storage = Storage(demo_settings.database_path)
        if not live:
            _seed_offline_demo(storage)
        mode = "live" if live else "offline"
        print(
            "LIVE DEMO: one 10-Post page, no force refresh."
            if live
            else "OFFLINE DEMO: synthetic data only; no X API calls."
        )
        app = create_app(demo_settings, storage=storage, demo_mode=mode)
        return _run_server(app, "127.0.0.1", port, open_browser=open_browser)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.command == "configure":
        return _configure(settings)
    if args.command == "doctor":
        return _doctor(settings, live=args.live, port=args.port)
    if args.command == "mcp":
        from .mcp_server import run_mcp

        run_mcp(args.url)
        return 0
    if args.command == "smoke":
        if not args.confirm_paid_x:
            print(
                "Refusing paid X reads without --confirm-paid-x "
                f"(10-Post read estimate ${SMOKE_POST_READ_ESTIMATE_USD:.2f}; "
                "expansions may be billed separately).",
                file=sys.stderr,
            )
            return EXIT_PRECONDITION
        try:
            report = _run_api_smoke(settings, normalize_profile(args.profile))
        except (InvalidRequestError, CollectionError, RuntimeError) as exc:
            print(f"API smoke failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, indent=2))
        return 0
    if args.command == "demo":
        return _run_demo(settings, live=args.live, port=args.port, open_browser=not args.no_open)
    if not _is_loopback(args.host):
        raise SystemExit("Refusing a non-loopback host. This workbench is local-only.")
    return _run_server(
        create_app(settings),
        args.host,
        args.port,
        open_browser=not args.no_open,
    )
