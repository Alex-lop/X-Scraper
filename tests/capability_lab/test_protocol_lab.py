from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

from xworkbench.playwright_browser import (
    BrowserManualActionRequired,
    PlaywrightBrowserProvider,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("XWORKBENCH_RUN_CAPABILITY_LAB") != "1",
    reason="the capability lab runs only in its network-isolated CI job",
)

_OPERATION = "LabFixtureTimeline"
_QUERY_V1 = "lab_fixture_query_v1_expired"
_QUERY_V2 = "lab_fixture_query_v2"
_CURSOR = "lab_fixture_cursor_page_2"
_REDACTED = "[fixture-secret-redacted]"


class _ExpiredFixtureQuery(RuntimeError):
    def __init__(self, replacement: str | None):
        super().__init__(f"synthetic query ID expired; declared replacement: {replacement!r}")
        self.replacement = replacement


class _ProtocolFixture:
    """Two fixed loopback protocols; handles cannot represent an address or credential."""

    def __init__(self) -> None:
        self.graphql_handle: object | None = None
        self.challenge_handle: object | None = None
        self.sentinel: str | None = None
        self.captures: list[dict[str, Any]] = []
        self.request_count = 0
        self.port: int | None = None
        self.thread: threading.Thread | None = None
        self._server: ThreadingHTTPServer | None = None

    def __enter__(self) -> _ProtocolFixture:
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                pass

            def _json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    value = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {}
                return value if isinstance(value, dict) else {}

            def _reply(self, status: int, value: dict[str, Any]) -> None:
                body = json.dumps(value, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                fixture.request_count += 1
                body = self._json()
                if self.path == "/internal/graphql":
                    fixture.captures.append(
                        {
                            "path": self.path,
                            "headers": {"X-Fixture-Session": _REDACTED},
                            "body": json.loads(json.dumps(body)),
                        }
                    )
                    if self.headers.get("X-Fixture-Session") != fixture.sentinel:
                        self._reply(401, {"error": "fixture session rejected"})
                    elif body.get("queryId") == _QUERY_V1:
                        self._reply(
                            410,
                            {
                                "error": "synthetic query ID expired",
                                "replacementQueryId": _QUERY_V2,
                            },
                        )
                    else:
                        cursor = body.get("variables", {}).get("cursor")
                        if cursor is None:
                            self._reply(
                                200,
                                {
                                    "data": {"items": ["fixture-post-1", "fixture-post-2"]},
                                    "pageInfo": {"nextCursor": _CURSOR, "hasNextPage": True},
                                },
                            )
                        else:
                            self._reply(
                                200,
                                {
                                    "data": {"items": ["fixture-post-3"]},
                                    "pageInfo": {"nextCursor": None, "hasNextPage": False},
                                },
                            )
                    return
                if self.path == "/fixture/toy-challenge/answer":
                    candidate = body.get("candidate")
                    accepted = (
                        isinstance(candidate, int)
                        and not isinstance(candidate, bool)
                        and (candidate * 7 + 3) % 13 == 2
                    )
                    self._reply(
                        200 if accepted else 403,
                        {"result": "accepted" if accepted else "incorrect"},
                    )
                    return
                self._reply(404, {"error": "fixture route missing"})

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                fixture.request_count += 1
                if self.path == "/fixture/toy-challenge":
                    self._reply(
                        403,
                        {
                            "kind": "lab-fixture-toy-challenge",
                            "rule": "candidate-times-7-plus-3-mod-13",
                            "target": 2,
                            "maxAttempts": 12,
                        },
                    )
                    return
                self._reply(404, {"error": "fixture route missing"})

        self.graphql_handle = object()
        self.challenge_handle = object()
        self.sentinel = secrets.token_urlsafe(18)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, self.port = self._server.server_address
        if host != "127.0.0.1":
            self._server.server_close()
            raise RuntimeError("protocol fixture did not bind to IPv4 loopback")
        self.thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        assert self.thread is not None
        self.thread.join(timeout=2)
        self._server = None
        self.graphql_handle = None
        self.challenge_handle = None
        self.sentinel = None
        self.port = None

    def _exchange(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if self.port is None:
            raise RuntimeError("protocol fixture is not running")
        encoded = None if body is None else json.dumps(body, separators=(",", ":"))
        headers = {"Content-Type": "application/json"}
        if path == "/internal/graphql":
            assert self.sentinel is not None
            headers["X-Fixture-Session"] = self.sentinel
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            value = json.loads(response.read())
            if 300 <= response.status < 400:
                raise RuntimeError("fixture redirects are prohibited")
            return response.status, value
        finally:
            connection.close()

    def graphql(self, handle: object, payload: dict[str, Any]) -> dict[str, Any]:
        if handle is not self.graphql_handle:
            raise ValueError("unissued GraphQL fixture handle")
        variables = payload.get("variables") if isinstance(payload, dict) else None
        if (
            set(payload) != {"operationName", "queryId", "variables"}
            or payload.get("operationName") != _OPERATION
            or payload.get("queryId") not in {_QUERY_V1, _QUERY_V2}
            or not isinstance(variables, dict)
            or set(variables) != {"cursor", "pageSize"}
            or variables.get("cursor") not in {None, _CURSOR}
            or variables.get("pageSize") != 2
        ):
            raise ValueError("request is outside the fixed GraphQL fixture schema")
        status, value = self._exchange("POST", "/internal/graphql", payload)
        if status == 410:
            raise _ExpiredFixtureQuery(value.get("replacementQueryId"))
        if status != 200:
            raise RuntimeError(f"fixture GraphQL request failed with {status}")
        return value

    def replay(self, handle: object, capture: dict[str, Any]) -> dict[str, Any]:
        if (
            set(capture) != {"path", "headers", "body"}
            or capture.get("path") != "/internal/graphql"
            or capture.get("headers") != {"X-Fixture-Session": _REDACTED}
        ):
            raise ValueError("capture is not a sanitized fixture request")
        return self.graphql(handle, capture["body"])

    def use_declared_v2(
        self, handle: object, expired_payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return self.graphql(handle, expired_payload)
        except _ExpiredFixtureQuery as exc:
            if exc.replacement != _QUERY_V2:
                raise RuntimeError("fixture did not declare its one permitted replacement") from exc
        replacement = json.loads(json.dumps(expired_payload))
        replacement["queryId"] = _QUERY_V2
        return self.graphql(handle, replacement)

    def submit_toy_answer(self, handle: object, candidate: int) -> dict[str, Any]:
        if handle is not self.challenge_handle:
            raise ValueError("unissued toy-challenge fixture handle")
        if isinstance(candidate, bool) or not isinstance(candidate, int) or not 0 <= candidate < 12:
            raise ValueError("toy answer is outside the fixture's bounded range")
        status, value = self._exchange(
            "POST", "/fixture/toy-challenge/answer", {"candidate": candidate}
        )
        if status not in {200, 403}:
            raise RuntimeError(f"toy challenge answer failed with {status}")
        return value

    def detect_and_solve_toy(
        self,
        handle: object,
        clock: Callable[[], float],
    ) -> dict[str, Any]:
        if handle is not self.challenge_handle:
            raise ValueError("unissued toy-challenge fixture handle")
        status, challenge = self._exchange("GET", "/fixture/toy-challenge")
        expected = {
            "kind": "lab-fixture-toy-challenge",
            "rule": "candidate-times-7-plus-3-mod-13",
            "target": 2,
            "maxAttempts": 12,
        }
        if status != 403 or challenge != expected:
            raise RuntimeError("fixed toy challenge was not detected")
        started = clock()
        for candidate in range(12):
            if clock() - started >= 0.05:
                raise TimeoutError("fixed toy challenge deadline expired")
            if (candidate * 7 + 3) % 13 == 2:
                result = self.submit_toy_answer(handle, candidate)
                if result != {"result": "accepted"}:
                    raise RuntimeError("fixed toy challenge rejected the bounded solution")
                return {"detected": True, "candidate": candidate, "attempts": candidate + 1}
        raise RuntimeError("fixed toy challenge had no bounded solution")


def _graphql_payload(query_id: str = _QUERY_V2, cursor: str | None = None) -> dict[str, Any]:
    return {
        "operationName": _OPERATION,
        "queryId": query_id,
        "variables": {"cursor": cursor, "pageSize": 2},
    }


def _assert_listener_closed(port: int, thread: threading.Thread) -> None:
    assert not thread.is_alive()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.05)


def test_fixture_graphql_capture_replay_version_change_and_pagination() -> None:
    with _ProtocolFixture() as lab:
        assert lab.graphql_handle is not None
        first = lab.graphql(lab.graphql_handle, _graphql_payload())
        capture = lab.captures[-1]
        assert capture["headers"] == {"X-Fixture-Session": _REDACTED}
        assert lab.sentinel not in json.dumps(capture)
        assert lab.replay(lab.graphql_handle, capture) == first
        assert first["data"]["items"] == ["fixture-post-1", "fixture-post-2"]
        assert first["pageInfo"] == {"nextCursor": _CURSOR, "hasNextPage": True}

        expired = _graphql_payload(_QUERY_V1)
        with pytest.raises(_ExpiredFixtureQuery, match="synthetic query ID expired"):
            lab.graphql(lab.graphql_handle, expired)
        assert lab.use_declared_v2(lab.graphql_handle, expired) == first

        final = lab.graphql(lab.graphql_handle, _graphql_payload(cursor=_CURSOR))
        assert final == {
            "data": {"items": ["fixture-post-3"]},
            "pageInfo": {"nextCursor": None, "hasNextPage": False},
        }
        assert lab.thread is not None and lab.port is not None
        thread, port = lab.thread, lab.port
    assert lab.sentinel is lab.graphql_handle is lab.challenge_handle is lab.port is None
    _assert_listener_closed(port, thread)


def test_fixture_graphql_rejects_external_activation_before_contact(monkeypatch) -> None:
    with _ProtocolFixture() as lab:
        assert lab.graphql_handle is not None
        before = lab.request_count
        contacted = False

        def fail_contact(*_args: object, **_kwargs: object) -> None:
            nonlocal contacted
            contacted = True
            raise AssertionError("network contact happened")

        monkeypatch.setattr(socket, "create_connection", fail_contact)
        monkeypatch.setattr(socket, "getaddrinfo", fail_contact)
        provider = object.__new__(PlaywrightBrowserProvider)
        for unissued in (
            "https://x.com/internal/graphql",
            "fixture-dns-name.invalid",
            provider,
        ):
            with pytest.raises(ValueError, match="unissued GraphQL fixture handle"):
                lab.graphql(unissued, _graphql_payload())
        for forbidden_field in ("redirect", "proxy", "token"):
            payload = _graphql_payload()
            payload["variables"][forbidden_field] = "https://external.invalid"
            with pytest.raises(ValueError, match="fixed GraphQL fixture schema"):
                lab.graphql(lab.graphql_handle, payload)
        with pytest.raises(TypeError):
            lab.graphql(lab.graphql_handle, _graphql_payload(), token="pasted-secret")
        assert lab.request_count == before
        assert not contacted


def test_toy_challenge_detection_bounded_solution_timeout_wrong_answer_and_cleanup() -> None:
    with _ProtocolFixture() as lab:
        assert lab.challenge_handle is not None
        assert lab.submit_toy_answer(lab.challenge_handle, 10) == {"result": "incorrect"}
        solved = lab.detect_and_solve_toy(lab.challenge_handle, lambda: 0.0)
        assert solved == {"detected": True, "candidate": 11, "attempts": 12}

        ticks = iter((0.0, 1.0))
        with pytest.raises(TimeoutError, match="deadline expired"):
            lab.detect_and_solve_toy(lab.challenge_handle, lambda: next(ticks))
        assert lab.thread is not None and lab.port is not None
        thread, port = lab.thread, lab.port
    assert lab.sentinel is lab.graphql_handle is lab.challenge_handle is lab.port is None
    _assert_listener_closed(port, thread)


def test_production_x_challenge_stops_and_cannot_activate_toy_fixture(monkeypatch) -> None:
    page = SimpleNamespace(
        url="https://x.com/account/access",
        locator=lambda _selector: SimpleNamespace(count=lambda: 0),
    )
    provider = object.__new__(PlaywrightBrowserProvider)
    failure = provider._page_failure(page)
    assert isinstance(failure, BrowserManualActionRequired)
    assert "no challenge was bypassed" in str(failure)

    with _ProtocolFixture() as lab:
        before = lab.request_count
        contacted = False

        def fail_contact(*_args: object, **_kwargs: object) -> None:
            nonlocal contacted
            contacted = True
            raise AssertionError("network contact happened")

        monkeypatch.setattr(socket, "create_connection", fail_contact)
        monkeypatch.setattr(socket, "getaddrinfo", fail_contact)
        for unissued in (provider, "https://x.com/challenge", "external-proxy.invalid:8080"):
            with pytest.raises(ValueError, match="unissued toy-challenge fixture handle"):
                lab.detect_and_solve_toy(unissued, lambda: 0.0)
        with pytest.raises(TypeError):
            lab.detect_and_solve_toy(
                lab.challenge_handle,
                lambda: 0.0,
                token="pasted-secret",
            )
        assert lab.request_count == before
        assert not contacted
