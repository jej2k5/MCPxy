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
            "admin": {"enabled": True, "mount_name": "admin", "require_token": require_token, "allowed_clients": ["testclient"]},
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
    response = client.post("/mcp/admin", json=payload)
    assert response.status_code == 401

    response = client.post("/mcp/admin", json=payload, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
