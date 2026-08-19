from __future__ import annotations

import ast
import errno
import importlib
import io
import os
import socket
import subprocess
import sys
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from zipfile import ZipFile

import pytest

from xworkbench.config import Settings, SettingsError
from xworkbench.errors import CredentialError, InvalidRequestError, RateLimitWaiting
from xworkbench.mcp_server import RestClient
from xworkbench.models import CollectionRequest
from xworkbench.playwright_browser import (
    BrowserManualActionRequired,
    BrowserRateLimitedError,
    BrowserSessionExpiredError,
    PlaywrightBrowserProvider,
    _record_status,
)
from xworkbench.providers import ProviderRegistry
from xworkbench.x_api import RECENT_ENDPOINT, XApiProvider

LAB_ENABLED = os.environ.get("XWORKBENCH_RUN_CAPABILITY_LAB") == "1"
pytestmark = pytest.mark.skipif(
    not LAB_ENABLED,
    reason="set XWORKBENCH_RUN_CAPABILITY_LAB=1 in the isolated lab job",
)
REPOSITORY = Path(__file__).resolve().parents[2]


def test_external_inputs_fail_before_file_or_network_access(tmp_path, monkeypatch):
    cookie_path = tmp_path / "installed-browser-profile" / "Cookies"
    cookie_path.parent.mkdir()
    cookie_path.write_bytes(b"MUST_NOT_BE_READ")
    contacts: list[tuple[str, object]] = []

    def deny(name):
        def denied(*args, **_kwargs):
            contacts.append((name, args))
            raise AssertionError(f"unexpected access through {name}")

        return denied

    monkeypatch.setattr(socket, "getaddrinfo", deny("dns"))
    monkeypatch.setattr(socket, "create_connection", deny("connection"))
    monkeypatch.setattr(socket.socket, "connect", deny("socket"))
    monkeypatch.setattr(Path, "read_text", deny("file"))

    with pytest.raises(InvalidRequestError):
        CollectionRequest.from_dict(
            {
                "provider": "playwright_browser",
                "sourceType": "profile",
                "sourceValue": "https://example.invalid/real-account",
                "maxPosts": 1,
            }
        )

    base = {
        "provider": "playwright_browser",
        "sourceType": "home",
        "maxPosts": 1,
    }
    attempts = {
        "redirectUrl": "https://example.invalid/redirect",
        "dnsHostname": "example.invalid",
        "proxyAddress": "http://198.51.100.7:8080",
        "cookiePath": str(cookie_path),
        "pastedToken": "PASTED_TOKEN_SENTINEL",
    }
    for field, value in attempts.items():
        with pytest.raises(InvalidRequestError, match=field):
            CollectionRequest.from_dict({**base, field: value})

    runtime = tmp_path / "runtime"
    with pytest.raises(SettingsError, match="app-owned auth directory"):
        Settings(
            runtime / "workbench.db",
            runtime / "auth" / "token",
            config_path=runtime / "config.json",
            storage_state_path=cookie_path,
        )

    with pytest.raises(ValueError, match="loopback"):
        RestClient("http://example.invalid:8765")

    assert contacts == []
    assert cookie_path.read_bytes() == b"MUST_NOT_BE_READ"


def test_redirect_is_rejected_after_one_local_request_without_following_external_target():
    opened: list[str] = []

    class Redirected:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return "https://example.invalid/redirected"

        def read(self):
            raise AssertionError("redirected content must not be read")

    def opener(request, timeout):
        assert timeout == 10
        opened.append(request.full_url)
        return Redirected()

    client = RestClient("http://127.0.0.1:8765", opener=opener)
    with pytest.raises(RuntimeError, match="redirected"):
        client.get("/api/health")

    assert opened == ["http://127.0.0.1:8765/api/health"]


@pytest.mark.parametrize(
    ("mechanism", "field"),
    [
        ("fingerprint controls", "fingerprintProfile"),
        ("session replay", "storageStateImport"),
        ("GraphQL replay", "graphqlOperation"),
        ("identity leasing", "identityPool"),
        ("challenge solving", "challengeSolver"),
        ("route transitions", "proxyRoutes"),
    ],
)
def test_production_request_and_provider_cannot_activate_lab_mechanisms(mechanism, field):
    body = {
        "provider": "playwright_browser",
        "sourceType": "home",
        "maxPosts": 1,
        field: {"activation": "production_x"},
    }
    with pytest.raises(InvalidRequestError, match=field):
        CollectionRequest.from_dict(body)

    class LabProvider:
        provider_id = f"capability_lab:{mechanism}"
        provider_version = 1

    with pytest.raises(InvalidRequestError, match="Unknown collection provider"):
        ProviderRegistry([LabProvider()])


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, CredentialError), (403, CredentialError), (429, RateLimitWaiting)],
)
def test_production_api_rejections_stop_once_without_changing_route_or_token(
    tmp_path, status, expected
):
    token_path = tmp_path / "auth" / "token"
    token_path.parent.mkdir(mode=0o700)
    token_path.write_text("SYNTHETIC_OFFICIAL_TOKEN", encoding="utf-8")
    token_path.chmod(0o600)
    configured = Settings(
        tmp_path / "workbench.db",
        token_path,
        allow_environment_token=False,
    )
    calls: list[tuple[str, int]] = []

    def rejected(request, timeout):
        calls.append((request.full_url, timeout))
        headers = Message()
        if status == 429:
            headers["x-rate-limit-reset"] = "4102444800"
            headers["x-rate-limit-remaining"] = "0"
        raise HTTPError(
            request.full_url,
            status,
            "synthetic rejection",
            headers,
            io.BytesIO(b"fixture rejection"),
        )

    provider = XApiProvider(
        configured,
        opener=rejected,
        sleeper=lambda _seconds: (_ for _ in ()).throw(
            AssertionError("terminal rejection must not retry")
        ),
    )
    with pytest.raises(expected):
        provider._request({"query": "fixture"})

    assert len(calls) == 1
    assert calls[0][0].startswith(f"{RECENT_ENDPOINT}?")
    assert calls[0][1] == 30
    assert token_path.read_text(encoding="utf-8") == "SYNTHETIC_OFFICIAL_TOKEN"
    assert configured.public_dict()["route_mode"] == "direct"


