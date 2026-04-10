"""Tests that the SPA dashboard is served from web/dist."""

import os
from pathlib import Path

from fastapi.testclient import TestClient

from mcpxy_proxy.config import AppConfig
from mcpxy_proxy.proxy.bridge import ProxyBridge
from mcpxy_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcpxy_proxy.server import AppState, create_app
from mcpxy_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline

DIST_ROOT = Path(__file__).resolve().parent.parent / "src" / "mcpxy_proxy" / "web" / "dist"


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


def test_admin_index_is_public() -> None:
    # The SPA shell is intentionally public so the in-page LoginGate can
    # render and collect the bearer token. Every /admin/api/* endpoint
    # remains auth-gated.
    client = _client()
    res = client.get("/admin")
    assert res.status_code == 200
    body = res.text
    assert "<html" in body.lower() or "<!doctype" in body.lower()


def test_admin_index_serves_html_with_token() -> None:
    client = _client()
    res = client.get("/admin", headers={"Authorization": "Bearer secret"})
    assert res.status_code == 200
    body = res.text
    assert "<html" in body.lower() or "<!doctype" in body.lower()


def test_admin_spa_catch_all_serves_dashboard_on_sub_routes() -> None:
    if not (DIST_ROOT / "index.html").is_file():
        client = _client()
        res = client.get("/admin/traffic")
        assert res.status_code in (200, 404)
        return

    client = _client()
    # Sub-route is also public so deep links work without a pre-set token.
    res = client.get("/admin/traffic")
    assert res.status_code == 200
    assert "<html" in res.text.lower() or "<!doctype" in res.text.lower()


def test_admin_api_routes_are_not_caught_by_spa_route() -> None:
    client = _client()
    # /admin/api/config is a real JSON endpoint, not the SPA catch-all.
    res = client.get("/admin/api/config", headers={"Authorization": "Bearer secret"})
    assert res.status_code == 200
    assert res.headers.get("content-type", "").startswith("application/json")
