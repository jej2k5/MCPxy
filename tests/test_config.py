from mcp_proxy.config import validate_config_payload
from mcp_proxy.proxy.admin import AdminService


class DummyManager:
    def health(self):
        return {}

    async def restart(self, name: str):
        return True


class DummyTelemetry:
    def emit_nowait(self, event):
        return True


def test_config_validation() -> None:
    ok, err = validate_config_payload({"default_upstream": "x", "upstreams": {}})
    assert not ok
    assert err


def test_config_atomic_apply_dry_run() -> None:
    raw = {"upstreams": {"a": {"type": "http", "url": "http://x"}}}
    service = AdminService(config=object(), manager=DummyManager(), telemetry=DummyTelemetry(), raw_config=raw)  # type: ignore[arg-type]
    result = service.apply_config({"config": raw, "dry_run": True})
    assert result["dry_run"] is True
    assert raw == {"upstreams": {"a": {"type": "http", "url": "http://x"}}}
