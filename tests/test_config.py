import asyncio
import json

import pytest

from mcp_proxy.config import (
    AppConfig,
    TlsConfig,
    _apply_expansions,
    load_config,
    redact_secrets,
    validate_config_payload,
)
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

    async def request(self, message, **kwargs):
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": "ok"}

    async def send_notification(self, message, **kwargs):
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
async def test_runtime_apply_persists_to_store(tmp_path) -> None:
    """Verify the runtime applier writes through to the ConfigStore.

    The file-mtime watcher that the previous version of this test
    exercised has been removed: with the DB as the canonical store
    there is no file to poll, so callers go through ``apply()`` (or
    the admin API, which calls ``apply()``) and we just need to know
    that ``apply()`` actually persists the new payload alongside
    bumping the in-memory state.
    """
    from cryptography.fernet import Fernet

    from mcp_proxy.storage.config_store import ConfigStore
    from mcp_proxy.storage.db import build_engine, run_migrations

    engine = build_engine(f"sqlite:///{tmp_path / 'mcpy.db'}")
    run_migrations(engine)
    store = ConfigStore(engine, Fernet(Fernet.generate_key()))
    store.load_all()

    registry = PluginRegistry()
    registry.upstreams["dummy"] = DummyTransport
    raw = {"upstreams": {"a": {"type": "dummy", "url": "http://x"}}}
    cfg = AppConfig.model_validate(raw)
    manager = UpstreamManager(raw["upstreams"], registry)
    runtime = RuntimeConfigManager(
        raw,
        cfg,
        manager,
        TelemetryPipeline(NoopTelemetrySink()),
        registry,
        store=store,
    )
    await manager.start()

    next_payload = {"upstreams": {"a": {"type": "dummy", "url": "http://changed"}}}
    result = await runtime.apply(next_payload, source="test")
    assert result["applied"] is True
    assert result["diff"]["version"] == 1
    assert runtime.config.upstreams["a"]["url"] == "http://changed"

    # The store now reflects the new payload, and a fresh ConfigStore
    # with the same engine reads it back identically.
    persisted = store.get_active_config()
    assert persisted == next_payload
    assert store.active_version() == 1
    history = store.list_config_history()
    assert history[0]["version"] == 1
    assert history[0]["source"] == "test"
    await manager.stop()
    store.close()


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


def test_tls_config_defaults_to_disabled() -> None:
    cfg = AppConfig.model_validate({"upstreams": {}})
    assert cfg.tls.enabled is False
    assert cfg.tls.certfile is None
    assert cfg.tls.keyfile is None
    assert cfg.tls.keyfile_password is None


def test_tls_config_loads_and_validates() -> None:
    cfg = AppConfig.model_validate(
        {
            "tls": {
                "enabled": True,
                "certfile": "/etc/mcpy/cert.pem",
                "keyfile": "/etc/mcpy/key.pem",
            },
            "upstreams": {},
        }
    )
    assert cfg.tls.enabled is True
    assert cfg.tls.certfile == "/etc/mcpy/cert.pem"
    assert cfg.tls.keyfile == "/etc/mcpy/key.pem"


def test_tls_config_allows_staged_values_when_disabled() -> None:
    # Operators can land certfile/keyfile in config and flip `enabled`
    # separately, e.g. via a follow-up admin PATCH.
    cfg = AppConfig.model_validate(
        {
            "tls": {
                "enabled": False,
                "certfile": "/etc/mcpy/cert.pem",
                "keyfile": "/etc/mcpy/key.pem",
            },
            "upstreams": {},
        }
    )
    assert cfg.tls.enabled is False
    assert cfg.tls.certfile == "/etc/mcpy/cert.pem"


def test_tls_config_enabled_requires_certfile_and_keyfile() -> None:
    with pytest.raises(Exception) as exc_info:
        TlsConfig.model_validate({"enabled": True, "certfile": "/a"})
    assert "certfile and keyfile" in str(exc_info.value)


def test_tls_config_password_requires_keyfile() -> None:
    with pytest.raises(Exception) as exc_info:
        TlsConfig.model_validate({"keyfile_password": "hunter2"})
    assert "keyfile" in str(exc_info.value)


def test_tls_config_keyfile_password_env_expansion(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TLS_PW", "s3cret")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tls": {
                    "enabled": True,
                    "certfile": "/etc/mcpy/cert.pem",
                    "keyfile": "/etc/mcpy/key.pem",
                    "keyfile_password": "${env:TLS_PW}",
                },
                "upstreams": {},
            }
        )
    )
    cfg = load_config(config_path)
    assert cfg.tls.keyfile_password == "s3cret"


def test_tls_config_keyfile_password_secret_expansion() -> None:
    payload = {
        "tls": {
            "enabled": True,
            "certfile": "/etc/mcpy/cert.pem",
            "keyfile": "/etc/mcpy/key.pem",
            "keyfile_password": "${secret:TLS_PW}",
        },
        "upstreams": {},
    }

    def resolver(name: str) -> str | None:
        return "stub-password" if name == "TLS_PW" else None

    expanded = _apply_expansions(payload, secrets=resolver)
    cfg = AppConfig.model_validate(expanded)
    assert cfg.tls.keyfile_password == "stub-password"


def test_redact_secrets_redacts_tls_keyfile_password() -> None:
    payload = {
        "tls": {
            "enabled": True,
            "certfile": "/etc/mcpy/cert.pem",
            "keyfile": "/etc/mcpy/key.pem",
            "keyfile_password": "hunter2",
        },
        "upstreams": {},
    }
    redacted = redact_secrets(payload)
    assert redacted["tls"]["keyfile_password"] == "***REDACTED***"
    # Non-secret tls fields stay visible.
    assert redacted["tls"]["certfile"] == "/etc/mcpy/cert.pem"
    assert redacted["tls"]["enabled"] is True
    # The original payload is not mutated.
    assert payload["tls"]["keyfile_password"] == "hunter2"
