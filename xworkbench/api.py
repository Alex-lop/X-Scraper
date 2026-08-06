from __future__ import annotations

import csv
import io
import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from .config import Settings
from .errors import InvalidRequestError
from .jobs import JobService
from .models import CollectionRequest
from .storage import Storage
from .x_api import (
    UNIT_PRICES_USD,
    XApiProvider,
    compile_request,
    returned_list_price,
    validate_compiled_request,
)

EXPORT_FIELDS = [
    "schema_version",
    "collection_id",
    "collection_status",
    "completion_reason",
    "warnings",
    "provider",
    "provider_version",
    "compiler_version",
    "search_mode",
    "endpoint",
    "effective_query",
    "start_time",
    "end_time",
    "post_resources_returned",
    "user_resources_returned",
    "media_resources_returned",
    "post_id",
    "text",
    "author_id",
    "author_username",
    "url",
    "created_at",
    "observed_at",
    "language",
    "conversation_id",
    "in_reply_to_post_id",
    "like_count",
    "reply_count",
    "repost_count",
    "quote_count",
    "bookmark_count",
    "is_reply",
    "is_repost",
    "is_quote",
    "has_media",
    "media",
]


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    compiled = job["compiled_request"]
    unit_prices = compiled.get("unitPricesUsd", UNIT_PRICES_USD)
    resources = {
        "posts": job.get("post_resource_count", 0),
        "users": job.get("user_resource_count", 0),
        "media": job.get("media_resource_count", 0),
    }
    provenance_fields = (
        "provider",
        "providerVersion",
        "compilerVersion",
        "searchMode",
        "endpoint",
        "query",
        "queryLength",
        "startTime",
        "endTime",
        "sortOrder",
        "tweetFields",
        "expansions",
        "userFields",
        "mediaFields",
        "compiledAt",
    )
    return {
        "id": job["id"],
        "request": job["request"],
        "status": job["status"],
        "targetCount": job["request"]["maxPosts"],
        "collectedCount": job["collected_count"],
        "resourcesReturned": resources,
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
        "provenance": {name: compiled[name] for name in provenance_fields},
        "cost": {
            "basis": "list_price_pre_dedup",
            "unitPricesUsd": unit_prices,
            "returnedListPriceEstimateUsd": returned_list_price(resources, unit_prices),
            "maximumPostResources": compiled["maximumPostResources"],
            "maximumPostListPriceUsd": compiled["maximumPostListPriceUsd"],
            "pricingAsOf": compiled["pricingAsOf"],
            "note": (
                "List-price estimate before X daily resource deduplication; "
                "it is not an invoice total."
            ),
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
    controls = {"confirmPaidRead", "compiledRequest"}
    return CollectionRequest.from_dict(
        {key: value for key, value in body.items() if key not in controls}
    )


def create_app(
    settings: Settings | None = None,
    *,
    storage: Storage | None = None,
    provider: XApiProvider | None = None,
    start_worker: bool = True,
) -> Flask:
    settings = settings or Settings.from_env()
    settings.ensure_runtime_dirs()
    storage = storage or Storage(settings.database_path)
    storage.initialize()
    provider = provider or XApiProvider(settings)
    jobs = JobService(storage, provider, start_worker=start_worker)

    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.extensions["xworkbench_jobs"] = jobs

    @app.before_request
    def require_local_host_and_json_mutations():
        hostname = urlsplit(f"//{request.host}").hostname
        allowed = hostname == "localhost"
        if hostname and not allowed:
            try:
                allowed = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                allowed = False
        if not allowed:
            return _error("local_only", "Only loopback hosts are accepted.", 403)
        remote = request.remote_addr
        if remote:
            try:
                if not ipaddress.ip_address(remote).is_loopback:
                    return _error("local_only", "Only loopback clients are accepted.", 403)
            except ValueError:
                return _error("local_only", "Only loopback clients are accepted.", 403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not request.is_json:
            return _error(
                "json_required",
                "State-changing requests require Content-Type: application/json.",
                415,
            )

    @app.after_request
    def secure_local_response(response: Response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; connect-src 'self'; "
            "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "img-src 'self' data: https:; object-src 'none'; script-src 'self'; "
            "style-src 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_exception):
        return _error("request_too_large", "Request body is too large.", 413)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/connection")
    def connection():
        status = settings.connection_status()
        return jsonify(
            {
                "status": status["status"],
                "configured": bool(status["valid"]),
                "source": status["source"],
                "message": status["message"],
            }
        )

    @app.post("/api/collections/preview")
    def preview_collection():
        try:
            collection_request = _request_body(request.get_json(silent=True))
            token = settings.bearer_token()
            if not token:
                return _error("connection_missing", "Run: xworkbench configure", 409)
            compiled = compile_request(collection_request, token)
        except InvalidRequestError as exc:
            return _error(exc.code, str(exc), 400)
        return jsonify(
            {
                "request": collection_request.to_dict(),
                "compiledRequest": compiled,
                "compiledIntent": {
                    "searchMode": compiled["searchMode"],
                    "endpoint": compiled["endpoint"],
                    "query": compiled["query"],
                    "compiledLength": compiled["queryLength"],
                    "startTime": compiled["startTime"],
                    "endTime": compiled["endTime"],
                    "sortOrder": compiled["sortOrder"],
                    "expiresAt": compiled["expiresAt"],
                },
                "costEstimate": {
                    "basis": "list_price_pre_dedup",
                    "maximumPostResources": compiled["maximumPostResources"],
                    "maximumPostListPriceUsd": compiled["maximumPostListPriceUsd"],
                    "unitPricesUsd": UNIT_PRICES_USD,
                    "variableResources": ["users", "media"],
                    "pricingAsOf": compiled["pricingAsOf"],
                    "pricingUrl": "https://docs.x.com/x-api/getting-started/pricing",
                    "note": (
                        "User and media resources vary with the response. Estimates use "
                        "list prices before X daily resource deduplication and are not an "
                        "invoice total. Set the hard spending limit in the Developer Console."
                    ),
                },
            }
        )

    @app.post("/api/jobs")
    def create_job():
        if not request.is_json:
            return _error("invalid_request", "Content-Type must be application/json.", 415)
        body = request.get_json(silent=True)
        try:
            collection_request = _request_body(body)
            if not isinstance(body, dict):
                raise InvalidRequestError("Request body must be a JSON object.")
            if "confirmPaidRead" in body and not isinstance(body["confirmPaidRead"], bool):
                raise InvalidRequestError("confirmPaidRead must be a boolean.")
            token = settings.bearer_token()
            if not token:
                return _error("connection_missing", "Run: xworkbench configure", 409)
            compiled = body.get("compiledRequest")
            validate_compiled_request(collection_request, compiled, token)
            if body.get("confirmPaidRead") is not True:
                raise InvalidRequestError("confirmPaidRead must be true for a paid X API read.")
        except InvalidRequestError as exc:
            return _error(exc.code, str(exc), 400)
        job_id = jobs.submit(collection_request, compiled)
        return jsonify({"jobId": job_id, "status": "queued"}), 202

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

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        if not storage.get_job(job_id):
            return _error("not_found", "Job not found.", 404)
        if not jobs.cancel(job_id):
            return _error("invalid_state", "Job cannot be cancelled.", 409)
        return jsonify({"status": "cancelling"}), 202

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id: str):
        if not storage.get_job(job_id):
            return _error("not_found", "Job not found.", 404)
        if not storage.delete_job(job_id):
            return _error("invalid_state", "Only terminal jobs can be deleted.", 409)
        return Response(status=204)

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
            "X-Search-Mode": job["compiled_request"]["searchMode"],
        }
        if export_format == "json":
            return Response(
                json.dumps(
                    {"schemaVersion": 3, "job": _public_job(job), "posts": rows},
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
            public_job = _public_job(job)
            provenance = public_job["provenance"]
            resources = public_job["resourcesReturned"]
            for row in rows:
                row = {
                    "schema_version": 3,
                    "collection_id": job_id,
                    "collection_status": job["status"],
                    "completion_reason": job.get("completion_reason"),
                    "warnings": json.dumps(job["warnings"], ensure_ascii=False),
                    "provider": provenance["provider"],
                    "provider_version": provenance["providerVersion"],
                    "compiler_version": provenance["compilerVersion"],
                    "search_mode": provenance["searchMode"],
                    "endpoint": provenance["endpoint"],
                    "effective_query": provenance["query"],
                    "start_time": provenance["startTime"],
                    "end_time": provenance["endTime"],
                    "post_resources_returned": resources["posts"],
                    "user_resources_returned": resources["users"],
                    "media_resources_returned": resources["media"],
                    **row,
                }
                row["media"] = json.dumps(row.get("media", []), ensure_ascii=False)
                for field, value in row.items():
                    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
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
