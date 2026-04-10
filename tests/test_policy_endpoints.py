import os

from fastapi.testclient import TestClient

from mcpxy_proxy.config import AppConfig
from mcpxy_proxy.proxy.bridge import ProxyBridge
from mcpxy_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcpxy_proxy.server import AppState, create_app
from mcpxy_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline


def _build() -> tuple[TestClient, AppState]:
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
    state = AppState(config, config.model_dump(), manager, bridge, telemetry, reg)
    return TestClient(create_app(state)), state


HEADERS = {"Authorization": "Bearer secret"}


def test_get_policies_returns_empty_by_default() -> None:
    client, _ = _build()
    res = client.get("/admin/api/policies", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert "per_upstream" in body
    assert body["per_upstream"] == {}


def test_get_policies_requires_auth() -> None:
    client, _ = _build()
    res = client.get("/admin/api/policies")
    assert res.status_code == 401


def test_post_policies_dry_run_validates_without_applying() -> None:
    client, state = _build()
    payload = {
        "policies": {
            "per_upstream": {
                "a": {"methods": {"deny": ["dangerous"]}},
            }
        },
        "dry_run": True,
    }
    res = client.post("/admin/api/policies", headers=HEADERS, json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["dry_run"] is True
    # Live config unchanged
    assert state.runtime_config.config.policies.per_upstream == {}


def test_post_policies_applies_and_blocks_subsequent_request() -> None:
    client, state = _build()
    payload = {
        "policies": {
            "per_upstream": {
                "a": {"methods": {"deny": ["forbidden"]}},
            }
        }
    }
    res = client.post("/admin/api/policies", headers=HEADERS, json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["applied"] is True
    # Live engine reflects new policy.
    assert "a" in state.runtime_config.config.policies.per_upstream
    decision = state.policy_engine.check(upstream="a", message={"method": "forbidden"})
    assert decision.allowed is False
    assert decision.reason == "method_denied"
