import asyncio
import json

import pytest

from mcp_proxy.config import AppConfig
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.proxy.admin import AdminService
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.runtime import RuntimeConfigManager

from mcp_proxy.proxy.base import UpstreamTransport


class DummyTransport(UpstreamTransport):
    def __init__(self, name, settings):
        self.name = name
        self.settings = settings

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

from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


class DummyTelemetry:
    def emit_nowait(self, event):
        return True


@pytest.mark.asyncio
async def test_hot_reload_via_admin_apply() -> None:
    registry = PluginRegistry()
    registry.upstreams["dummy"] = DummyTransport
    raw = {"upstreams": {"a": {"type": "dummy", "url": "http://x"}}}
    cfg = AppConfig.model_validate(raw)
    manager = UpstreamManager(raw["upstreams"], registry)
    runtime = RuntimeConfigManager(raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), registry)
    await manager.start()

    service = AdminService(manager=manager, telemetry=DummyTelemetry(), raw_config=raw, runtime_config=runtime)
    result = await service.apply_config({"config": {"upstreams": {"a": {"type": "dummy", "url": "http://changed"}}}})

    assert result["applied"] is True
    assert runtime.config.upstreams["a"]["url"] == "http://changed"
    await manager.stop()


@pytest.mark.asyncio
async def test_hot_reload_via_file_watcher(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"upstreams": {"a": {"type": "dummy", "url": "http://x"}}}))

    registry = PluginRegistry()
    registry.upstreams["dummy"] = DummyTransport
    cfg = AppConfig.model_validate(json.loads(config_path.read_text()))
    raw = json.loads(config_path.read_text())
    manager = UpstreamManager(raw["upstreams"], registry)
    runtime = RuntimeConfigManager(raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), registry, config_path=str(config_path), poll_interval_s=0.05)

    await manager.start()
    await runtime.start()

    await asyncio.sleep(0.06)
    config_path.write_text(json.dumps({"upstreams": {"a": {"type": "dummy", "url": "http://watched"}}}))

    deadline = asyncio.get_running_loop().time() + 1.0
    while runtime.config.upstreams["a"]["url"] != "http://watched" and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)

    assert runtime.config.upstreams["a"]["url"] == "http://watched"
    await runtime.stop()
    await manager.stop()
