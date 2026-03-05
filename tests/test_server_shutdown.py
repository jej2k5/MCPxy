import signal
import time

from fastapi.testclient import TestClient

from mcp_proxy.config import AppConfig
from mcp_proxy.proxy.base import UpstreamTransport
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.telemetry.base import TelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


class DummyTransport(UpstreamTransport):
    def __init__(self, name, settings):
        self.name = name

    async def start(self):
        return None

    async def stop(self):
        return None

    async def restart(self):
        return None

    async def request(self, message):
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": "ok"}

    async def send_notification(self, message):
        return None

    def health(self):
        return {"ok": True}


class CollectSink(TelemetrySink):
    def __init__(self):
        self.events = []

    async def start(self):
        return None

    async def stop(self):
        return None

    async def emit(self, event):
        self.events.append(event)

    def health(self):
        return {"ok": True}


def test_sigint_and_sigterm_mark_shutdown_and_emit_telemetry() -> None:
    config = AppConfig.model_validate(
        {
            "admin": {"enabled": False},
            "upstreams": {"a": {"type": "dummy"}},
            "default_upstream": "a",
        }
    )
    registry = PluginRegistry()
    registry.upstreams["dummy"] = DummyTransport
    manager = UpstreamManager(config.upstreams, registry)
    bridge = ProxyBridge(manager)
    sink = CollectSink()
    telemetry = TelemetryPipeline(sink, flush_interval_ms=50)
    app = create_app(AppState(config, config.model_dump(), manager, bridge, telemetry, registry))

    with TestClient(app) as client:
        app.state.handle_shutdown_signal(signal.SIGINT)
        app.state.handle_shutdown_signal(signal.SIGTERM)

        time.sleep(0.1)
        events = [event for event in sink.events if event.get("event") == "proxy_shutdown_signal"]
        assert {event["signal"] for event in events} >= {"SIGINT", "SIGTERM"}

        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert response.status_code == 200
        assert "proxy_shutting_down" in response.text
