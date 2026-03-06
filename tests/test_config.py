import asyncio
import json

import pytest

from mcp_proxy.config import AppConfig, load_config, validate_config_payload
from mcp_proxy.proxy.admin import AdminService
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager
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


class DummyManager:
    def health(self):
        return {}

    async def restart(self, name: str):
        return True


class DummyTelemetry:
    def emit_nowait(self, event):
        return True


@pytest.mark.asyncio
async def test_config_atomic_apply_dry_run() -> None:
    raw = {"upstreams": {"a": {"type": "dummy", "url": "http://x"}}}
    cfg = AppConfig.model_validate(raw)
    manager = UpstreamManager(cfg.upstreams, PluginRegistry())
    runtime = RuntimeConfigManager(raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), PluginRegistry())
    service = AdminService(manager=DummyManager(), telemetry=DummyTelemetry(), raw_config=raw, runtime_config=runtime)  # type: ignore[arg-type]
    result = await service.apply_config({"config": raw, "dry_run": True})
    assert result["dry_run"] is True
    assert raw == {"upstreams": {"a": {"type": "dummy", "url": "http://x"}}}


def test_config_validation() -> None:
    ok, err = validate_config_payload({"default_upstream": "x", "upstreams": {}})
    assert not ok
    assert err


@pytest.mark.asyncio
async def test_admin_apply_config_updates_upstream_diff() -> None:
    registry = PluginRegistry()
    registry.upstreams["dummy"] = DummyTransport
    raw = {"upstreams": {"a": {"type": "dummy", "url": "http://x"}}}
    cfg = AppConfig.model_validate(raw)
    manager = UpstreamManager(raw["upstreams"], registry)
    runtime = RuntimeConfigManager(raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), registry)
    await manager.start()

    service = AdminService(manager=manager, telemetry=DummyTelemetry(), raw_config=raw, runtime_config=runtime)
    candidate = {"upstreams": {"a": {"type": "dummy", "url": "http://y"}, "b": {"type": "dummy", "url": "http://z"}}}
    result = await service.apply_config({"config": candidate})

    assert result["applied"] is True
    assert sorted(result["diff"]["upstreams"]["added"]) == ["b"]
    assert sorted(result["diff"]["upstreams"]["restarted"]) == ["a"]
    await manager.stop()


@pytest.mark.asyncio
async def test_file_watcher_reload(tmp_path) -> None:
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
    config_path.write_text(json.dumps({"upstreams": {"a": {"type": "dummy", "url": "http://changed"}}}))

    deadline = asyncio.get_running_loop().time() + 1.0
    while runtime.config.upstreams["a"]["url"] != "http://changed" and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    assert runtime.config.upstreams["a"]["url"] == "http://changed"

    await runtime.stop()
    await manager.stop()


def test_telemetry_config_spec_fields_and_env_expansion(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TELEM_ENDPOINT", "https://telemetry.example.com/ingest")
    monkeypatch.setenv("TELEM_KEY", "abc123")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "telemetry": {
                    "sink": "http",
                    "endpoint": "${env:TELEM_ENDPOINT}",
                    "headers": {"X-Api-Key": "${env:TELEM_KEY}"},
                    "queue_max": 5,
                    "drop_policy": "drop_oldest",
                },
                "upstreams": {"a": {"type": "http", "url": "http://x"}},
            }
        )
    )

    cfg = load_config(config_path)
    assert cfg.telemetry.endpoint == "https://telemetry.example.com/ingest"
    assert cfg.telemetry.headers["X-Api-Key"] == "abc123"
    assert cfg.telemetry.queue_max == 5
    assert cfg.telemetry.drop_policy == "drop_oldest"


def test_telemetry_config_rejects_invalid_spec_fields() -> None:
    ok, err = validate_config_payload(
        {
            "telemetry": {"queue_max": 0, "drop_policy": "invalid"},
            "upstreams": {"a": {"type": "http", "url": "http://x"}},
        }
    )
    assert not ok
    assert err


def test_admin_mount_name_default() -> None:
    cfg = AppConfig.model_validate({"upstreams": {}})
    assert cfg.admin.mount_name == "__admin__"
