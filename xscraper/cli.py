from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import sys
import time

from .api import create_app
from .config import Settings
from .errors import InvalidRequestError
from .jobs import JobService
from .models import CollectionRequest, normalize_profile
from .providers.playwright import PlaywrightProvider, authenticate_interactively
from .smoke import (
    EXIT_PRECONDITION,
    SmokePreconditionError,
    SmokeRunError,
    run_graphql_smoke,
)
from .storage import Storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local authenticated X collection workbench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("auth", help="Open a browser and save a local X session")
    serve = subparsers.add_parser("serve", help="Run the dashboard and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)
    collect = subparsers.add_parser("collect", help="Run one collection and print its job record")
    collect.add_argument("source_type", choices=("profile", "search"))
    collect.add_argument("source_value")
    collect.add_argument("--max-tweets", type=int, default=25)
    collect.add_argument("--start-date")
    collect.add_argument("--end-date")
    collect.add_argument("--include-replies", action="store_true")
    collect.add_argument("--media-only", action="store_true")
    collect.add_argument("--sentiment", action="store_true")
    smoke = subparsers.add_parser("smoke", help="Run an explicit live smoke gate")
    smoke_commands = smoke.add_subparsers(dest="smoke_command", required=True)
    graphql = smoke_commands.add_parser("graphql", help="Validate X GraphQL collection")
    graphql.add_argument("--profile", required=True)
    graphql.add_argument("--confirm-live-x", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.command == "smoke":
        if not args.confirm_live_x:
            print("Refusing live X requests without --confirm-live-x.", file=sys.stderr)
            return EXIT_PRECONDITION
        try:
            profile = normalize_profile(args.profile)
            settings.ensure_runtime_dirs()
            report = run_graphql_smoke(settings, profile)
        except (InvalidRequestError, SmokePreconditionError) as exc:
            print(f"Smoke precondition failed: {exc}", file=sys.stderr)
            return EXIT_PRECONDITION
        except SmokeRunError as exc:
            print(f"GraphQL smoke failed: {exc}", file=sys.stderr)
            if exc.report_path:
                print(f"Sanitized report: {exc.report_path}", file=sys.stderr)
            return exc.exit_code
        print(f"GraphQL smoke passed: {report}")
        return 0

    settings.ensure_runtime_dirs()
    if args.command == "auth":
        path = authenticate_interactively(settings)
        print(f"Saved browser session to {path}")
        return 0
    if args.command == "serve":
        try:
            loopback = args.host == "localhost" or ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise SystemExit(
                "Refusing a non-loopback host. This MVP is intentionally local-only."
            )
        app = create_app(settings)
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
        return 0

    storage = Storage(settings.database_path)
    storage.initialize()
    provider = PlaywrightProvider(settings)
    service = JobService(storage, provider)
    body = {
        "sourceType": args.source_type,
        "sourceValue": args.source_value,
        "maxTweets": args.max_tweets,
        "startDate": args.start_date,
        "endDate": args.end_date,
        "includeReplies": args.include_replies,
        "mediaOnly": args.media_only,
        "analyzeSentiment": args.sentiment,
    }
    job_id = service.submit(CollectionRequest.from_dict(body))
    while True:
        job = storage.get_job(job_id)
        assert job is not None
        print(
            f"\r{job['status']}: {job['collected_count']}/{job['target_count']}", end="", flush=True
        )
        if job["status"] in {"succeeded", "failed", "cancelled", "partial", "interrupted"}:
            print()
            print(json.dumps(job, indent=2))
            break
        time.sleep(1)
    return 0
