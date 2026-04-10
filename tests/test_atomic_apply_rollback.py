import pytest

from mcpxy_proxy.config import AppConfig
from mcpxy_proxy.plugins.registry import PluginRegistry
from mcpxy_proxy.proxy.base import UpstreamTransport
from mcpxy_proxy.proxy.manager import UpstreamManager
from mcpxy_proxy.runtime import RuntimeConfigManager
from mcpxy_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline


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

    async def request(self, message, **kwargs):
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": "ok"}

    async def send_notification(self, message, **kwargs):
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


@pytest.mark.asyncio
async def test_apply_rejects_tls_changes() -> None:
    """Hot-reload must refuse to mutate the tls block.

    The uvicorn listener's SSL context is bound at startup, so silently
    accepting a ``tls`` change would leave the running process serving
    whatever protocol it was launched with while the stored config
    claims otherwise. We return a "restart required" error instead.
    """
    reg = PluginRegistry()
    reg.upstreams["dummy"] = FailingTransport
    raw = {"upstreams": {"a": {"type": "dummy"}}}
    cfg = AppConfig.model_validate(raw)
    manager = UpstreamManager(cfg.upstreams, reg)
    await manager.start()

    runtime = RuntimeConfigManager(
        raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), reg
    )
    candidate = {
        "tls": {
            "enabled": True,
            "certfile": "/etc/mcpxy/cert.pem",
            "keyfile": "/etc/mcpxy/key.pem",
        },
        "upstreams": {"a": {"type": "dummy"}},
    }
    result = await runtime.apply(candidate)

    assert result["applied"] is False
    assert result["rolled_back"] is True
    assert "restart" in result["error"]
    # Running config is unchanged.
    assert runtime.config.tls.enabled is False
    assert runtime.config.tls.certfile is None
    await manager.stop()


@pytest.mark.asyncio
async def test_apply_diff_reports_tls_changed_false_when_unchanged() -> None:
    """The diff must expose a tls_changed flag so dry-run callers see it."""
    reg = PluginRegistry()
    reg.upstreams["dummy"] = FailingTransport
    raw = {"upstreams": {"a": {"type": "dummy"}}}
    cfg = AppConfig.model_validate(raw)
    manager = UpstreamManager(cfg.upstreams, reg)
    await manager.start()

    runtime = RuntimeConfigManager(
        raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), reg
    )
    result = await runtime.apply(
        {"upstreams": {"a": {"type": "dummy"}}}, dry_run=True
    )
    assert result["diff"]["tls_changed"] is False
    await manager.stop()
