from __future__ import annotations

import html
import inspect
import json
import logging
import os
import secrets
import stat
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.skipif(
    os.environ.get("XWORKBENCH_RUN_CAPABILITY_LAB") != "1",
    reason="the hard-isolated capability lab has a dedicated CI job",
)

_LOGGER = logging.getLogger("capability_lab.browser_session")
_COOKIE_NAME = "lab-session-sentinel"
_STORAGE_NAME = "lab-storage-sentinel"
_FINGERPRINT_PATH = "/fingerprint"
_SIGNIN_PATH = "/session/signin"
_APP_PATH = "/session/app"


class _LoopbackBrowserSessionFixture:
    def __init__(self) -> None:
        self.blocked: list[tuple[str, str]] = []
        self.requests: list[str] = []
        self._username = f"fixture-{secrets.token_hex(8)}"
        self._password = secrets.token_urlsafe(24)
        self._cookie_secret = secrets.token_urlsafe(32)
        self._storage_secret = secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self._username,
                self._password,
                self._cookie_secret,
                self._storage_secret,
            )
            if value
        )

    def __enter__(self) -> _LoopbackBrowserSessionFixture:
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                fixture.requests.append(self.path)
                path = urlsplit(self.path).path
                if path == _FINGERPRINT_PATH:
                    self._html(
                        """<!doctype html><meta charset=utf-8><body><script>
const result = {
  userAgent: navigator.userAgent,
  locale: navigator.language,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  viewport: `${innerWidth}x${innerHeight}`,
  fixtureSignal: globalThis.__capabilityLabSignal,
  platform: navigator.platform,
  vendor: navigator.vendor,
  hardwareConcurrency: navigator.hardwareConcurrency,
  webdriver: navigator.webdriver
};
document.body.textContent = JSON.stringify(result);
</script>"""
                    )
                    return
                if path == _SIGNIN_PATH:
                    self._html(
                        f"""<!doctype html><meta charset=utf-8>
<form method=post action={_SIGNIN_PATH!r}>
  <input name=username value={html.escape(fixture._username)!r}>
  <input name=password value={html.escape(fixture._password)!r}>
  <button>Sign in to synthetic fixture</button>
</form>"""
                    )
                    return
                if path == _APP_PATH:
                    expected = f"{_COOKIE_NAME}={fixture._cookie_secret}"
                    if expected not in self.headers.get("Cookie", ""):
                        self.send_error(401)
                        return
                    self._html("<!doctype html><title>Fixture app</title><p>authenticated</p>")
                    return
                if path == "/subresource":
                    self._html(
                        "<!doctype html><img id=subresource "
                        "src=https://outside.invalid/subresource>"
                    )
                    return
                if path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://outside.invalid/redirected")
                    self.end_headers()
                    return
                self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                fixture.requests.append(self.path)
                if urlsplit(self.path).path != _SIGNIN_PATH:
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                fields = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
                if fields != {
                    "username": [fixture._username],
                    "password": [fixture._password],
                }:
                    self.send_error(401)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header(
                    "Set-Cookie",
                    f"{_COOKIE_NAME}={fixture._cookie_secret}; HttpOnly; SameSite=Strict; Path=/",
                )
                self.end_headers()
                script_secret = json.dumps(fixture._storage_secret)
                self.wfile.write(
                    (
                        "<!doctype html><script>"
                        f"localStorage.setItem('{_STORAGE_NAME}', {script_secret});"
                        f"location.replace('{_APP_PATH}');"
                        "</script>"
                    ).encode()
                )

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _html(self, body: str) -> None:
                encoded = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        assert not self._thread.is_alive()
        assert self._server.fileno() == -1
        self._username = self._password = ""
        self._cookie_secret = self._storage_secret = ""


def _fixture_url_allowed(fixture: _LoopbackBrowserSessionFixture, url: str) -> bool:
    parsed = urlsplit(url)
    expected = urlsplit(fixture.origin)
    return (
        parsed.scheme in {"http", "ws"}
        and parsed.hostname == "127.0.0.1"
        and parsed.port == expected.port
        and parsed.username is None
        and parsed.password is None
    )


def _guard_context(context, fixture: _LoopbackBrowserSessionFixture) -> None:
    context.route("**/*", lambda route: _route_fixture_request(fixture, route))
    context.route_web_socket("**/*", lambda websocket: _route_fixture_websocket(fixture, websocket))
    context.set_default_timeout(3_000)
    context.set_default_navigation_timeout(3_000)


