from __future__ import annotations

import csv
import io
import ipaddress
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from .config import Settings
from .errors import InvalidRequestError
from .jobs import JobService
from .models import CollectionRequest
from .storage import Storage
from .x_api import XApiProvider, compile_request, validate_compiled_request

EXPORT_FIELDS = [
    "tweet_id",
    "text",
    "author_username",
    "url",
    "created_at",
    "scraped_at",
    "language",
    "conversation_id",
    "in_reply_to_tweet_id",
    "like_count",
    "reply_count",
    "retweet_count",
    "quote_count",
    "bookmark_count",
    "is_reply",
    "is_retweet",
    "is_quote",
    "has_media",
    "media",
]


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    compiled = job["compiled_request"]
    return {
        "id": job["id"],
        "request": job["request"],
        "status": job["status"],
        "targetCount": job["request"]["maxPosts"],
        "collectedCount": job["collected_count"],
        "readCount": job.get("billable_read_count", 0),
        "warnings": job["warnings"],
        "error": (
            {
                "code": job["error_code"],
                "message": job["error_message"],
                "retryable": job["error_retryable"],
            }
            if job["error_code"]
            else None
        ),
        "cancelRequested": job["cancel_requested"],
        "completionReason": job.get("completion_reason"),
        "retryAt": job.get("retry_at"),
        "rateLimit": {
            "remaining": job.get("rate_limit_remaining"),
            "reset": job.get("rate_limit_reset"),
        },
        "cost": {
            "maximumPostReads": compiled["maxBillableReads"],
            "estimatedPostReadUsd": compiled["estimatedPostReadUsd"],
            "actualPostReads": job.get("billable_read_count", 0),
            "pricingAsOf": compiled["pricingAsOf"],
            "estimateScope": "posts_only",
        },
        "isPartial": job["status"] == "partial"
        or (job["collected_count"] > 0 and job["status"] in {"failed", "cancelled", "interrupted"}),
        "createdAt": job["created_at"],
        "startedAt": job["started_at"],
        "finishedAt": job["finished_at"],
        "updatedAt": job["updated_at"],
    }


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _request_body(body: Any) -> CollectionRequest:
    if not isinstance(body, dict):
        raise InvalidRequestError("Request body must be a JSON object.")
    controls = {"confirmPaidRead", "forceRefresh", "compiledRequest"}
    return CollectionRequest.from_dict(
        {key: value for key, value in body.items() if key not in controls}
    )


