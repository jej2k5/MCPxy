import pytest

from mcp_proxy.config import AppConfig
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.proxy.base import UpstreamTransport
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.runtime import RuntimeConfigManager
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


class FailingTransport(UpstreamTransport):
    def __init__(self, _name, settings):
        self.settings = settings

    async def start(self):
        if self.settings.get("fail"):
            raise RuntimeError("boom")

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


@pytest.mark.asyncio
async def test_atomic_apply_rolls_back_on_failure() -> None:
    reg = PluginRegistry()
    reg.upstreams["dummy"] = FailingTransport
    raw = {"upstreams": {"a": {"type": "dummy"}}}
    cfg = AppConfig.model_validate(raw)
    manager = UpstreamManager(cfg.upstreams, reg)
    await manager.start()

    runtime = RuntimeConfigManager(raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), reg)
    result = await runtime.apply({"upstreams": {"a": {"type": "dummy", "fail": True}}})

    assert result["applied"] is False
    assert result["rolled_back"] is True
    assert runtime.config.upstreams["a"] == {"type": "dummy"}
    await manager.stop()
