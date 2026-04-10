import pytest

from mcpxy_proxy.observability.discovery import RouteDiscoverer
from mcpxy_proxy.proxy.base import UpstreamTransport
from mcpxy_proxy.proxy.manager import PluginRegistry, UpstreamManager


class ToolListingTransport(UpstreamTransport):
    def __init__(self, name, settings):
        pass

    async def start(self):
        return None

    async def stop(self):
        return None

    async def restart(self):
        return None

    async def request(self, message, **kwargs):
        if message.get("method") == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {"name": "echo", "description": "Echoes input"},
                        {"name": "sum", "description": "Adds numbers"},
                    ]
                },
            }
        return {"jsonrpc": "2.0", "id": message["id"], "result": None}

    async def send_notification(self, message, **kwargs):
        return None

    def health(self):
        return {"ok": True}


class FailingToolList(UpstreamTransport):
    def __init__(self, name, settings):
        pass

    async def start(self):
        return None

    async def stop(self):
        return None

    async def restart(self):
        return None

    async def request(self, message, **kwargs):
        raise RuntimeError("no tools/list")

    async def send_notification(self, message, **kwargs):
        return None

    def health(self):
        return {"ok": True}


@pytest.mark.asyncio
async def test_discoverer_caches_tool_list() -> None:
    reg = PluginRegistry()
    reg.upstreams["toolie"] = ToolListingTransport
    manager = UpstreamManager({"a": {"type": "toolie"}}, reg)
    await manager.start()
    disc = RouteDiscoverer(manager, interval_s=60.0)

    await disc.refresh_now()
    snap = disc.snapshot()
    assert "a" in snap
    assert snap["a"]["discovery"]["ok"] is True
    assert len(snap["a"]["discovery"]["tools"]) == 2
    assert snap["a"]["discovery"]["tools"][0]["name"] == "echo"


@pytest.mark.asyncio
async def test_discoverer_records_error_on_failure() -> None:
    reg = PluginRegistry()
    reg.upstreams["broke"] = FailingToolList
    manager = UpstreamManager({"a": {"type": "broke"}}, reg)
    await manager.start()
    disc = RouteDiscoverer(manager)

    await disc.refresh_now()
    snap = disc.snapshot()
    assert snap["a"]["discovery"]["ok"] is False
    assert snap["a"]["discovery"]["error"] == "no tools/list"
    assert snap["a"]["discovery"]["tools"] == []
