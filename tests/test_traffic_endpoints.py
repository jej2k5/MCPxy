import os

from fastapi.testclient import TestClient

from mcpxy_proxy.config import AppConfig
from mcpxy_proxy.observability.traffic import TrafficRecord
from mcpxy_proxy.proxy.bridge import ProxyBridge
from mcpxy_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcpxy_proxy.server import AppState, create_app
from mcpxy_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline


def _build_app() -> tuple[TestClient, AppState]:
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
    app = create_app(state)
    return TestClient(app), state


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_traffic_endpoint_requires_auth() -> None:
    client, _ = _build_app()
    res = client.get("/admin/api/traffic")
    assert res.status_code == 401


def test_traffic_endpoint_returns_recorded_items() -> None:
    client, state = _build_app()
    state.traffic.record(
        TrafficRecord(
            timestamp=1.0,
            upstream="a",
            method="tools/call",
            request_id=1,
            status="ok",
            latency_ms=12.3,
        )
    )
    state.traffic.record(
        TrafficRecord(
            timestamp=2.0,
            upstream="a",
            method="tools/list",
            request_id=2,
            status="error",
            latency_ms=45.0,
            error_code="boom",
        )
    )

    res = client.get("/admin/api/traffic", headers=_headers())
    assert res.status_code == 200
    body = res.json()
    assert "items" in body
    assert len(body["items"]) == 2
    # Newest first
    assert body["items"][0]["method"] == "tools/list"

    res_filtered = client.get("/admin/api/traffic?status=error", headers=_headers())
    assert res_filtered.status_code == 200
    assert len(res_filtered.json()["items"]) == 1


def test_metrics_endpoint_returns_aggregates() -> None:
    client, state = _build_app()
    import time as _time

    for _ in range(3):
        state.traffic.record(
            TrafficRecord(
                timestamp=_time.time(),
                upstream="a",
                method="m",
                request_id=1,
                status="ok",
                latency_ms=10.0,
            )
        )
    res = client.get("/admin/api/metrics", headers=_headers())
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert "per_upstream" in body
    assert "uptime_s" in body
    assert body["per_upstream"]["a"]["total"] == 3


def test_routes_endpoint_returns_snapshot() -> None:
    # Uses the same pattern as other tests that do not start the manager,
    # so the snapshot is empty but the endpoint should still respond with
    # a valid JSON object.
    client, _ = _build_app()
    res = client.get("/admin/api/routes", headers=_headers())
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body, dict)


def test_traffic_stream_endpoint_requires_auth() -> None:
    client, _ = _build_app()
    res = client.get("/admin/api/traffic/stream")
    assert res.status_code == 401


def test_traffic_endpoint_clamps_limit() -> None:
    client, state = _build_app()
    for i in range(50):
        state.traffic.record(
            TrafficRecord(
                timestamp=float(i),
                upstream="a",
                method=f"m{i}",
                request_id=i,
                status="ok",
                latency_ms=1.0,
            )
        )
    res = client.get("/admin/api/traffic?limit=10", headers=_headers())
    assert res.status_code == 200
    assert len(res.json()["items"]) == 10
