import asyncio
import json

import pytest

from mcp_proxy.config import AppConfig, load_config, validate_config_payload
from mcp_proxy.proxy.admin import AdminService
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcp_proxy.runtime import RuntimeConfigManager
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
    raw = {"upstreams": {"a": {"type": "http", "url": "http://x"}}}
    cfg = AppConfig.model_validate(raw)
    manager = UpstreamManager(cfg.upstreams, PluginRegistry())
    runtime = RuntimeConfigManager(raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), PluginRegistry())
    service = AdminService(manager=DummyManager(), telemetry=DummyTelemetry(), raw_config=raw, runtime_config=runtime)  # type: ignore[arg-type]
    result = await service.apply_config({"config": raw, "dry_run": True})
    assert result["dry_run"] is True
    assert raw == {"upstreams": {"a": {"type": "http", "url": "http://x"}}}


def test_config_validation() -> None:
    ok, err = validate_config_payload({"default_upstream": "x", "upstreams": {}})
    assert not ok
    assert err


@pytest.mark.asyncio
async def test_admin_apply_config_updates_upstream_diff() -> None:
    registry = PluginRegistry()
    raw = {"upstreams": {"a": {"type": "http", "url": "http://x"}}}
    cfg = AppConfig.model_validate(raw)
    manager = UpstreamManager(cfg.upstreams, registry)
    runtime = RuntimeConfigManager(raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), registry)
    await manager.start()

    service = AdminService(manager=manager, telemetry=DummyTelemetry(), raw_config=raw, runtime_config=runtime)
    candidate = {"upstreams": {"a": {"type": "http", "url": "http://y"}, "b": {"type": "http", "url": "http://z"}}}
    result = await service.apply_config({"config": candidate})

    assert result["applied"] is True
    assert sorted(result["diff"]["upstreams"]["added"]) == ["b"]
    assert sorted(result["diff"]["upstreams"]["restarted"]) == ["a"]
    await manager.stop()


@pytest.mark.asyncio
async def test_file_watcher_reload(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"upstreams": {"a": {"type": "http", "url": "http://x"}}}))

    registry = PluginRegistry()
    cfg = AppConfig.model_validate(json.loads(config_path.read_text()))
    raw = json.loads(config_path.read_text())
    manager = UpstreamManager(cfg.upstreams, registry)
    runtime = RuntimeConfigManager(raw, cfg, manager, TelemetryPipeline(NoopTelemetrySink()), registry, config_path=str(config_path), poll_interval_s=0.05)

    await manager.start()
    await runtime.start()

    config_path.write_text(json.dumps({"upstreams": {"a": {"type": "http", "url": "http://changed"}}}))
    await asyncio.sleep(0.2)
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
