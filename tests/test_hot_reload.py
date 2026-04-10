import asyncio
import json

import pytest

from mcpxy_proxy.config import AppConfig
from mcpxy_proxy.plugins.registry import PluginRegistry
from mcpxy_proxy.proxy.admin import AdminService
from mcpxy_proxy.proxy.manager import UpstreamManager
from mcpxy_proxy.runtime import RuntimeConfigManager

from mcpxy_proxy.proxy.base import UpstreamTransport


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

    async def request(self, message, **kwargs):
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": "ok"}

    async def send_notification(self, message, **kwargs):
        return None

    def health(self):
        return {"ok": True}

from mcpxy_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline


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
async def test_runtime_start_stop_is_a_noop_without_file_watcher() -> None:
    """The mtime-polling file watcher was removed when config moved to
    the DB; ``start()``/``stop()`` are kept for lifecycle compatibility
    but must not spin up any background tasks. This regression guard
    catches anyone who tries to bring it back without remembering that
    the DB is now the source of truth.
    """
    registry = PluginRegistry()
    registry.upstreams["dummy"] = DummyTransport
    raw = {"upstreams": {"a": {"type": "dummy", "url": "http://x"}}}
    cfg = AppConfig.model_validate(raw)
    manager = UpstreamManager(raw["upstreams"], registry)
    runtime = RuntimeConfigManager(
        raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), registry,
        config_path="/tmp/whatever-this-is-not-read-anymore",
        poll_interval_s=0.05,
    )
    await manager.start()
    await runtime.start()
    # No background task scheduled. The runtime keeps no asyncio.Task
    # references; only the apply() / store path mutates state now.
    assert getattr(runtime, "_watch_task", None) is None
    await runtime.stop()
    await manager.stop()
