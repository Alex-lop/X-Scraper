from __future__ import annotations

import csv
import io
import ipaddress
import json
import math
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from .config import Settings
from .errors import CredentialError, InvalidRequestError
from .jobs import ERROR_MESSAGES, JobService
from .models import CollectionRequest, ProviderType
from .playwright_browser import PlaywrightBrowserProvider
from .providers import ProviderRegistry
from .storage import Storage
from .x_api import UNIT_PRICES_USD, XApiProvider

OFFICIAL_PROVIDER = ProviderType.OFFICIAL_X_API.value
BROWSER_PROVIDER = ProviderType.PLAYWRIGHT_BROWSER.value
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "interrupted", "partial"}
JOB_STATUSES = {*TERMINAL_STATUSES, "queued", "running", "waiting"}
PUBLIC_STRING_LIMIT = 32_768
PUBLIC_MEDIA_LIMIT = 25

COMMON_PROVENANCE_FIELDS = (
    "provider",
    "providerVersion",
    "sourceKind",
    "sourceUrl",
    "preparedAt",
    "compiledAt",
)
OFFICIAL_PROVENANCE_FIELDS = (
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
)
BROWSER_DETAIL_FIELDS = (
    "browserVersion",
    "sourceKind",
    "sourceUrl",
    "scanIterations",
    "scrollIterations",
    "observedAt",
)

REQUEST_FIELDS = (
    "provider",
    "sourceType",
    "sourceValue",
    "searchMode",
    "maxPosts",
    "startDate",
    "endDate",
    "includeReplies",
    "mediaOnly",
)
POST_FIELDS = (
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
    "view_count",
    "is_reply",
    "is_repost",
    "is_quote",
    "has_media",
    "source_position",
    "snapshot_position",
    "capture_segment",
    "scan_ordinal",
    "dom_position",
    "media",
)
POST_STRING_FIELDS = (
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
)
POST_INTEGER_FIELDS = (
    "like_count",
    "reply_count",
    "repost_count",
    "quote_count",
    "bookmark_count",
    "view_count",
    "source_position",
    "snapshot_position",
    "capture_segment",
    "scan_ordinal",
    "dom_position",
)
POST_BOOLEAN_FIELDS = ("is_reply", "is_repost", "is_quote", "has_media")
MEDIA_STRING_FIELDS = (
    "id",
    "mediaKey",
    "type",
    "url",
    "previewImageUrl",
    "altText",
)
MEDIA_INTEGER_FIELDS = ("width", "height", "durationMs")
COMPLETION_REASONS = {
    *ERROR_MESSAGES,
    "cursor_stalled",
    "full_archive_exhausted",
    "no_progress",
    "offline_demo_seeded",
    "post_resource_limit_reached",
    "recent_search_exhausted",
    "target_not_reached",
    "target_reached",
    "timeline_exhausted",
}

EXPORT_FIELDS = [
    "schema_version",
    "collection_id",
    "collection_status",
    "completion_reason",
    "warnings",
    "provider",
    "provider_version",
    "source_kind",
    "source_url",
    "browser_version",
    "scan_iterations",
    "scroll_iterations",
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
    "view_count",
    "is_reply",
    "is_repost",
    "is_quote",
    "has_media",
    "source_position",
    "media",
]


def _provider_value(value: Any) -> str:
    if value is None:
        raw = OFFICIAL_PROVIDER
    elif not isinstance(value, str):
        return "unknown"
    else:
        raw = value
    if raw == "x_api_search":
        return OFFICIAL_PROVIDER
    return raw if raw in {OFFICIAL_PROVIDER, BROWSER_PROVIDER} else "unknown"


def _execution_plan(job: dict[str, Any]) -> dict[str, Any]:
    plan = job.get("execution_plan") or job.get("compiled_request") or {}
    return plan if isinstance(plan, dict) else {}


