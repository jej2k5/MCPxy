import os

from fastapi.testclient import TestClient

from mcp_proxy.config import AppConfig
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


def _build_client(require_token: bool = True):
    config = AppConfig.model_validate(
        {
            "auth": {"token_env": "MCP_PROXY_TOKEN"},
            "admin": {"enabled": True, "mount_name": "__admin__", "require_token": require_token, "allowed_clients": ["testclient"]},
            "upstreams": {},
        }
    )
    os.environ["MCP_PROXY_TOKEN"] = "secret"
    manager = UpstreamManager({}, PluginRegistry())
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(NoopTelemetrySink())
    app = create_app(AppState(config, config.model_dump(), manager, bridge, telemetry, PluginRegistry()))
    return TestClient(app)


def test_admin_requires_token() -> None:
    client = _build_client()
    payload = {"jsonrpc": "2.0", "id": 1, "method": "admin.get_health", "params": {}}
    response = client.post("/mcp/__admin__", json=payload)
    assert response.status_code == 401

    response = client.post("/mcp/__admin__", json=payload, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200


def test_admin_require_token_fails_when_missing_expected_token() -> None:
    client = _build_client()
    del os.environ["MCP_PROXY_TOKEN"]

    payload = {"jsonrpc": "2.0", "id": 1, "method": "admin.get_health", "params": {}}
    response = client.post("/mcp/__admin__", json=payload, headers={"Authorization": "Bearer secret"})

    assert response.status_code == 500
    assert response.json()["detail"] == "admin_token_not_configured"
    assert "authorization" not in response.text.lower()
    assert "bearer" not in response.text.lower()


def test_admin_allowlist_is_still_enforced_without_token_check() -> None:
    config = AppConfig.model_validate(
        {
            "auth": {"token_env": "MCP_PROXY_TOKEN"},
            "admin": {"enabled": True, "mount_name": "__admin__", "require_token": False, "allowed_clients": ["other-client"]},
            "upstreams": {},
        }
    )
    os.environ["MCP_PROXY_TOKEN"] = "secret"
    manager = UpstreamManager({}, PluginRegistry())
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(NoopTelemetrySink())
    app = create_app(AppState(config, config.model_dump(), manager, bridge, telemetry, PluginRegistry()))
    client = TestClient(app)

    payload = {"jsonrpc": "2.0", "id": 1, "method": "admin.get_health", "params": {}}
    response = client.post("/mcp/__admin__", json=payload, headers={"Authorization": "Bearer secret"})

    assert response.status_code == 403
