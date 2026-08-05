from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class RestClient:
    def __init__(self, base_url: str):
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        loopback = hostname == "localhost"
        if hostname and not loopback:
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = False
        if parsed.scheme != "http" or not loopback:
            raise ValueError("The MCP bridge only connects to a loopback HTTP dashboard.")
        self.base_url = base_url.rstrip("/")

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method=method,
        )
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", {}).get("message")
            except Exception:
                detail = None
            raise RuntimeError(detail or f"Dashboard returned HTTP {exc.code}.") from exc
        except URLError as exc:
            raise RuntimeError(
                "The xscraper dashboard is unavailable. Start it with: xscraper serve"
            ) from exc


def run_mcp(base_url: str = "http://127.0.0.1:5000") -> None:
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise SystemExit('Install MCP support with: pip install -e ".[mcp]"') from exc

    client = RestClient(base_url)
    server = MCPServer("xscraper")

    @server.tool()
    def preview_x_collection(request: dict[str, Any]) -> dict[str, Any]:
        """Preview an official X API collection and its maximum paid reads."""
        return client.call("POST", "/api/collections/preview", request)

    @server.tool()
    def start_x_collection(
        preview: dict[str, Any], confirm_paid_read: bool, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Start a previewed collection; paid-read confirmation must be explicit."""
        request = preview.get("request")
        compiled = preview.get("compiledRequest")
        if not isinstance(request, dict) or not isinstance(compiled, dict):
            raise ValueError("Pass the structured result from preview_x_collection.")
        return client.call(
            "POST",
            "/api/jobs",
            {
                **request,
                "compiledRequest": compiled,
                "confirmPaidRead": confirm_paid_read,
                "forceRefresh": force_refresh,
            },
        )

    @server.tool()
    def get_x_collection(job_id: str) -> dict[str, Any]:
        """Get collection state, counts, rate limit, retry, and cost metadata."""
        return client.call("GET", f"/api/jobs/{job_id}")

    @server.tool()
    def get_x_posts(job_id: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """Get a persisted page of collected public posts."""
        query = urlencode({"offset": offset, "limit": limit})
        return client.call("GET", f"/api/jobs/{job_id}/posts?{query}")

    @server.tool()
    def list_x_collections(limit: int = 25) -> dict[str, Any]:
        """List recent persisted collections."""
        return client.call("GET", f"/api/jobs?{urlencode({'limit': limit})}")

    server.run()
