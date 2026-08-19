from __future__ import annotations

import html
import inspect
import json
import logging
import os
import secrets
import stat
import subprocess
import sys
import threading
import time
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
_SUBRESOURCE_PATH = "/subresource"
_DOWNLOAD_PATH = "/download"
_SECRET_REDIRECT_PATH = "/secret-redirect"
_HTTP_PATHS = {
    _FINGERPRINT_PATH,
    _SIGNIN_PATH,
    _APP_PATH,
    _SUBRESOURCE_PATH,
    _DOWNLOAD_PATH,
    _SECRET_REDIRECT_PATH,
}


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
        self._browser = None
        self._replay_state: dict[str, object] | None = None
        self.contexts_created = 0
        self.websocket_blocked = threading.Event()

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
                parsed = urlsplit(self.path)
                path = parsed.path
                if parsed.query or parsed.fragment or path not in _HTTP_PATHS:
                    fixture.requests.append("[rejected-path]")
                    self.send_error(400)
                    return
                fixture.requests.append(path)
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
                if path == _SUBRESOURCE_PATH:
                    self._html(
                        "<!doctype html><img id=subresource "
                        "src=https://outside.invalid/subresource>"
                    )
                    return
                if path == _DOWNLOAD_PATH:
                    self._html(
                        "<!doctype html><a id=download download "
                        "href=https://outside.invalid/artifact>"
                        "download synthetic file</a>"
                    )
                    return
                if path == _SECRET_REDIRECT_PATH:
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        f"https://outside.invalid/blocked?token={fixture._cookie_secret}",
                    )
                    self.end_headers()
                    return
                self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                if parsed.query or parsed.fragment or parsed.path != _SIGNIN_PATH:
                    fixture.requests.append("[rejected-path]")
                    self.send_error(400)
                    return
                fixture.requests.append(parsed.path)
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

    def launch_browser(self, playwright):
        parsed = urlsplit(self.origin)
        assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
        if self._browser is not None:
            raise RuntimeError("the fixture already owns a browser")
        self._browser = playwright.chromium.launch(headless=True)
        return self._browser

    def close_browser(self, browser) -> None:
        if browser is not self._browser:
            raise ValueError("only the fixture-owned browser may be closed")
        try:
            browser.close()
        finally:
            self._browser = None

    def ordinary_a_context(self, browser):
        return self._new_context(browser, "ordinary-a")

    def ordinary_b_context(self, browser):
        return self._new_context(browser, "ordinary-b")

    def session_context(self, browser):
        return self._new_context(browser, "session")

    def set_replay_state(self, state: dict[str, object]) -> None:
        self._replay_state = _validate_fixture_state(state, self)

    def replay_context(self, browser):
        assert self._replay_state is not None
        return self._new_context(browser, "replay")

    def boundary_context(self, browser):
        return self._new_context(browser, "boundary")

    def _new_context(self, browser, mode: str):
        if browser is not self._browser:
            raise ValueError("only the fixture-owned browser may create a context")
        options = {
            "ordinary-a": {
                "viewport": {"width": 800, "height": 600},
                "locale": "en-US",
                "timezone_id": "UTC",
                "user_agent": "CapabilityLab/ordinary-a",
            },
            "ordinary-b": {
                "viewport": {"width": 1024, "height": 700},
                "locale": "en-GB",
                "timezone_id": "Europe/London",
                "user_agent": "CapabilityLab/ordinary-b",
            },
            "session": {},
            "replay": {"storage_state": self._replay_state},
            "boundary": {"accept_downloads": True},
        }[mode]
        self.contexts_created += 1
        context = browser.new_context(**options)
        context.route("**/*", lambda route: _route_fixture_request(self, route))
        context.route_web_socket(
            "**/*", lambda websocket: _route_fixture_websocket(self, websocket)
        )
        context.set_default_timeout(2_000)
        context.set_default_navigation_timeout(2_000)
        signal = {"ordinary-a": "fixture-a", "ordinary-b": "fixture-b"}.get(mode)
        if signal:
            context.add_init_script(f"globalThis.__capabilityLabSignal = {signal!r}")
        context.add_init_script(
            """addEventListener('click', event => {
              const download = event.composedPath().find(
                node => node?.matches?.('a[download]')
              );
              if (!download) return;
              event.preventDefault();
              event.stopImmediatePropagation();
              globalThis.__fixtureDownloadBlocked = true;
            }, true);"""
        )
        return context

    def __exit__(self, *_args: object) -> None:
        assert self._server is not None and self._thread is not None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        assert not self._thread.is_alive()
        assert self._server.fileno() == -1
        self._username = self._password = ""
        self._cookie_secret = self._storage_secret = ""
        self._replay_state = None