class _Page:
    def __init__(self, *, url: str, body: str = "") -> None:
        self.url = url
        self.body = body

    def locator(self, selector):
        page = self

        class Locator:
            def count(self):
                return 0

            def evaluate(self, _script):
                return page.body if selector == "body" else ""

        return Locator()


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        (_Page(url="https://x.com/i/flow/login"), BrowserSessionExpiredError),
        (_Page(url="https://x.com/account/access"), BrowserManualActionRequired),
        (_Page(url="https://x.com/home", body="Rate limit exceeded"), BrowserRateLimitedError),
    ],
)
def test_production_browser_login_challenge_and_rate_limit_preserve_session(
    tmp_path, page, expected
):
    runtime = tmp_path / "runtime"
    auth = runtime / "auth"
    auth.mkdir(parents=True, mode=0o700)
    state_path = auth / "state.json"
    state_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    state_path.chmod(0o600)
    configured = Settings(
        runtime / "workbench.db",
        auth / "token",
        config_path=runtime / "config.json",
        storage_state_path=state_path,
    )
    assert _record_status(configured, "verified_live")
    before = state_path.read_bytes()
    provider = PlaywrightBrowserProvider(configured)

    with pytest.raises(expected):
        provider._raise_page_failure(page)

    assert state_path.read_bytes() == before
    assert provider.settings is configured
    assert configured.public_dict()["route_mode"] == "direct"
    assert "proxy" not in provider.capabilities()


def test_production_import_graph_has_no_lab_path_or_activation_surface():
    xworkbench = importlib.import_module("xworkbench")
    production_root = Path(xworkbench.__file__).parent
    forbidden = []
    for path in production_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "tests" or name.startswith("tests.") for name in names):
                forbidden.append((path.name, names))
        if "capability_lab" in source:
            forbidden.append((path.name, ["capability_lab activation string"]))

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("xworkbench.capability_lab")

    assert forbidden == []
    assert not any(name.startswith("xworkbench.capability_lab") for name in sys.modules)


def test_built_wheel_excludes_lab_and_all_tests():
    wheels = list(Path.home().glob("x_collection_workbench-*.whl"))
    if not wheels:
        wheels = list((REPOSITORY / "dist").glob("x_collection_workbench-*.whl"))
    assert len(wheels) == 1
    with ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()

    assert any(name == "xworkbench/__init__.py" for name in names)
    assert not any("capability_lab" in name for name in names)
    assert not any(name.startswith("tests/") for name in names)


@pytest.mark.skipif(sys.platform != "linux", reason="CI network namespaces are Linux-only")
def test_ci_process_has_only_loopback_and_no_external_route():
    interfaces = {name for _index, name in socket.if_nameindex()}
    assert interfaces == {"lo"}, "capability lab must run under unshare --net"
    routes = Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]
    assert not any(line.split()[1] == "00000000" for line in routes if line.split())

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        result = probe.connect_ex(("192.0.2.1", 443))
    assert result in {errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH}
    status = Path("/proc/self/status").read_text(encoding="ascii")
    assert os.geteuid() != 0
    assert os.getppid() == 1
    assert set(status.split("Uid:\t", 1)[1].splitlines()[0].split()) == {str(os.geteuid())}
    assert "NoNewPrivs:\t1" in status
    assert "CapEff:\t0000000000000000" in status
    assert not {
        "GITHUB_TOKEN",
        "XWORKBENCH_X_BEARER_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
    } & os.environ.keys()
    persisted_credentials = subprocess.run(
        [
            "git",
            "config",
            "--local",
            "--get-regexp",
            r"^(http\..*\.extraheader|include.*\.path)$",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=2,
    )
    if persisted_credentials.returncode != 1:
        pytest.fail("repository credentials are visible to the capability lab", pytrace=False)
    elevation = subprocess.run(
        ["sudo", "-n", "true"],
        capture_output=True,
        check=False,
        timeout=2,
    )
    assert elevation.returncode != 0
