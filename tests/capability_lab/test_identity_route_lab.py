from __future__ import annotations

import json
import os
import secrets
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("XWORKBENCH_RUN_CAPABILITY_LAB") != "1",
    reason="capability lab runs only in its network-isolated CI job",
)


def _require_lab_activation(activation: str) -> None:
    if activation != "lab":
        raise RuntimeError("Capability fixtures cannot activate for a production X provider.")


@dataclass
class _FakeIdentity:
    alias: str
    secret: str = field(repr=False)
    expires_at: int = 100
    leased: bool = False
    cooldown_until: int = 0


class _IdentityFixture:
    """Hard-coded fixture scenario, deliberately not a reusable identity-pool API."""

    def __init__(self, activation: str = "lab") -> None:
        _require_lab_activation(activation)
        self.now = 0
        self.audit: list[dict[str, str | int]] = []
        self.identities = {
            "fixture-alpha": _FakeIdentity(
                "fixture-alpha", f"sentinel-{secrets.token_hex(12)}"
            ),
            "fixture-beta": _FakeIdentity(
                "fixture-beta", f"sentinel-{secrets.token_hex(12)}", expires_at=5
            ),
        }

    def __enter__(self) -> _IdentityFixture:
        return self

    def __exit__(self, *_: object) -> None:
        for identity in self.identities.values():
            identity.secret = ""
            identity.leased = False
        self.identities.clear()

    def lease(self) -> _FakeIdentity:
        for alias, identity in self.identities.items():
            if (
                identity.expires_at > self.now
                and not identity.leased
                and identity.cooldown_until <= self.now
            ):
                identity.leased = True
                self.audit.append({"at": self.now, "event": "leased", "identity": alias})
                return identity
        raise RuntimeError("no fixture identity is eligible for its one permitted lease")

    def release(self, alias: str) -> None:
        identity = self.identities[alias]
        identity.leased = False
        identity.cooldown_until = self.now + 10
        self.audit.append(
            {
                "at": self.now,
                "event": "released_to_cooldown",
                "identity": alias,
                "cooldownUntil": identity.cooldown_until,
            }
        )

    def replace_expired_beta(self) -> str:
        expired = self.identities["fixture-beta"]
        if expired.expires_at > self.now or expired.leased:
            raise RuntimeError("fixture-beta is not replaceable")
        expired.secret = ""
        del self.identities["fixture-beta"]
        alias = "fixture-beta-r2"
        self.identities[alias] = _FakeIdentity(
            alias, f"sentinel-{secrets.token_hex(12)}", expires_at=100
        )
        self.audit.append(
            {"at": self.now, "event": "expired_identity_replaced", "identity": alias}
        )
        return alias

    def report(self) -> str:
        return json.dumps(self.audit, sort_keys=True)


class _FixtureHTTPServer(HTTPServer):
    allow_reuse_address = False


class _FixtureDisconnectServer(socketserver.TCPServer):
    allow_reuse_address = False


_FIXTURE_TARGET = "http://127.0.0.1:9/fixture-target"
_ABSOLUTE_REQUEST_LINE = f"GET {_FIXTURE_TARGET} HTTP/1.1"


def _disconnect_handler_for(records: list[str]) -> type[socketserver.BaseRequestHandler]:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            request = self.request.recv(2_048)
            records.append(request.partition(b"\r\n")[0].decode("ascii", "replace"))
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    return Handler