def _fixture_http_url_allowed(fixture: _LoopbackBrowserSessionFixture, url: str) -> bool:
    parsed = urlsplit(url)
    expected = urlsplit(fixture.origin)
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.port == expected.port
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in _HTTP_PATHS
    )


def _route_fixture_request(fixture: _LoopbackBrowserSessionFixture, route) -> None:
    if _fixture_http_url_allowed(fixture, route.request.url):
        response = route.fetch(max_redirects=0)
        redirect = response.headers.get("location") if 300 <= response.status < 400 else None
        if redirect and not _fixture_http_url_allowed(
            fixture, urljoin(route.request.url, redirect)
        ):
            source_path = urlsplit(route.request.url).path
            fixture.blocked.append(("redirect", source_path))
            route.abort("blockedbyclient")
        else:
            route.fulfill(response=response)
        response.dispose()
    else:
        parsed = urlsplit(route.request.url)
        if parsed.hostname != "127.0.0.1":
            safe_target = "[external-target]"
        elif parsed.path in _HTTP_PATHS:
            safe_target = parsed.path
        else:
            safe_target = "[invalid-local-target]"
        fixture.blocked.append(("request", safe_target))
        route.abort("blockedbyclient")


def _route_fixture_websocket(fixture: _LoopbackBrowserSessionFixture, websocket) -> None:
    fixture.blocked.append(("websocket", "[external-target]"))
    fixture.websocket_blocked.set()
    # Routed WebSockets are disconnected unless connect_to_server() is called.


