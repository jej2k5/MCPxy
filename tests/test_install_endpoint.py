"""Tests for the /admin/api/install/{client} endpoint."""

import os

from fastapi.testclient import TestClient

from mcpxy_proxy.config import AppConfig
from mcpxy_proxy.proxy.bridge import ProxyBridge
from mcpxy_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcpxy_proxy.server import AppState, create_app
from mcpxy_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline


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


HEADERS = {"Authorization": "Bearer secret"}


def test_install_clients_lists_known() -> None:
    client = _client()
    res = client.get("/admin/api/install/clients", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert "claude-desktop" in body["clients"]
    assert "claude-code" in body["clients"]
    assert "chatgpt" in body["clients"]


def test_install_snippet_for_claude_desktop_uses_stdio_adapter() -> None:
    client = _client()
    res = client.get(
        "/admin/api/install/claude-desktop?url=http://h:9000",
        headers=HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["client"] == "claude-desktop"
    assert body["supports_auto_install"] is True
    entry = body["entry"]
    assert entry["command"] == "mcpxy-proxy"
    assert entry["args"][0] == "stdio"
    assert "http://h:9000" in entry["args"]


def test_install_snippet_for_claude_code_returns_http_entry() -> None:
    client = _client()
    res = client.get("/admin/api/install/claude-code?url=http://h:9000", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    entry = body["entry"]
    assert entry["type"] == "http"
    assert entry["url"].startswith("http://h:9000")


def test_install_snippet_for_chatgpt_marks_no_auto_install() -> None:
    client = _client()
    res = client.get("/admin/api/install/chatgpt?url=http://h:9000", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["supports_auto_install"] is False


def test_install_snippet_unknown_client_404() -> None:
    client = _client()
    res = client.get("/admin/api/install/unknown", headers=HEADERS)
    assert res.status_code == 404


def test_install_endpoint_requires_auth() -> None:
    client = _client()
    res = client.get("/admin/api/install/claude-desktop")
    assert res.status_code == 401