def create_app(
    settings: Settings | None = None,
    *,
    storage: Storage | None = None,
    provider: XApiProvider | None = None,
    start_worker: bool = True,
    demo_mode: str | None = None,
) -> Flask:
    if demo_mode not in {None, "offline", "live"}:
        raise ValueError("demo_mode must be 'offline', 'live', or None.")
    settings = settings or Settings.from_env()
    settings.ensure_runtime_dirs()
    storage = storage or Storage(settings.database_path)
    storage.initialize()
    provider = provider or XApiProvider(settings)
    jobs = JobService(storage, provider, start_worker=start_worker)

    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.extensions["xscraper_jobs"] = jobs

    @app.before_request
    def require_local_host():
        hostname = urlsplit(f"//{request.host}").hostname
        allowed = hostname == "localhost"
        if hostname and not allowed:
            try:
                allowed = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                allowed = False
        if not allowed:
            return _error("local_only", "Only loopback hosts are accepted.", 403)

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_error):
        return _error("request_too_large", "Request body is too large.", 413)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/connection")
    def connection():
        if demo_mode == "offline":
            return jsonify(
                {
                    "status": "offline_demo",
                    "valid": False,
                    "source": "none",
                    "message": "Offline demo data; no X API calls are available.",
                    "demoMode": "offline",
                }
            )
        return jsonify({**settings.connection_status(), "demoMode": demo_mode})

    @app.post("/api/collections/preview")
    def preview_collection():
        if demo_mode == "offline":
            return _error(
                "offline_demo_read_disabled",
                "Offline demo mode cannot make X API reads.",
                409,
            )
        try:
            collection_request = _request_body(request.get_json(silent=True))
            if demo_mode == "live" and collection_request.max_posts != 10:
                return _error(
                    "demo_post_limit",
                    "Live demo collections are limited to exactly 10 Posts.",
                    400,
                )
            token = settings.bearer_token()
            if not token:
                return _error("connection_missing", "Run: xscraper configure", 409)
            compiled = compile_request(collection_request, token)
            cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
            cached = storage.find_cached_job(
                compiled["requestFingerprint"], compiled["accountScope"], cutoff=cutoff
            )
        except InvalidRequestError as exc:
            return _error(exc.code, str(exc), 400)
        return jsonify(
            {
                "request": collection_request.to_dict(),
                "compiledRequest": compiled,
                "compiledIntent": {
                    "endpoint": compiled["endpoint"],
                    "query": compiled["query"],
                    "startTime": compiled["startTime"],
                    "endTime": compiled["endTime"],
                    "sortOrder": compiled["sortOrder"],
                },
                "maximumPostReads": compiled["maxBillableReads"],
                "estimatedPostReadUsd": compiled["estimatedPostReadUsd"],
                "pricingAsOf": compiled["pricingAsOf"],
                "pricingUrl": "https://docs.x.com/x-api/getting-started/pricing",
                "billingWarning": (
                    "Post-read estimate only. Author and media expansions may be billed "
                    "separately; actual billing and deduplication may differ. Use the X "
                    "Developer Console spending limit as the hard cap."
                ),
                "estimateScope": "posts_only",
                "cacheAvailable": bool(cached),
                "cachedJobId": cached["id"] if cached else None,
            }
        )

    @app.post("/api/jobs")
    def create_job():
        if demo_mode == "offline":
            return _error(
                "offline_demo_read_disabled",
                "Offline demo mode cannot make X API reads.",
                409,
            )
        if not request.is_json:
            return _error("invalid_request", "Content-Type must be application/json.", 415)
        body = request.get_json(silent=True)
        try:
            collection_request = _request_body(body)
            if not isinstance(body, dict):
                raise InvalidRequestError("Request body must be a JSON object.")
            for name in ("confirmPaidRead", "forceRefresh"):
                if name in body and not isinstance(body[name], bool):
                    raise InvalidRequestError(f"{name} must be a boolean.")
            if demo_mode == "live" and collection_request.max_posts != 10:
                return _error(
                    "demo_post_limit",
                    "Live demo collections are limited to exactly 10 Posts.",
                    400,
                )
            if demo_mode == "live" and body.get("forceRefresh", False):
                return _error(
                    "demo_force_refresh_disabled",
                    "Force refresh is disabled in live demo mode.",
                    400,
                )
            token = settings.bearer_token()
            if not token:
                return _error("connection_missing", "Run: xscraper configure", 409)
            compiled = body.get("compiledRequest")
            validate_compiled_request(collection_request, compiled, token)
            cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
            cached = (
                None
                if body.get("forceRefresh", False)
                else storage.find_cached_job(
                    compiled["requestFingerprint"], compiled["accountScope"], cutoff=cutoff
                )
            )
            if cached:
                return jsonify(
                    {"jobId": cached["id"], "status": cached["status"], "cacheHit": True}
                )
            if body.get("confirmPaidRead") is not True:
                raise InvalidRequestError("confirmPaidRead must be true for a paid X API read.")
        except InvalidRequestError as exc:
            return _error(exc.code, str(exc), 400)
        job_id = jobs.submit(collection_request, compiled)
        return jsonify({"jobId": job_id, "status": "queued", "cacheHit": False}), 202

    @app.get("/api/jobs")
    def list_jobs():
        try:
            limit = min(max(int(request.args.get("limit", "25")), 1), 100)
        except ValueError:
            return _error("invalid_request", "limit must be an integer.", 400)
        return jsonify({"jobs": [_public_job(job) for job in storage.list_jobs(limit)]})

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        job = storage.get_job(job_id)
        if not job:
            return _error("not_found", "Job not found.", 404)
        return jsonify(_public_job(job))

    @app.get("/api/jobs/<job_id>/posts")
    def get_posts(job_id: str):
        if not storage.get_job(job_id):
            return _error("not_found", "Job not found.", 404)
        try:
            limit = min(max(int(request.args.get("limit", "50")), 1), 100)
            offset = max(int(request.args.get("offset", "0")), 0)
        except ValueError:
            return _error("invalid_request", "Pagination must use integers.", 400)
        posts = storage.get_job_posts(job_id, limit=limit, offset=offset)
        total = storage.count_job_posts(job_id)
        next_offset = offset + len(posts) if offset + len(posts) < total else None
        return jsonify(
            {
                "posts": posts,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "count": len(posts),
                    "total": total,
                    "nextOffset": next_offset,
                },
            }
        )

    @app.delete("/api/jobs/<job_id>")
    def cancel_job(job_id: str):
        if not storage.get_job(job_id):
            return _error("not_found", "Job not found.", 404)
        if not jobs.cancel(job_id):
            return _error("invalid_state", "Job cannot be cancelled.", 409)
        return jsonify({"status": "cancelling"}), 202

    @app.post("/api/jobs/<job_id>/resume")
    def resume_job(job_id: str):
        job = storage.get_job(job_id)
        if not job:
            return _error("not_found", "Job not found.", 404)
        if not jobs.resume(job_id):
            return _error("invalid_state", "Job cannot be resumed.", 409)
        return jsonify({"status": "queued"}), 202

    @app.get("/api/jobs/<job_id>/export")
    def export_job(job_id: str):
        job = storage.get_job(job_id)
        if not job:
            return _error("not_found", "Job not found.", 404)
        rows = storage.get_job_posts(job_id, limit=500)
        export_format = request.args.get("format", "json").lower()
        filename = f"x-collection-{job_id[:8]}-{job['status']}"
        headers = {
            "X-Collection-Status": job["status"],
            "X-Completion-Reason": job.get("completion_reason") or "",
            "X-Result-Count": str(len(rows)),
            "X-Snapshot-At": job["updated_at"],
        }
        if export_format == "json":
            return Response(
                json.dumps(
                    {"schemaVersion": 2, "job": _public_job(job), "posts": rows},
                    indent=2,
                    ensure_ascii=False,
                ),
                mimetype="application/json",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}.json"',
                    **headers,
                },
            )
        if export_format == "csv":
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                row = dict(row)
                row["media"] = json.dumps(row.get("media", []), ensure_ascii=False)
                for field, value in row.items():
                    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                        row[field] = f"'{value}"
                writer.writerow(row)
            return Response(
                stream.getvalue(),
                mimetype="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}.csv"',
                    **headers,
                },
            )
        return _error("invalid_request", "format must be json or csv.", 400)

    return app