def _route_fixture_request(fixture: _LoopbackBrowserSessionFixture, route) -> None:
    if _fixture_url_allowed(fixture, route.request.url):
        response = route.fetch(max_redirects=0)
        redirect = response.headers.get("location") if 300 <= response.status < 400 else None
        if redirect and not _fixture_url_allowed(fixture, urljoin(route.request.url, redirect)):
            fixture.blocked.append(("redirect", urljoin(route.request.url, redirect)))
            route.abort("blockedbyclient")
        else:
            route.fulfill(response=response)
        response.dispose()
    else:
        fixture.blocked.append(("request", route.request.url))
        route.abort("blockedbyclient")


def _route_fixture_websocket(fixture: _LoopbackBrowserSessionFixture, websocket) -> None:
    if _fixture_url_allowed(fixture, websocket.url):
        websocket.connect_to_server()
    else:
        fixture.blocked.append(("websocket", websocket.url))
        # Routed WebSockets are disconnected unless connect_to_server() is called.


class _SyntheticWebSocketAttempt:
    def __init__(self, url: str) -> None:
        self.url = url
        self.connected = False

    def connect_to_server(self) -> None:
        self.connected = True


class _SyntheticDownloadAttempt:
    def __init__(self) -> None:
        self.request = type("SyntheticRequest", (), {"url": "https://outside.invalid/artifact"})()
        self.aborted_with: str | None = None

    def abort(self, reason: str) -> None:
        self.aborted_with = reason

@contextmanager
def _fingerprint_context(browser, fixture: _LoopbackBrowserSessionFixture, case: str):
    cases = {
        "ordinary-a": {
            "viewport": {"width": 800, "height": 600},
            "locale": "en-US",
            "timezone_id": "UTC",
            "user_agent": "CapabilityLab/ordinary-a",
            "signal": "fixture-a",
        },
        "ordinary-b": {
            "viewport": {"width": 1024, "height": 700},
            "locale": "en-GB",
            "timezone_id": "Europe/London",
            "user_agent": "CapabilityLab/ordinary-b",
            "signal": "fixture-b",
        },
    }
    selected = cases[case]
    options = {key: value for key, value in selected.items() if key != "signal"}
    context = browser.new_context(**options)
    _guard_context(context, fixture)
    context.add_init_script(f"globalThis.__capabilityLabSignal = {selected['signal']!r}")
    try:
        yield context
    finally:
        context.close()
        assert context.pages == []


def _collect_fingerprint(context, fixture: _LoopbackBrowserSessionFixture) -> dict[str, object]:
    page = context.new_page()
    page.goto(fixture.origin + _FINGERPRINT_PATH, wait_until="domcontentloaded")
    result = json.loads(page.locator("body").inner_text())
    page.close()
    assert page.is_closed()
    return result


def _fingerprint_report(browser, first: dict[str, object], second: dict[str, object]):
    ordinary = {"fixtureSignal", "locale", "timezone", "userAgent", "viewport"}
    lower = {"hardwareConcurrency", "platform", "vendor", "webdriver"}
    changed = {key for key in first if first[key] != second[key]}
    return {
        "changedOrdinaryObservables": sorted(changed & ordinary),
        "unchangedLowerBrowserTraits": sorted(key for key in lower if first[key] == second[key]),
        "browserEngineVersion": browser.version,
        "notImpersonated": [
            "browser engine version",
            "TLS ClientHello",
            "TLS cipher suite",
            "network stack",
        ],
        "transport": "plain HTTP on an ephemeral loopback listener; TLS was not used",
        "claim": "ordinary context controls only; no stealth or anti-detection patch",
    }


