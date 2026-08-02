from __future__ import annotations

import csv
import io
import json
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory

from .config import PROJECT_ROOT, Settings
from .errors import InvalidRequestError
from .jobs import JobService
from .models import CollectionRequest
from .providers.playwright import PlaywrightProvider
from .storage import Storage

EXPORT_FIELDS = [
    "tweet_id",
    "text",
    "author_username",
    "url",
    "created_at",
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
    "sentiment_label",
    "sentiment_score",
]


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "request": job["request"],
        "status": job["status"],
        "targetCount": job["target_count"],
        "collectedCount": job["collected_count"],
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
        "createdAt": job["created_at"],
        "startedAt": job["started_at"],
        "finishedAt": job["finished_at"],
        "updatedAt": job["updated_at"],
    }


def create_app(
    settings: Settings | None = None,
    *,
    storage: Storage | None = None,
    provider: PlaywrightProvider | None = None,
    start_worker: bool = True,
) -> Flask:
    settings = settings or Settings.from_env()
    settings.ensure_runtime_dirs()
    storage = storage or Storage(settings.database_path)
    storage.initialize()
    provider = provider or PlaywrightProvider(settings)
    jobs = JobService(storage, provider, start_worker=start_worker)

    app = Flask(__name__, static_folder=str(PROJECT_ROOT), static_url_path="")
    app.extensions["xscraper_storage"] = storage
    app.extensions["xscraper_jobs"] = jobs
    app.extensions["xscraper_provider"] = provider

    @app.get("/")
    def index():
        return send_from_directory(PROJECT_ROOT, "index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/session")
    def session():
        return jsonify(provider.session_status())

    @app.post("/api/jobs")
    def create_job():
        try:
            collection_request = CollectionRequest.from_dict(request.get_json(silent=True) or {})
        except InvalidRequestError as exc:
            return jsonify({"error": {"code": exc.code, "message": str(exc)}}), 400
        job_id = jobs.submit(collection_request)
        return jsonify({"jobId": job_id, "status": "queued"}), 202

    @app.get("/api/jobs")
    def list_jobs():
        try:
            limit = min(max(int(request.args.get("limit", "25")), 1), 100)
        except ValueError:
            return jsonify(
                {"error": {"code": "invalid_request", "message": "limit must be an integer."}}
            ), 400
        return jsonify({"jobs": [_public_job(job) for job in storage.list_jobs(limit)]})

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        job = storage.get_job(job_id)
        if not job:
            return jsonify({"error": {"code": "not_found", "message": "Job not found."}}), 404
        return jsonify(_public_job(job))

    @app.get("/api/jobs/<job_id>/tweets")
    def get_tweets(job_id: str):
        if not storage.get_job(job_id):
            return jsonify({"error": {"code": "not_found", "message": "Job not found."}}), 404
        try:
            limit = min(max(int(request.args.get("limit", "100")), 1), 500)
            offset = max(int(request.args.get("offset", "0")), 0)
        except ValueError:
            return jsonify(
                {"error": {"code": "invalid_request", "message": "Pagination must use integers."}}
            ), 400
        return jsonify({"tweets": storage.get_job_tweets(job_id, limit=limit, offset=offset)})

    @app.delete("/api/jobs/<job_id>")
    def cancel_job(job_id: str):
        if not storage.get_job(job_id):
            return jsonify({"error": {"code": "not_found", "message": "Job not found."}}), 404
        if not jobs.cancel(job_id):
            return jsonify(
                {"error": {"code": "invalid_state", "message": "Job cannot be cancelled."}}
            ), 409
        return jsonify({"status": "cancelling"}), 202

    @app.post("/api/jobs/<job_id>/resume")
    def resume_job(job_id: str):
        if not storage.get_job(job_id):
            return jsonify({"error": {"code": "not_found", "message": "Job not found."}}), 404
        if not jobs.resume(job_id):
            return jsonify(
                {"error": {"code": "invalid_state", "message": "Job cannot be resumed."}}
            ), 409
        return jsonify({"status": "queued"}), 202

    @app.get("/api/jobs/<job_id>/export")
    def export_job(job_id: str):
        job = storage.get_job(job_id)
        if not job:
            return jsonify({"error": {"code": "not_found", "message": "Job not found."}}), 404
        rows = storage.get_job_tweets(job_id, limit=500)
        export_format = request.args.get("format", "json").lower()
        filename = f"x-collection-{job_id[:8]}"
        if export_format == "json":
            return Response(
                json.dumps(rows, indent=2, ensure_ascii=False),
                mimetype="application/json",
                headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
            )
        if export_format == "csv":
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            csv_rows = []
            for row in rows:
                csv_row = dict(row)
                csv_row["media"] = json.dumps(row.get("media", []), ensure_ascii=False)
                csv_rows.append(csv_row)
            writer.writerows(csv_rows)
            return Response(
                stream.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
            )
        return jsonify(
            {"error": {"code": "invalid_request", "message": "format must be json or csv."}}
        ), 400

    return app
