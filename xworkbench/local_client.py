from __future__ import annotations

import ipaddress
import json
import re
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_API_PATH = re.compile(r"^/api/[A-Za-z0-9._~:/-]+$")
_ID = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
_ALLOWED_PATHS = {
    "GET": (
        re.compile(
            r"^/api/(?:health|connection|sources|snapshots|compare|evidence/search|"
            r"collection-health|jobs|progress|queue/metrics)$"
        ),
        re.compile(rf"^/api/jobs/{_ID}(?:/posts)?$"),
    ),
    "POST": (
        re.compile(r"^/api/(?:sources|collections/preview|jobs|batches/preview|batches/confirm)$"),
        re.compile(rf"^/api/(?:jobs|batches)/{_ID}/cancel$"),
    ),
}


class OutcomeUnknownError(RuntimeError):
    """A mutation may have reached the server; callers must refresh durable state."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_LOCAL_OPENER = build_opener(ProxyHandler({}), _NoRedirect).open


def loopback_base_url(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Local clients connect only to a loopback HTTP dashboard root.") from exc
    if not hostname or "%" in hostname:
        loopback = False
    elif hostname.casefold() == "localhost":
        hostname, loopback = "127.0.0.1", True
    else:
        try:
            address = ipaddress.ip_address(hostname)
            hostname, loopback = str(address), address.is_loopback
        except ValueError:
            loopback = False
    if (
        parsed.scheme != "http"
        or not loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Local clients connect only to a loopback HTTP dashboard root.")
    authority = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{authority}{f':{port}' if port is not None else ''}"


def _fixed_api_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or not _API_PATH.fullmatch(path)
        or "//" in path
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        raise ValueError("Local clients may call only fixed local API paths.")
    return path


def _bounded_read(response, maximum: int = MAX_RESPONSE_BYTES) -> bytes:
    length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    try:
        if length is not None and int(length) > maximum:
            raise RuntimeError("The local dashboard response was too large.")
    except (TypeError, ValueError):
        pass
    try:
        payload = response.read(maximum + 1)
    except TypeError:  # Small test doubles and legacy openers may not accept a size.
        payload = response.read()
    if not isinstance(payload, bytes) or len(payload) > maximum:
        raise RuntimeError("The local dashboard response was too large.")
    return payload


class LocalJsonClient:
    def __init__(
        self,
        base_url: str,
        *,
        opener=None,
        timeout: float = 10,
    ) -> None:
        self.base_url = loopback_base_url(base_url)
        self._opener = opener or _LOCAL_OPENER
        self.timeout = timeout

    def get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, query=query)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, body=body)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if method not in {"GET", "POST"}:
            raise ValueError("Local clients support only GET and POST.")
        path = _fixed_api_path(path)
        if not any(pattern.fullmatch(path) for pattern in _ALLOWED_PATHS[method]):
            raise ValueError("Local clients may call only fixed local API paths.")
        if query is not None and (method != "GET" or not isinstance(query, dict)):
            raise ValueError("Only GET requests may contain a query object.")
        if body is not None and (method != "POST" or not isinstance(body, dict)):
            raise ValueError("Only POST requests may contain a JSON object.")
        target = self.base_url + path
        if query:
            target += "?" + urlencode(query, doseq=True)
        data = None
        headers = {}
        if method == "POST":
            data = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":")).encode()
            if len(data) > MAX_REQUEST_BYTES:
                raise ValueError("The local request body is too large.")
            headers["Content-Type"] = "application/json"
        request = Request(target, data=data, headers=headers, method=method)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                final_url = getattr(response, "geturl", lambda: target)()
                if final_url != target:
                    raise RuntimeError("The local dashboard response redirected unexpectedly.")
                payload = json.loads(_bounded_read(response))
        except HTTPError as exc:
            try:
                error = json.loads(_bounded_read(exc))
                detail = error.get("error", {}).get("message")
            except (AttributeError, json.JSONDecodeError, OSError, RuntimeError):
                detail = None
            raise RuntimeError(detail or f"Dashboard returned HTTP {exc.code}.") from exc
        except (
            URLError,
            TimeoutError,
            OSError,
            HTTPException,
            json.JSONDecodeError,
            UnicodeError,
        ) as exc:
            message = "The local dashboard is unavailable. Start it with: xworkbench start"
            if method == "POST":
                raise OutcomeUnknownError(
                    "Mutation outcome unknown; durable state must be refreshed before retrying."
                ) from exc
            raise RuntimeError(message) from exc
        except RuntimeError as exc:
            if method == "POST" and "redirected" not in str(exc):
                raise OutcomeUnknownError(
                    "Mutation outcome unknown; durable state must be refreshed before retrying."
                ) from exc
            raise
        if not isinstance(payload, dict):
            error = "The local dashboard returned an invalid response."
            if method == "POST":
                raise OutcomeUnknownError(
                    "Mutation outcome unknown; durable state must be refreshed before retrying."
                )
            raise RuntimeError(error)
        return payload
