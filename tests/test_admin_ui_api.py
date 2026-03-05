import os

from fastapi.testclient import TestClient

from mcp_proxy.config import AppConfig
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


def _client():
    config = AppConfig.model_validate(
        {
            "auth": {"token_env": "MCP_PROXY_TOKEN"},
            "admin": {"enabled": True, "mount_name": "__admin__", "require_token": True, "allowed_clients": ["testclient"]},
            "upstreams": {"a": {"type": "http", "url": "http://example"}},
        }
    )
    os.environ["MCP_PROXY_TOKEN"] = "secret"
    reg = PluginRegistry()
    manager = UpstreamManager(config.upstreams, reg)
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(NoopTelemetrySink())
    app = create_app(AppState(config, config.model_dump(), manager, bridge, telemetry, reg))
    return TestClient(app)


def test_admin_api_endpoints_and_ui_auth() -> None:
    client = _client()

    unauth = client.get("/admin")
    assert unauth.status_code == 401

    headers = {"Authorization": "Bearer secret"}
    page = client.get("/admin", headers=headers)
    assert page.status_code == 200

    config_response = client.get("/admin/api/config", headers=headers)
    assert config_response.status_code == 200
    assert "upstreams" in config_response.json()

    upstreams = client.get("/admin/api/upstreams", headers=headers)
    assert upstreams.status_code == 200

    validate = client.post("/admin/api/config/validate", headers=headers, json={"config": {"upstreams": {}}})
    assert validate.status_code == 200
    assert validate.json()["valid"] is True

    logs = client.get("/admin/api/logs", headers=headers)
    assert logs.status_code == 200
