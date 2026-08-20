from __future__ import annotations

import json
from http.client import IncompleteRead
from urllib.error import URLError
from urllib.request import ProxyHandler

import pytest

from xworkbench.local_client import (
    _LOCAL_OPENER,
    MAX_RESPONSE_BYTES,
    LocalJsonClient,
    OutcomeUnknownError,
)
from xworkbench.mcp_server import RestClient


class _Response:
    def __init__(self, payload, *, final_url=None, declared_size=None) -> None:
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.final_url = final_url
        self.headers = {}
        if declared_size is not None:
            self.headers["Content-Length"] = str(declared_size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]

    def geturl(self):
        return self.final_url


def test_shared_client_uses_no_proxy_and_only_bounded_loopback_api_paths():
    proxy_handlers = [
        handler for handler in _LOCAL_OPENER.__self__.handlers if isinstance(handler, ProxyHandler)
    ]
    assert proxy_handlers == []  # Passing ProxyHandler({}) suppresses the environment default.

    for url in (
        "https://127.0.0.1:5000",
        "http://example.com:5000",
        "http://user:pass@127.0.0.1:5000",
        "http://127.0.0.1:5000/#fragment",
        "http://127.0.0.1:5000/api",
        "http://[::1%25lo0]:5000",
    ):
        with pytest.raises(ValueError, match="loopback"):
            LocalJsonClient(url)

    client = LocalJsonClient(
        "http://localhost:5000",
        opener=lambda request, **_kwargs: _Response({}, final_url=request.full_url),
    )
    assert client.base_url == "http://127.0.0.1:5000"
    for method, path in (
        ("GET", "/api/../jobs"),
        ("GET", "/api/jobs?limit=1"),
        ("GET", "/api/not-a-real-surface"),
        ("POST", "/api/jobs/not-valid!/cancel"),
        ("DELETE", "/api/jobs/valid"),
    ):
        with pytest.raises(ValueError, match="fixed|GET and POST"):
            client.request(method, path)

    with pytest.raises(ValueError, match="too large"):
        client.post("/api/jobs", {"value": "x" * (64 * 1024)})


def test_shared_client_bounds_responses_rejects_redirects_and_marks_unknown_mutations():
    target = "http://127.0.0.1:5000/api/jobs"
    too_large = LocalJsonClient(
        "http://127.0.0.1:5000",
        opener=lambda *_args, **_kwargs: _Response(
            {}, final_url=target, declared_size=MAX_RESPONSE_BYTES + 1
        ),
    )
    with pytest.raises(RuntimeError, match="too large"):
        too_large.get("/api/jobs")

    redirected = LocalJsonClient(
        "http://127.0.0.1:5000",
        opener=lambda *_args, **_kwargs: _Response(
            {}, final_url="http://127.0.0.1:5000/api/health"
        ),
    )
    with pytest.raises(RuntimeError, match="redirected"):
        redirected.get("/api/jobs")

    malformed = LocalJsonClient(
        "http://127.0.0.1:5000",
        opener=lambda request, **_kwargs: _Response(b"not-json", final_url=request.full_url),
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        malformed.get("/api/jobs")
    with pytest.raises(OutcomeUnknownError, match="outcome unknown"):
        malformed.post("/api/jobs", {})

    unavailable = LocalJsonClient(
        "http://127.0.0.1:5000",
        opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    with pytest.raises(OutcomeUnknownError, match="durable state"):
        unavailable.post("/api/jobs", {})

    class Partial(_Response):
        def read(self, _size=-1):
            raise IncompleteRead(b'{"status":', 20)

    truncated = LocalJsonClient(
        "http://127.0.0.1:5000",
        opener=lambda request, **_kwargs: Partial({}, final_url=request.full_url),
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        truncated.get("/api/jobs")
    with pytest.raises(OutcomeUnknownError, match="outcome unknown"):
        truncated.post("/api/jobs", {})


def test_mcp_wrapper_remains_get_only():
    assert not hasattr(RestClient, "post")