def _public_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, str):
        return value[:PUBLIC_STRING_LIMIT]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _allowlist(source: Any, names: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    result = {}
    for name in names:
        value = _public_scalar(source.get(name))
        if value is not None:
            result[name] = value
    return result


def _public_request(value: Any) -> dict[str, Any]:
    try:
        return CollectionRequest.from_dict(_allowlist(value, REQUEST_FIELDS)).to_dict()
    except InvalidRequestError:
        return {}


def _public_post(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    post = {
        **{
            key: item[:PUBLIC_STRING_LIMIT] if isinstance((item := value.get(key)), str) else None
            for key in POST_STRING_FIELDS
        },
        **{
            key: item
            if isinstance((item := value.get(key)), int)
            and not isinstance(item, bool)
            and item >= 0
            else None
            for key in POST_INTEGER_FIELDS
        },
        **{
            key: item if isinstance((item := value.get(key)), bool) else None
            for key in POST_BOOLEAN_FIELDS
        },
    }
    media = value.get("media")
    if isinstance(media, list):
        public_media = []
        for item in media[:PUBLIC_MEDIA_LIMIT]:
            if not isinstance(item, dict):
                continue
            public_item = {
                key: field[:PUBLIC_STRING_LIMIT]
                for key in MEDIA_STRING_FIELDS
                if isinstance((field := item.get(key)), str)
            }
            public_item.update(
                {
                    key: field
                    for key in MEDIA_INTEGER_FIELDS
                    if isinstance((field := item.get(key)), int)
                    and not isinstance(field, bool)
                    and field >= 0
                }
            )
            if public_item:
                public_media.append(public_item)
        post["media"] = public_media
    else:
        post["media"] = None
    return post


def _spreadsheet_safe(value: str) -> str:
    for character in value:
        if character.isspace() or unicodedata.category(character)[0] in {"C", "Z"}:
            continue
        return f"'{value}" if character in "=+-@" else value
    return value


def _local_authority(value: Any) -> tuple[str, int | None] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    if hostname.casefold() == "localhost":
        return "localhost", port
    if "%" in hostname:
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return None
    return (str(address), port) if address.is_loopback else None


def _same_origin(origin: Any, host: tuple[str, int | None], scheme: str) -> bool:
    if not isinstance(origin, str) or scheme not in {"http", "https"}:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() != scheme
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    authority = _local_authority(parsed.netloc)
    if authority is None or authority[0] != host[0]:
        return False
    default_port = 80 if scheme == "http" else 443
    return (authority[1] or default_port) == (host[1] or default_port)


def _public_error(job: dict[str, Any]) -> dict[str, Any] | None:
    raw_code = job.get("error_code")
    if not raw_code:
        return None
    code = (
        raw_code
        if isinstance(raw_code, str) and raw_code in ERROR_MESSAGES
        else "provider_error"
    )
    return {
        "code": code,
        "message": ERROR_MESSAGES[code],
        "retryable": bool(job.get("error_retryable")),
    }


def _public_prices(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return UNIT_PRICES_USD
    prices = {}
    for name, fallback in UNIT_PRICES_USD.items():
        price = value.get(name)
        valid = (
            isinstance(price, int | float)
            and not isinstance(price, bool)
            and 0 <= price <= 1_000_000
            and (not isinstance(price, float) or math.isfinite(price))
        )
        prices[name] = (
            float(price)
            if valid
            else fallback
        )
    return prices


def _returned_list_price(resources: dict[str, int], prices: dict[str, float]) -> float:
    return round(
        resources["posts"] * prices["post"]
        + resources["users"] * prices["user"]
        + resources["media"] * prices["media"],
        3,
    )


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    plan = _execution_plan(job)
    provider = _provider_value(job.get("provider") or plan.get("provider"))
    provider_version = plan.get("providerVersion")
    if not isinstance(provider_version, int) or isinstance(provider_version, bool):
        provider_version = None
    request_body = _public_request(job.get("request"))
    provenance = _allowlist(plan, COMMON_PROVENANCE_FIELDS)
    provenance["provider"] = provider
    if provider_version is not None:
        provenance["providerVersion"] = provider_version
    if provider == OFFICIAL_PROVIDER:
        provenance.update(_allowlist(plan, OFFICIAL_PROVENANCE_FIELDS))
    raw_warnings = job.get("warnings")
    warnings = raw_warnings if isinstance(raw_warnings, list) else []
    raw_completion_reason = job.get("completion_reason")
    completion_reason = (
        raw_completion_reason
        if isinstance(raw_completion_reason, str)
        and raw_completion_reason in COMPLETION_REASONS
        else None
    )
    raw_status = job.get("status")
    status = (
        raw_status
        if isinstance(raw_status, str) and raw_status in JOB_STATUSES
        else "failed"
    )
    collected_count = job.get("collected_count")
    if (
        not isinstance(collected_count, int)
        or isinstance(collected_count, bool)
        or collected_count < 0
    ):
        collected_count = 0

    public = {
        "id": _public_scalar(job.get("id")),
        "provider": provider,
        "providerVersion": provider_version,
        "request": request_body,
        "status": status,
        "targetCount": request_body.get("maxPosts")
        or (
            plan.get("targetPosts")
            if isinstance(plan.get("targetPosts"), int)
            and not isinstance(plan.get("targetPosts"), bool)
            else 0
        ),
        "collectedCount": collected_count,
        "warnings": [
            warning[:500]
            for warning in warnings[:25]
            if isinstance(warning, str)
        ],
        "error": _public_error(job),
        "cancelRequested": bool(job.get("cancel_requested")),
        "completionReason": completion_reason,
        "retryAt": _public_scalar(job.get("retry_at")),
        "provenance": provenance,
        "isPartial": status == "partial"
        or (
            collected_count > 0 and status in {"failed", "cancelled", "interrupted"}
        ),
        "createdAt": _public_scalar(job.get("created_at")),
        "startedAt": _public_scalar(job.get("started_at")),
        "finishedAt": _public_scalar(job.get("finished_at")),
        "updatedAt": _public_scalar(job.get("updated_at")),
        "capturedAt": _public_scalar(job.get("finished_at") or job.get("updated_at")),
    }

    if provider == OFFICIAL_PROVIDER:
        resources = {
            "posts": _nonnegative_int(job.get("post_resource_count")),
            "users": _nonnegative_int(job.get("user_resource_count")),
            "media": _nonnegative_int(job.get("media_resource_count")),
        }
        prices = _public_prices(plan.get("unitPricesUsd"))
        public.update(
            resourcesReturned=resources,
            rateLimit={
                "remaining": _public_scalar(job.get("rate_limit_remaining")),
                "reset": _public_scalar(job.get("rate_limit_reset")),
            },
            cost={
                "basis": "list_price_pre_dedup",
                "unitPricesUsd": prices,
                "returnedListPriceEstimateUsd": _returned_list_price(resources, prices),
                "maximumPostResources": _public_scalar(plan.get("maximumPostResources")),
                "maximumPostListPriceUsd": _public_scalar(
                    plan.get("maximumPostListPriceUsd")
                ),
                "pricingAsOf": _public_scalar(plan.get("pricingAsOf")),
                "note": (
                    "List-price estimate before X daily resource deduplication; "
                    "it is not an invoice total."
                ),
            },
        )
    elif provider == BROWSER_PROVIDER:
        checkpoint = job.get("checkpoint") or {}
        metadata = checkpoint.get("metadata") if isinstance(checkpoint, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        details = _allowlist({**plan, **metadata}, BROWSER_DETAIL_FIELDS)
        public["providerDetails"] = details

    return public


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _request_body(body: Any) -> CollectionRequest:
    if not isinstance(body, dict):
        raise InvalidRequestError("Request body must be a JSON object.")
    controls = {
        "confirmPaidRead",
        "confirmBrowserCapture",
        "compiledRequest",
        "executionPlan",
    }
    return CollectionRequest.from_dict(
        {key: value for key, value in body.items() if key not in controls}
    )


def _default_registry(settings: Settings) -> ProviderRegistry:
    return ProviderRegistry(
        [PlaywrightBrowserProvider(settings), XApiProvider(settings)]
    )


def _registry_view(registry: ProviderRegistry, provider: ProviderType) -> tuple[dict, dict]:
    try:
        return registry.capabilities(provider), registry.connection_status(provider)
    except (InvalidRequestError, KeyError, ValueError):
        return (
            {"provider": provider.value, "available": False},
            {
                "status": "unavailable",
                "ready": False,
                "message": "Provider is unavailable in this application instance.",
            },
        )


def create_app(
    settings: Settings | None = None,
    *,
    storage: Storage | None = None,
    registry: ProviderRegistry | None = None,
    provider: Any | None = None,
    start_worker: bool = True,
    collection_enabled: bool = True,
) -> Flask:
    settings = settings or Settings.from_env()
    settings.ensure_runtime_dirs()
    storage = storage or Storage(settings.database_path)
    storage.initialize()
    if registry is not None and provider is not None:
        raise ValueError("Pass registry or provider, not both.")
    registry = registry or (
        ProviderRegistry([provider]) if provider else _default_registry(settings)
    )
    jobs = JobService(storage, registry, start_worker=start_worker)

    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.extensions["xworkbench_jobs"] = jobs
    app.extensions["xworkbench_registry"] = registry

    @app.before_request
    def require_local_host_and_json_mutations():
        host = _local_authority(request.host)
        if host is None or request.scheme not in {"http", "https"}:
            return _error("local_only", "Only loopback hosts are accepted.", 403)
        remote = request.remote_addr
        try:
            local_remote = bool(
                remote and "%" not in remote and ipaddress.ip_address(remote).is_loopback
            )
        except ValueError:
            local_remote = False
        if not local_remote:
            return _error("local_only", "Only loopback clients are accepted.", 403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not request.is_json:
            return _error(
                "json_required",
                "State-changing requests require Content-Type: application/json.",
                415,
            )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if origin and not _same_origin(origin, host, request.scheme):
                return _error(
                    "local_origin_required",
                    "State-changing requests require the same loopback origin.",
                    403,
                )

    @app.after_request
    def secure_local_response(response: Response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; connect-src 'self'; "
            "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "img-src 'self' data:; object-src 'none'; script-src 'self'; "
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
        browser_capabilities, browser = _registry_view(
            registry, ProviderType.PLAYWRIGHT_BROWSER
        )
        official_capabilities, official = _registry_view(
            registry, ProviderType.OFFICIAL_X_API
        )
        return jsonify(
            {
                "defaultProvider": BROWSER_PROVIDER,
                "providers": {
                    BROWSER_PROVIDER: {
                        "capabilities": browser_capabilities,
                        "connection": browser,
                    },
                    OFFICIAL_PROVIDER: {
                        "capabilities": official_capabilities,
                        "connection": official,
                    },
                },
                # Retained for older local clients.
                "status": official.get("status"),
                "configured": bool(official.get("ready", official.get("valid"))),
                "source": official.get("source", "none"),
                "message": official.get("message"),
                "demoMode": "offline" if not collection_enabled else None,
            }
        )

    @app.post("/api/collections/preview")
    def preview_collection():
        if not collection_enabled:
            return _error("collection_disabled", "Live collection is disabled in demo mode.", 409)
        try:
            collection_request = _request_body(request.get_json(silent=True))
            execution_plan = registry.prepare(collection_request)
        except CredentialError as exc:
            return _error("connection_missing", str(exc), 409)
        except InvalidRequestError as exc:
            return _error(exc.code, str(exc), 400)
        provider_id = _provider_value(collection_request.provider)
        result = {
            "provider": provider_id,
            "request": collection_request.to_dict(),
            "executionPlan": execution_plan,
            "confirmation": {
                "kind": "paid_read" if provider_id == OFFICIAL_PROVIDER else "browser_capture",
                "required": True,
            },
        }
        if provider_id == OFFICIAL_PROVIDER:
            result.update(
                compiledRequest=execution_plan,
                compiledIntent={
                    "searchMode": execution_plan["searchMode"],
                    "endpoint": execution_plan["endpoint"],
                    "query": execution_plan["query"],
                    "compiledLength": execution_plan["queryLength"],
                    "startTime": execution_plan["startTime"],
                    "endTime": execution_plan["endTime"],
                    "sortOrder": execution_plan["sortOrder"],
                    "expiresAt": execution_plan["expiresAt"],
                },
                costEstimate={
                    "basis": "list_price_pre_dedup",
                    "maximumPostResources": execution_plan["maximumPostResources"],
                    "maximumPostListPriceUsd": execution_plan["maximumPostListPriceUsd"],
                    "unitPricesUsd": execution_plan.get("unitPricesUsd", UNIT_PRICES_USD),
                    "variableResources": ["users", "media"],
                    "pricingAsOf": execution_plan["pricingAsOf"],
                    "pricingUrl": "https://docs.x.com/x-api/getting-started/pricing",
                    "note": (
                        "User and media resources vary with the response. Estimates use "
                        "list prices before X daily resource deduplication and are not an "
                        "invoice total. Set the hard spending limit in the Developer Console."
                    ),
                },
            )
        else:
            result["captureIntent"] = _allowlist(
                execution_plan,
                ("sourceKind", "sourceUrl", "targetPosts", "providerVersion"),
            )
        return jsonify(result)

    @app.post("/api/jobs")
    def create_job():
        if not collection_enabled:
            return _error("collection_disabled", "Live collection is disabled in demo mode.", 409)
        body = request.get_json(silent=True)
        try:
            collection_request = _request_body(body)
            if not isinstance(body, dict):
                raise InvalidRequestError("Request body must be a JSON object.")
            provider_id = _provider_value(collection_request.provider)
            execution_plan_value = body.get("executionPlan")
            compiled_request_value = body.get("compiledRequest")
            if execution_plan_value is None and compiled_request_value is None:
                raise InvalidRequestError("A collection preview executionPlan is required.")
            if (
                execution_plan_value is not None
                and compiled_request_value is not None
                and execution_plan_value != compiled_request_value
            ):
                raise InvalidRequestError("executionPlan and compiledRequest must match.")
            supplied_plan = (
                execution_plan_value
                if execution_plan_value is not None
                else compiled_request_value
            )
            execution_plan = registry.prepare(collection_request, supplied_plan)
            confirmation = (
                "confirmPaidRead" if provider_id == OFFICIAL_PROVIDER else "confirmBrowserCapture"
            )
            if confirmation in body and not isinstance(body[confirmation], bool):
                raise InvalidRequestError(f"{confirmation} must be a boolean.")
            if body.get(confirmation) is not True:
                message = (
                    "confirmPaidRead must be true for a paid X API read."
                    if provider_id == OFFICIAL_PROVIDER
                    else "confirmBrowserCapture must be true to start a browser capture."
                )
                raise InvalidRequestError(message)
        except CredentialError as exc:
            return _error("connection_missing", str(exc), 409)
        except InvalidRequestError as exc:
            return _error(exc.code, str(exc), 400)
        job_id = jobs.submit(collection_request, execution_plan)
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
        posts = [
            _public_post(post)
            for post in storage.get_job_posts(job_id, limit=limit, offset=offset)
        ]
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
        if not storage.get_job(job_id):
            return _error("not_found", "Job not found.", 404)
        if not jobs.resume(job_id):
            return _error("invalid_state", "Job cannot be resumed.", 409)
        return jsonify({"status": "queued"}), 202

    @app.get("/api/jobs/<job_id>/export")
    def export_job(job_id: str):
        job = storage.get_job(job_id)
        if not job:
            return _error("not_found", "Job not found.", 404)
        rows = [_public_post(post) for post in storage.get_job_posts(job_id, limit=500)]
        export_format = request.args.get("format", "json").lower()
        public_job = _public_job(job)
        filename = f"x-snapshot-{job_id[:8]}-{job['status']}"
        headers = {
            "X-Collection-Status": public_job["status"],
            "X-Completion-Reason": public_job.get("completionReason") or "",
            "X-Result-Count": str(len(rows)),
            "X-Snapshot-At": str(public_job.get("updatedAt") or ""),
            "X-Provider": public_job["provider"],
            "X-Source-Kind": str(public_job["provenance"].get("sourceKind") or ""),
        }
        if export_format == "json":
            return Response(
                json.dumps(
                    {"schemaVersion": 4, "job": public_job, "posts": rows},
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
            provenance = public_job["provenance"]
            details = public_job.get("providerDetails", {})
            resources = public_job.get("resourcesReturned", {})
            for post in rows:
                row = {
                    "schema_version": 4,
                    "collection_id": job_id,
                    "collection_status": public_job["status"],
                    "completion_reason": public_job.get("completionReason"),
                    "warnings": json.dumps(public_job["warnings"], ensure_ascii=False),
                    "provider": public_job["provider"],
                    "provider_version": public_job.get("providerVersion"),
                    "source_kind": provenance.get("sourceKind"),
                    "source_url": provenance.get("sourceUrl"),
                    "browser_version": details.get("browserVersion"),
                    "scan_iterations": details.get("scanIterations"),
                    "scroll_iterations": details.get("scrollIterations"),
                    "compiler_version": provenance.get("compilerVersion"),
                    "search_mode": provenance.get("searchMode"),
                    "endpoint": provenance.get("endpoint"),
                    "effective_query": provenance.get("query"),
                    "start_time": provenance.get("startTime"),
                    "end_time": provenance.get("endTime"),
                    "post_resources_returned": resources.get("posts"),
                    "user_resources_returned": resources.get("users"),
                    "media_resources_returned": resources.get("media"),
                    **post,
                }
                if row.get("media") is not None:
                    row["media"] = json.dumps(row["media"], ensure_ascii=False)
                for field, value in row.items():
                    if isinstance(value, str):
                        row[field] = _spreadsheet_safe(value)
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
