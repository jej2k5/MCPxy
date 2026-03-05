from types import SimpleNamespace

import mcp_proxy.plugins.registry as registry_mod
from mcp_proxy.plugins.registry import PluginRegistry


class DummyUpstream:
    def __init__(self, name, settings):
        self.name = name


def test_plugin_discovery(monkeypatch) -> None:
    def fake_entry_points(group: str):
        if group == "mcp_proxy.upstreams":
            return [SimpleNamespace(name="dummy", value="pkg:DummyUpstream", load=lambda: DummyUpstream)]
        if group == "mcp_proxy.telemetry_sinks":
            return [SimpleNamespace(name="sink", value="pkg:Sink", load=lambda: dict)]
        return []

    monkeypatch.setattr(registry_mod, "entry_points", fake_entry_points)
    reg = PluginRegistry()
    reg.load_entry_points()
    assert "dummy" in reg.upstreams
    assert "sink" in reg.telemetry_sinks


def test_duplicate_key_rejected_for_entry_point(monkeypatch) -> None:
    def fake_entry_points(group: str):
        if group == "mcp_proxy.upstreams":
            return [SimpleNamespace(name="stdio", value="pkg:AltStdio", load=lambda: DummyUpstream)]
        return []

    monkeypatch.setattr(registry_mod, "entry_points", fake_entry_points)
    reg = PluginRegistry()

    try:
        reg.load_entry_points()
    except ValueError as exc:
        message = str(exc)
        assert "Duplicate upstream plugin key 'stdio'" in message
        assert "allow_override=True" in message
    else:
        raise AssertionError("Expected duplicate registration to fail")


def test_validation_error_lists_available_keys() -> None:
    reg = PluginRegistry()

    try:
        reg.validate_upstream_type("missing")
    except ValueError as exc:
        message = str(exc)
        assert "Unknown upstream type 'missing'" in message
        assert "http, stdio" in message
    else:
        raise AssertionError("Expected upstream validation failure")

    try:
        reg.validate_telemetry_sink_type("missing")
    except ValueError as exc:
        message = str(exc)
        assert "Unknown telemetry sink type 'missing'" in message
        assert "http, noop" in message
    else:
        raise AssertionError("Expected telemetry sink validation failure")