def _chromium_processes() -> set[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,comm="],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    chromium = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3:
            continue
        command = parts[2].lower()
        if "chrom" in command or "headless_shell" in command:
            chromium.add(int(parts[0]))
    return chromium


@contextmanager
def _fixture_browser(fixture: _LoopbackBrowserSessionFixture, playwright):
    baseline = _chromium_processes()
    browser = fixture.launch_browser(playwright)
    assert _chromium_processes() - baseline, "the real Chromium process was not observed"
    try:
        yield browser
    finally:
        fixture.close_browser(browser)
        assert not browser.is_connected()
        deadline = time.monotonic() + 3
        while _chromium_processes() - baseline and time.monotonic() < deadline:
            time.sleep(0.05)
        if _chromium_processes() - baseline:
            raise AssertionError("Chromium processes survived fixture cleanup")


@contextmanager
def _private_artifacts(tmp_path: Path):
    directory = tmp_path / "browser-session-artifacts"
    directory.mkdir(mode=0o700)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    paths = {
        "state": directory / "sentinel-state.json",
        "report": directory / "sanitized-report.json",
        "screenshot": directory / "authenticated.png",
    }
    try:
        yield paths
    finally:
        for path in paths.values():
            path.unlink(missing_ok=True)
        directory.rmdir()


def _safe_browser_error(_error: BaseException) -> dict[str, str]:
    return {"type": "blocked_navigation", "message": "loopback fixture rejected the target"}


def _require_secret_match(actual: object, expected: str) -> None:
    if not isinstance(actual, str) or not secrets.compare_digest(actual, expected):
        raise AssertionError("sentinel replay did not match")


def _require_redacted(value: str | bytes, secret_values: tuple[str, ...]) -> None:
    encoded = value if isinstance(value, bytes) else value.encode()
    if any(secret_value.encode() in encoded for secret_value in secret_values):
        raise AssertionError("sentinel escaped a redacted boundary")


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
        with _fixture_browser(fixture, playwright) as browser:
            first_context = fixture.ordinary_a_context(browser)
            first_page = first_context.new_page()
            first_page.goto(fixture.origin + _FINGERPRINT_PATH, wait_until="domcontentloaded")
            first = json.loads(first_page.locator("body").inner_text())
            first_page.close()
            first_context.close()
            assert first_context.pages == []

            second_context = fixture.ordinary_b_context(browser)
            second_page = second_context.new_page()
            second_page.goto(fixture.origin + _FINGERPRINT_PATH, wait_until="domcontentloaded")
            second = json.loads(second_page.locator("body").inner_text())
            report = _fingerprint_report(browser, first, second)
            second_page.close()
            second_context.close()
            assert second_context.pages == []

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
    with _private_artifacts(tmp_path) as artifacts:
        with _LoopbackBrowserSessionFixture() as fixture, sync_playwright() as playwright:
            secret_values = fixture.secrets
            with _fixture_browser(fixture, playwright) as browser:
                first = fixture.session_context(browser)
                replay = None
                try:
                    page = first.new_page()
                    page.goto(fixture.origin + _SIGNIN_PATH)
                    page.get_by_role("button", name="Sign in to synthetic fixture").click()
                    page.wait_for_url(fixture.origin + _APP_PATH)
                    assert page.locator("body").inner_text() == "authenticated"
                    state = _validate_fixture_state(first.storage_state(), fixture)
                    _write_private_state(artifacts["state"], state)
                    assert stat.S_IMODE(artifacts["state"].stat().st_mode) == 0o600
                    parsed = _validate_fixture_state(
                        json.loads(artifacts["state"].read_text()), fixture
                    )
                    page.close()
                    first.close()
                    assert first.pages == []
                    first = None

                    fixture.set_replay_state(parsed)
                    replay = fixture.replay_context(browser)
                    replay_page = replay.new_page()
                    replay_page.goto(fixture.origin + _APP_PATH)
                    assert replay_page.locator("body").inner_text() == "authenticated"
                    storage_secret = replay_page.evaluate(
                        f"localStorage.getItem('{_STORAGE_NAME}')"
                    )
                    _require_secret_match(storage_secret, secret_values[3])
                    artifacts["state"].unlink()

                    sanitized = _redact(
                        "signed in with " + " and ".join(secret_values),
                        secret_values,
                    )
                    with caplog.at_level(logging.INFO, logger=_LOGGER.name):
                        _LOGGER.info("%s", sanitized)
                    artifacts["report"].write_text(
                        json.dumps({"result": sanitized}), encoding="utf-8"
                    )
                    replay_page.evaluate(
                        "secret => document.body.textContent = secret", secret_values[2]
                    )
                    _require_secret_match(
                        replay_page.locator("body").inner_text(), secret_values[2]
                    )
                    replay_page.evaluate("document.body.textContent = '[REDACTED]'")
                    assert replay_page.locator("body").inner_text() == "[REDACTED]"
                    replay_page.screenshot(path=str(artifacts["screenshot"]))
                    for path in (artifacts["report"], artifacts["screenshot"]):
                        _require_redacted(path.read_bytes(), secret_values)
                    _require_redacted(caplog.text, secret_values)
                    assert sanitized in caplog.text
                    replay_page.close()
                    replay.close()
                    assert replay.pages == []
                    replay = None
                finally:
                    if replay is not None:
                        replay.close()
                    if first is not None:
                        first.close()

    assert not artifact_dir.exists()
    assert fixture.secrets == ()


def test_private_session_artifacts_are_removed_after_injected_failure(tmp_path: Path):
    directory = tmp_path / "browser-session-artifacts"
    with pytest.raises(RuntimeError, match="injected fixture failure"):
        with _private_artifacts(tmp_path) as artifacts:
            _write_private_state(artifacts["state"], {"sentinel": "secret"})
            assert stat.S_IMODE(artifacts["state"].stat().st_mode) == 0o600
            artifacts["report"].write_text("redacted", encoding="utf-8")
            artifacts["screenshot"].write_bytes(b"synthetic screenshot")
            raise RuntimeError("injected fixture failure")
    assert not directory.exists()


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
        with _fixture_browser(fixture, playwright) as browser:
            context = fixture.boundary_context(browser)
            page = context.new_page()
            try:
                safe_errors = []
                for blocked_url in (
                    "https://outside.invalid/external-url",
                    "http://outside.invalid/dns-lookup",
                    "http://198.51.100.7:1/raw-socket",
                ):
                    with pytest.raises(PlaywrightError) as blocked_error:
                        page.goto(blocked_url)
                    safe_errors.append(_safe_browser_error(blocked_error.value))
                    page.close()
                    page = context.new_page()

                requests_before = list(fixture.requests)
                assert not _fixture_http_url_allowed(
                    fixture, fixture.origin + _FINGERPRINT_PATH + "#not-allowed"
                )
                with pytest.raises(PlaywrightError):
                    page.goto(fixture.origin + _FINGERPRINT_PATH + "?not-allowed=1")
                assert fixture.requests == requests_before
                page.close()
                page = context.new_page()

                with pytest.raises(PlaywrightError):
                    page.goto(fixture.origin + "/unknown?not-allowed=1")
                assert fixture.requests == requests_before
                page.close()
                page = context.new_page()

                secret = fixture._cookie_secret
                with pytest.raises(PlaywrightError) as redirect_error:
                    page.goto(fixture.origin + _SECRET_REDIRECT_PATH)
                safe_errors.append(_safe_browser_error(redirect_error.value))
                _require_redacted(str(redirect_error.value), (secret,))
                _require_redacted(json.dumps(fixture.blocked), (secret,))
                _require_redacted(json.dumps(safe_errors), (secret,))
                page.close()
                page = context.new_page()

                page.goto(fixture.origin + _SUBRESOURCE_PATH)
                assert ("request", "[external-target]") in fixture.blocked
                page.goto(fixture.origin + _DOWNLOAD_PATH)
                requests_before = list(fixture.requests)
                blocks_before = list(fixture.blocked)
                page.locator("#download").click(no_wait_after=True, timeout=1_000)
                page.wait_for_function(
                    "globalThis.__fixtureDownloadBlocked === true", timeout=1_000
                )
                assert fixture.requests == requests_before
                assert fixture.blocked == blocks_before

                contexts_before = fixture.contexts_created
                requests_before = list(fixture.requests)
                blocks_before = list(fixture.blocked)
                with pytest.raises(TypeError, match="unexpected keyword"):
                    fixture.boundary_context(browser, proxy="http://127.0.0.1:9")
                assert fixture.contexts_created == contexts_before
                assert fixture.requests == requests_before
                assert fixture.blocked == blocks_before

                class ForgedProxyBrowser:
                    proxy = "http://127.0.0.1:9"
                    new_context_calls = 0

                    def new_context(self, **_options):
                        self.new_context_calls += 1
                        raise AssertionError("forged browser created a context")

                forged_browser = ForgedProxyBrowser()
                with pytest.raises(ValueError, match="fixture-owned browser"):
                    fixture.boundary_context(forged_browser)
                assert forged_browser.new_context_calls == 0
                assert fixture.contexts_created == contexts_before
                assert fixture.requests == requests_before
                assert fixture.blocked == blocks_before
            finally:
                page.close()
                context.close()
            assert context.pages == []

            websocket_context = fixture.boundary_context(browser)
            websocket_page = websocket_context.new_page()
            try:
                websocket_page.goto(fixture.origin + _FINGERPRINT_PATH)
                websocket_result = websocket_page.evaluate(
                    """() => {
                      globalThis.__fixtureSocket = new WebSocket('ws://outside.invalid/socket');
                      return globalThis.__fixtureSocket.url;
                    }"""
                )
                assert websocket_result == "ws://outside.invalid/socket"
                assert fixture.websocket_blocked.wait(timeout=1)
            finally:
                websocket_page.close()
                websocket_context.close()
            assert websocket_context.pages == []

    blocked = fixture.blocked
    assert blocked.count(("request", "[external-target]")) >= 4
    assert ("request", "[invalid-local-target]") in blocked
    assert ("redirect", _SECRET_REDIRECT_PATH) in blocked
    assert ("websocket", "[external-target]") in blocked
    assert all(request in _HTTP_PATHS for request in fixture.requests)
    assert fixture.secrets == ()