def _handler_for(outcome: str, records: list[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            records.append(self.requestline)
            if self.path != _FIXTURE_TARGET:
                self.send_error(404)
                return
            if outcome == "timeout":
                time.sleep(0.08)
            status = int(outcome) if outcome.isdigit() else 200
            body = b"fixture-challenge" if outcome == "challenge" else outcome.encode()
            try:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_: Any) -> None:
            pass

    return Handler


class _RouteHandle:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<opaque fixture route>"


class _RouteFixture:
    """One fixed failure script, deliberately not a reusable routing policy."""

    _SCRIPT = (
        ("route-forbidden", "403"),
        ("route-limited", "429"),
        ("route-challenge", "challenge"),
        ("route-slow", "timeout"),
        ("route-disconnect", "disconnect"),
    )

    def __init__(self, activation: str = "lab") -> None:
        _require_lab_activation(activation)
        self.audit: list[dict[str, str | int]] = []
        self.cooldowns: dict[str, int] = {}
        self.now = 0
        self.contact_count = 0
        self._servers: list[socketserver.BaseServer] = []
        self._threads: list[threading.Thread] = []
        self._handles = {alias: _RouteHandle() for alias, _ in self._SCRIPT}
        self._aliases = {handle: alias for alias, handle in self._handles.items()}
        self._ports: dict[_RouteHandle, int] = {}
        self._records = {handle: [] for handle in self._handles.values()}

    def __enter__(self) -> _RouteFixture:
        try:
            for alias, outcome in self._SCRIPT:
                handle = self._handles[alias]
                records = self._records[handle]
                if outcome == "disconnect":
                    server: socketserver.BaseServer = _FixtureDisconnectServer(
                        ("127.0.0.1", 0), _disconnect_handler_for(records)
                    )
                else:
                    server = _FixtureHTTPServer(
                        ("127.0.0.1", 0), _handler_for(outcome, records)
                    )
                host, port = server.server_address
                if host != "127.0.0.1":
                    server.server_close()
                    raise RuntimeError("Fixture listener was not loopback-bound.")
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self._servers.append(server)
                self._threads.append(thread)
                self._ports[handle] = port
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=1)
        self._servers.clear()
        self._threads.clear()
        self._ports.clear()
        self._records.clear()
        self._aliases.clear()
        self._handles.clear()
        self.cooldowns.clear()

    def _attempt(self, handle: _RouteHandle, attempt: int) -> str:
        if handle not in self._ports or handle not in self._aliases:
            raise RuntimeError("Route handle was not issued by this fixture.")
        alias = self._aliases[handle]
        if self.cooldowns.get(alias, 0) > self.now:
            raise RuntimeError(f"{alias} is cooling down")
        self.contact_count += 1
        request = (
            f"{_ABSOLUTE_REQUEST_LINE}\r\n"
            "Host: 127.0.0.1:9\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        response = bytearray()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.02)
                connection.connect(("127.0.0.1", self._ports[handle]))
                connection.sendall(request)
                while chunk := connection.recv(2_048):
                    response.extend(chunk)
        except TimeoutError:
            observed = "timeout"
        except (ConnectionError, OSError):
            observed = "connection_failure"
        else:
            first_line = bytes(response).partition(b"\r\n")[0]
            if first_line.startswith(b"HTTP/"):
                status = first_line.split(maxsplit=2)[1].decode("ascii")
                observed = (
                    "challenge" if b"fixture-challenge" in response else status
                )
            else:
                observed = "connection_failure"
        self.cooldowns[alias] = self.now + 5
        self.audit.append(
            {
                "attempt": attempt,
                "route": alias,
                "outcome": observed,
                "cooldownUntil": self.cooldowns[alias],
            }
        )
        return observed

    def run(self) -> dict[str, object]:
        self.now = 0
        for attempt, (alias, expected) in enumerate(self._SCRIPT, start=1):
            observed = self._attempt(self._handles[alias], attempt)
            expected_observation = "connection_failure" if expected == "disconnect" else expected
            if observed != expected_observation:
                raise AssertionError(f"fixture {alias} returned {observed}")
            self.now += 5
        self.audit.append(
            {
                "attempt": len(self._SCRIPT),
                "event": "terminal_failure",
                "reason": "fixture_retry_ceiling_reached",
            }
        )
        return {
            "attempts": len(self._SCRIPT),
            "terminal": True,
            "audit": list(self.audit),
        }


def test_synthetic_identity_lease_cap_cooldown_expiry_replacement_and_redaction() -> None:
    fixture = _IdentityFixture()
    sentinels: list[str] = []
    errors: list[str] = []
    with fixture:
        alpha = fixture.lease()
        assert alpha.alias == "fixture-alpha"
        sentinels.append(alpha.secret)
        beta = fixture.lease()
        assert beta.alias == "fixture-beta"
        sentinels.append(beta.secret)
        with pytest.raises(RuntimeError) as capped:
            fixture.lease()
        errors.append(str(capped.value))
        fixture.release("fixture-alpha")
        fixture.release("fixture-beta")
        with pytest.raises(RuntimeError) as cooling:
            fixture.lease()
        errors.append(str(cooling.value))

        fixture.now = 6
        replacement_alias = fixture.replace_expired_beta()
        replacement = fixture.lease()
        assert replacement.alias == replacement_alias
        sentinels.append(replacement.secret)

        fixture.now = 10
        assert fixture.lease().alias == "fixture-alpha"
        public_evidence = fixture.report() + repr(fixture.identities) + "".join(errors)
        if any(secret in public_evidence for secret in sentinels):
            pytest.fail("synthetic identity secret escaped redaction", pytrace=False)
        assert [event["event"] for event in fixture.audit] == [
            "leased",
            "leased",
            "released_to_cooldown",
            "released_to_cooldown",
            "expired_identity_replaced",
            "leased",
            "leased",
        ]

    if fixture.identities:
        pytest.fail("synthetic identities survived cleanup", pytrace=False)
    sentinels.clear()


def test_identity_fixture_rejects_production_x_before_secret_access(monkeypatch) -> None:
    accessed = False

    def forbidden_secret(_: int) -> str:
        nonlocal accessed
        accessed = True
        raise AssertionError("secret generator must remain unreachable")

    monkeypatch.setattr(secrets, "token_hex", forbidden_secret)
    with pytest.raises(RuntimeError, match="cannot activate"):
        _IdentityFixture(activation="production_x")
    assert not accessed


def test_loopback_route_transitions_are_bounded_audited_and_cleaned_up() -> None:
    cooldown_fixture = _RouteFixture()
    with cooldown_fixture:
        cooldown_threads = list(cooldown_fixture._threads)
        forbidden = cooldown_fixture._handles["route-forbidden"]
        assert cooldown_fixture._attempt(forbidden, 1) == "403"
        contacts = cooldown_fixture.contact_count
        with pytest.raises(RuntimeError, match="cooling down"):
            cooldown_fixture._attempt(forbidden, 2)
        assert cooldown_fixture.contact_count == contacts

    assert cooldown_fixture._ports == {}
    assert cooldown_fixture._handles == {}
    assert all(not thread.is_alive() for thread in cooldown_threads)

    fixture = _RouteFixture()
    with fixture:
        threads = list(fixture._threads)
        assert len(threads) == 5
        result = fixture.run()
        assert result["attempts"] == 5
        assert result["terminal"] is True
        assert [event.get("outcome") for event in fixture.audit[:-1]] == [
            "403",
            "429",
            "challenge",
            "timeout",
            "connection_failure",
        ]
        assert list(fixture.cooldowns.values()) == [5, 10, 15, 20, 25]
        assert all(
            fixture._records[fixture._handles[alias]] == [_ABSOLUTE_REQUEST_LINE]
            for alias, _ in fixture._SCRIPT
        )
        assert fixture.audit[-1] == {
            "attempt": 5,
            "event": "terminal_failure",
            "reason": "fixture_retry_ceiling_reached",
        }

    assert fixture._servers == []
    assert fixture._threads == []
    assert fixture._ports == {}
    assert fixture._records == {}
    assert fixture._aliases == {}
    assert fixture._handles == {}
    assert fixture.cooldowns == {}
    assert all(not thread.is_alive() for thread in threads)


def test_routes_reject_production_x_and_external_proxy_before_contact(monkeypatch) -> None:
    contacted = False
    fixture = _RouteFixture()

    def forbidden_socket(*_: Any, **__: Any) -> socket.socket:
        nonlocal contacted
        contacted = True
        raise AssertionError("network contact must remain unreachable")

    monkeypatch.setattr(socket, "socket", forbidden_socket)
    with pytest.raises(RuntimeError, match="cannot activate"):
        _RouteFixture(activation="production_x")
    with pytest.raises(TypeError):
        _RouteFixture(proxy_address="http://198.51.100.7:8080")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        _RouteHandle("http://198.51.100.7:8080")  # type: ignore[call-arg]
    with pytest.raises(RuntimeError, match="not issued"):
        fixture._attempt(_RouteHandle(), 1)
    assert fixture.contact_count == 0
    assert not contacted
    fixture.close()
