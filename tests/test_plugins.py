from types import SimpleNamespace

import mcp_proxy.proxy.manager as manager_mod
from mcp_proxy.proxy.manager import PluginRegistry


class DummyUpstream:
    def __init__(self, name, settings):
        self.name = name


def test_plugin_discovery(monkeypatch) -> None:
    def fake_entry_points(group: str):
        if group == "mcp_proxy.upstreams":
            return [SimpleNamespace(name="dummy", load=lambda: DummyUpstream)]
        if group == "mcp_proxy.telemetry_sinks":
            return [SimpleNamespace(name="sink", load=lambda: dict)]
        return []

    monkeypatch.setattr(manager_mod, "entry_points", fake_entry_points)
    reg = PluginRegistry()
    reg.load_entry_points()
    assert "dummy" in reg.upstreams
    assert "sink" in reg.telemetry_sinks
