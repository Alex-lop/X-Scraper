from __future__ import annotations

import argparse
import json
import logging
import time

from .api import create_app
from .config import Settings
from .jobs import JobService
from .models import CollectionRequest
from .providers.playwright import PlaywrightProvider, authenticate_interactively
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
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    settings = Settings.from_env()
    settings.ensure_runtime_dirs()
    if args.command == "auth":
        path = authenticate_interactively(settings)
        print(f"Saved browser session to {path}")
        return
    if args.command == "serve":
        app = create_app(settings)
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
        return

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
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            print()
            print(json.dumps(job, indent=2))
            break
        time.sleep(1)
