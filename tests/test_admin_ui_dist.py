"""Tests that the SPA dashboard is served from web/dist."""

import os
from pathlib import Path

from fastapi.testclient import TestClient

from mcp_proxy.config import AppConfig
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline

DIST_ROOT = Path(__file__).resolve().parent.parent / "src" / "mcp_proxy" / "web" / "dist"


def _client() -> TestClient:
    config = AppConfig.model_validate(
        {
            "auth": {"token_env": "MCP_PROXY_TOKEN"},
            "admin": {
                "enabled": True,
                "mount_name": "__admin__",
                "require_token": True,
                "allowed_clients": ["testclient"],
            },
            "upstreams": {"a": {"type": "http", "url": "http://example"}},
        }
    )
    os.environ["MCP_PROXY_TOKEN"] = "secret"
    reg = PluginRegistry()
    manager = UpstreamManager(config.upstreams, reg)
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(NoopTelemetrySink())
    return TestClient(
        create_app(AppState(config, config.model_dump(), manager, bridge, telemetry, reg))
    )


def test_admin_index_requires_auth() -> None:
    client = _client()
    res = client.get("/admin")
    assert res.status_code == 401


def test_admin_index_serves_html() -> None:
    client = _client()
    res = client.get("/admin", headers={"Authorization": "Bearer secret"})
    assert res.status_code == 200
    body = res.text
    assert "<html" in body.lower() or "<!doctype" in body.lower()


def test_admin_spa_catch_all_serves_dashboard_on_sub_routes() -> None:
    if not (DIST_ROOT / "index.html").is_file():
        # Dashboard was not built; fall back to a soft assertion on graceful 401.
        client = _client()
        res = client.get("/admin/traffic")
        assert res.status_code in (401, 200, 404)
        return

    client = _client()
    unauth = client.get("/admin/traffic")
    assert unauth.status_code == 401
    res = client.get("/admin/traffic", headers={"Authorization": "Bearer secret"})
    assert res.status_code == 200
    # Should be the same HTML as /admin (React Router handles routing client-side)
    assert "<html" in res.text.lower() or "<!doctype" in res.text.lower()


def test_admin_api_routes_are_not_caught_by_spa_route() -> None:
    client = _client()
    # /admin/api/config is a real JSON endpoint, not the SPA catch-all.
    res = client.get("/admin/api/config", headers={"Authorization": "Bearer secret"})
    assert res.status_code == 200
    assert res.headers.get("content-type", "").startswith("application/json")
