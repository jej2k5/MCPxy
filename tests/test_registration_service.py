"""Tests for RegistrationService and FileDropWatcher."""

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio

from mcp_proxy.config import AppConfig
from mcp_proxy.discovery.registration import (
    FileDropWatcher,
    RegistrationError,
    RegistrationService,
)
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.policy.engine import PolicyEngine
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.runtime import RuntimeConfigManager
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


@pytest_asyncio.fixture
async def runtime() -> RuntimeConfigManager:
    raw = {
        "auth": {"token_env": None},
        "upstreams": {"existing": {"type": "http", "url": "https://example.com/mcp"}},
    }
    config = AppConfig.model_validate(raw)
    registry = PluginRegistry()
    manager = UpstreamManager(config.upstreams, registry)
    telemetry = TelemetryPipeline(NoopTelemetrySink())
    policy = PolicyEngine(config)
    rt = RuntimeConfigManager(
        raw_config=raw,
        config=config,
        manager=manager,
        telemetry=telemetry,
        registry=registry,
        policy_engine=policy,
    )
    await manager.start()
    try:
        yield rt
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_register_adds_and_applies_upstream(runtime: RuntimeConfigManager) -> None:
    service = RegistrationService(runtime)
    result = await service.add(
        "brand-new",
        {"type": "http", "url": "https://new.example.com/mcp"},
    )
    assert result["applied"] is True
    assert "brand-new" in runtime.raw_config["upstreams"]
    assert "brand-new" in runtime.config.upstreams
    assert runtime.manager.get("brand-new") is not None


@pytest.mark.asyncio
async def test_register_refuses_duplicate_without_replace(runtime: RuntimeConfigManager) -> None:
    service = RegistrationService(runtime)
    with pytest.raises(RegistrationError, match="already exists"):
        await service.add(
            "existing",
            {"type": "http", "url": "https://other.example.com/mcp"},
        )


@pytest.mark.asyncio
async def test_register_replace_overwrites(runtime: RuntimeConfigManager) -> None:
    service = RegistrationService(runtime)
    result = await service.add(
        "existing",
        {"type": "http", "url": "https://new.example.com/mcp"},
        replace=True,
    )
    assert result["applied"] is True
    assert runtime.config.upstreams["existing"].url == "https://new.example.com/mcp"


@pytest.mark.asyncio
async def test_register_rejects_definition_without_type(runtime: RuntimeConfigManager) -> None:
    service = RegistrationService(runtime)
    with pytest.raises(RegistrationError, match="missing 'type'"):
        await service.add("no-type", {"url": "https://x"})


@pytest.mark.asyncio
async def test_unregister_removes_upstream(runtime: RuntimeConfigManager) -> None:
    service = RegistrationService(runtime)
    result = await service.remove("existing")
    assert result["applied"] is True
    assert "existing" not in runtime.raw_config["upstreams"]
    assert "existing" not in runtime.config.upstreams


@pytest.mark.asyncio
async def test_unregister_unknown_raises(runtime: RuntimeConfigManager) -> None:
    service = RegistrationService(runtime)
    with pytest.raises(RegistrationError, match="not found"):
        await service.remove("ghost")


@pytest.mark.asyncio
async def test_bulk_add_atomic_on_failure(runtime: RuntimeConfigManager) -> None:
    service = RegistrationService(runtime)
    before = dict(runtime.raw_config["upstreams"])
    with pytest.raises(RegistrationError):
        await service.bulk_add(
            [
                ("first", {"type": "http", "url": "https://a"}),
                ("second", {}),  # invalid: missing type
            ]
        )
    # Nothing applied because the whole batch is validated before apply().
    assert runtime.raw_config["upstreams"] == before


@pytest.mark.asyncio
async def test_bulk_add_applies_all_successfully(runtime: RuntimeConfigManager) -> None:
    service = RegistrationService(runtime)
    result = await service.bulk_add(
        [
            ("alpha", {"type": "http", "url": "https://alpha"}),
            ("beta", {"type": "http", "url": "https://beta"}),
        ]
    )
    assert result["applied"] is True
    assert runtime.manager.get("alpha") is not None
    assert runtime.manager.get("beta") is not None


@pytest.mark.asyncio
async def test_file_drop_watcher_picks_up_new_and_removed_files(
    runtime: RuntimeConfigManager, tmp_path: Path
) -> None:
    service = RegistrationService(runtime)
    watcher = FileDropWatcher(service, directory=tmp_path, poll_interval_s=0.05)
    await watcher.start()
    try:
        drop = tmp_path / "dropped.json"
        drop.write_text(
            json.dumps({"type": "http", "url": "https://dropped.example/mcp"}),
            encoding="utf-8",
        )
        # Give the poller two tick cycles to observe the new file.
        for _ in range(40):
            if "dropped" in runtime.raw_config.get("upstreams", {}):
                break
            await asyncio.sleep(0.05)
        assert "dropped" in runtime.raw_config["upstreams"]

        # Deleting the file should remove the upstream on the next scan.
        drop.unlink()
        for _ in range(40):
            if "dropped" not in runtime.raw_config.get("upstreams", {}):
                break
            await asyncio.sleep(0.05)
        assert "dropped" not in runtime.raw_config["upstreams"]
    finally:
        await watcher.stop()