def _write_private_state(path: Path, state: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(state, stream, sort_keys=True)


def _validate_fixture_state(
    state: object, fixture: _LoopbackBrowserSessionFixture
) -> dict[str, object]:
    if not isinstance(state, dict) or set(state) != {"cookies", "origins"}:
        raise ValueError("invalid fixture storage-state schema")
    cookies = state["cookies"]
    origins = state["origins"]
    if not isinstance(cookies, list) or len(cookies) != 1:
        raise ValueError("fixture state must contain exactly one cookie")
    if not isinstance(origins, list) or len(origins) != 1:
        raise ValueError("fixture state must contain exactly one origin")
    cookie = cookies[0]
    origin = origins[0]
    expected_cookie_fields = {
        "name",
        "value",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
    }
    if not isinstance(cookie, dict) or set(cookie) != expected_cookie_fields:
        raise ValueError("fixture cookie schema changed")
    if (
        cookie["name"] != _COOKIE_NAME
        or cookie["value"] != fixture._cookie_secret
        or cookie["domain"] != "127.0.0.1"
        or cookie["path"] != "/"
        or cookie["httpOnly"] is not True
        or cookie["secure"] is not False
        or cookie["sameSite"] != "Strict"
        or cookie["expires"] != -1
    ):
        raise ValueError("fixture cookie escaped its sentinel boundary")
    if not isinstance(origin, dict) or set(origin) != {"origin", "localStorage"}:
        raise ValueError("fixture origin schema changed")
    if origin["origin"] != fixture.origin or origin["localStorage"] != [
        {"name": _STORAGE_NAME, "value": fixture._storage_secret}
    ]:
        raise ValueError("fixture local storage escaped its sentinel boundary")
    return state


def _redact(value: str, secret_values: tuple[str, ...]) -> str:
    for secret_value in secret_values:
        value = value.replace(secret_value, "[REDACTED]")
    return value


def _reject_external_session_material(*, cookie_path: Path | None = None, token: str | None = None):
    if cookie_path is not None or token is not None:
        raise ValueError("only the in-memory fixture sign-in may create lab session state")


def test_fingerprint_fixture_reports_exact_ordinary_changes_without_impersonation():
    with _LoopbackBrowserSessionFixture() as fixture, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            with _fingerprint_context(browser, fixture, "ordinary-a") as context:
                first = _collect_fingerprint(context, fixture)
            with _fingerprint_context(browser, fixture, "ordinary-b") as context:
                second = _collect_fingerprint(context, fixture)
            report = _fingerprint_report(browser, first, second)
        finally:
            browser.close()

    assert report["changedOrdinaryObservables"] == [
        "fixtureSignal",
        "locale",
        "timezone",
        "userAgent",
        "viewport",
    ]
    assert report["unchangedLowerBrowserTraits"] == [
        "hardwareConcurrency",
        "platform",
        "vendor",
        "webdriver",
    ]
    assert first["webdriver"] is True and second["webdriver"] is True
    assert report["notImpersonated"] == [
        "browser engine version",
        "TLS ClientHello",
        "TLS cipher suite",
        "network stack",
    ]
    assert report["transport"].startswith("plain HTTP")
    assert report["claim"] == "ordinary context controls only; no stealth or anti-detection patch"
    assert fixture.secrets == ()


def test_fingerprint_lab_cannot_be_activated_from_production_provider():
    import xworkbench.playwright_browser as production

    source = inspect.getsource(production)
    assert "capability_lab" not in source
    assert "__capabilityLabSignal" not in source
    assert not hasattr(production.PlaywrightBrowserProvider, "fingerprint_case")
    assert not hasattr(production.PlaywrightBrowserProvider, "impersonate")


def test_session_fixture_exports_private_state_replays_fresh_and_redacts_every_artifact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    artifact_dir = tmp_path / "browser-session-artifacts"
    artifact_dir.mkdir(mode=0o700)
    state_path = artifact_dir / "sentinel-state.json"
    report_path = artifact_dir / "sanitized-report.json"
    screenshot_path = artifact_dir / "authenticated.png"

    with _LoopbackBrowserSessionFixture() as fixture, sync_playwright() as playwright:
        secret_values = fixture.secrets
        browser = playwright.chromium.launch(headless=True)
        try:
            first = browser.new_context()
            _guard_context(first, fixture)
            page = first.new_page()
            page.goto(fixture.origin + _SIGNIN_PATH)
            page.get_by_role("button", name="Sign in to synthetic fixture").click()
            page.wait_for_url(fixture.origin + _APP_PATH)
            assert page.locator("body").inner_text() == "authenticated"
            state = _validate_fixture_state(first.storage_state(), fixture)
            _write_private_state(state_path, state)
            assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
            parsed = _validate_fixture_state(json.loads(state_path.read_text()), fixture)
            page.close()
            first.close()
            assert first.pages == []

            replay = browser.new_context(storage_state=parsed)
            _guard_context(replay, fixture)
            replay_page = replay.new_page()
            replay_page.goto(fixture.origin + _APP_PATH)
            assert replay_page.locator("body").inner_text() == "authenticated"
            storage_secret = replay_page.evaluate(f"localStorage.getItem('{_STORAGE_NAME}')")
            assert storage_secret == secret_values[3]
            state_path.unlink()

            sanitized = _redact(
                "signed in with " + " and ".join(secret_values),
                secret_values,
            )
            with caplog.at_level(logging.INFO, logger=_LOGGER.name):
                _LOGGER.info("%s", sanitized)
            report_path.write_text(json.dumps({"result": sanitized}), encoding="utf-8")
            replay_page.screenshot(path=str(screenshot_path))
            safe_error = RuntimeError(_redact("fixture failure " + secret_values[2], secret_values))
            assert str(safe_error) == "fixture failure [REDACTED]"
            assert replay_page.locator("html").inner_text().find(secret_values[2]) == -1
            assert all(
                secret.encode() not in path.read_bytes()
                for path in (report_path, screenshot_path)
                for secret in secret_values
            )
            assert all(secret not in caplog.text for secret in secret_values)
            assert sanitized in caplog.text
            replay_page.close()
            replay.close()
            assert replay.pages == []
        finally:
            browser.close()
            assert not browser.is_connected()

    for path in (report_path, screenshot_path):
        path.unlink()
    artifact_dir.rmdir()
    assert not artifact_dir.exists()
    assert fixture.secrets == ()


def test_session_lab_rejects_real_cookie_paths_tokens_and_production_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    touched: list[Path] = []
    real_profile = tmp_path / "Default" / "Cookies"

    def forbidden_read(path: Path, *_args: object, **_kwargs: object):
        touched.append(path)
        raise AssertionError("external cookie path was accessed")

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    with pytest.raises(ValueError, match="in-memory fixture sign-in"):
        _reject_external_session_material(cookie_path=real_profile)
    with pytest.raises(ValueError, match="in-memory fixture sign-in"):
        _reject_external_session_material(token="pasted-real-token")
    assert touched == []

    import xworkbench.playwright_browser as production

    source = inspect.getsource(production)
    assert "tests.capability_lab" not in source
    assert "lab-session-sentinel" not in source
    assert not hasattr(production.PlaywrightBrowserProvider, "import_fixture_session")
    assert not hasattr(production.PlaywrightBrowserProvider, "export_fixture_session")
    assert all(
        not name.startswith("tests.capability_lab") for name in sys.modules if name != __name__
    )


def test_browser_guards_external_url_redirect_dns_subresource_websocket_download_and_proxy():
    with _LoopbackBrowserSessionFixture() as fixture, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        _guard_context(context, fixture)
        page = context.new_page()
        try:
            for blocked_url in (
                "https://outside.invalid/external-url",
                "http://outside.invalid/dns-lookup",
                "http://198.51.100.7:1/raw-socket",
            ):
                with pytest.raises(PlaywrightError):
                    page.goto(blocked_url)
                page.close()
                page = context.new_page()
            with pytest.raises(PlaywrightError):
                page.goto(fixture.origin + "/redirect")
            page.close()
            page = context.new_page()
            page.goto(fixture.origin + "/subresource")
            assert any(url.endswith("/subresource") for _, url in fixture.blocked)

            websocket_attempt = _SyntheticWebSocketAttempt("ws://outside.invalid/socket")
            _route_fixture_websocket(fixture, websocket_attempt)
            assert websocket_attempt.connected is False

            download_attempt = _SyntheticDownloadAttempt()
            _route_fixture_request(fixture, download_attempt)
            assert download_attempt.aborted_with == "blockedbyclient"
            with pytest.raises(TypeError, match="unexpected keyword"):
                with _fingerprint_context(
                    browser, fixture, "ordinary-a", proxy="http://127.0.0.1:9"
                ):
                    pass
        finally:
            page.close()
            context.close()
            browser.close()
        assert context.pages == []
        assert not browser.is_connected()

    blocked = fixture.blocked
    assert any(url == "https://outside.invalid/external-url" for _, url in blocked)
    assert any(url == "http://outside.invalid/dns-lookup" for _, url in blocked)
    assert any(url == "http://198.51.100.7:1/raw-socket" for _, url in blocked)
    assert any(url == "https://outside.invalid/redirected" for _, url in blocked)
    assert any(url == "https://outside.invalid/subresource" for _, url in blocked)
    assert any(
        kind == "websocket" and url == "ws://outside.invalid/socket" for kind, url in blocked
    )
    assert any(url == "https://outside.invalid/artifact" for _, url in blocked)
    assert all("outside.invalid" not in request for request in fixture.requests)
    assert fixture.secrets == ()
