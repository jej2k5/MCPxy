import pytest

from mcp_proxy.jsonrpc import JsonRpcError
from mcp_proxy.proxy.base import UpstreamTransport
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager


class DummyTransport(UpstreamTransport):
    def __init__(self, name, settings):
        self.name = name

    async def start(self):
        return None

    async def stop(self):
        return None

    async def restart(self):
        return None

    async def request(self, message):
        return {"jsonrpc": "2.0", "id": message["id"], "result": "ok"}

    async def send_notification(self, message):
        return None

    def health(self):
        return {"ok": True}


@pytest.mark.asyncio
async def test_overload_returns_backpressure_error() -> None:
    reg = PluginRegistry()
    reg.upstreams["dummy"] = DummyTransport
    manager = UpstreamManager({"a": {"type": "dummy"}}, reg)
    await manager.start()
    bridge = ProxyBridge(manager, queue_size=1)
    bridge.queue.put_nowait(1)

    with pytest.raises(JsonRpcError) as exc:
        await bridge.forward("a", {"jsonrpc": "2.0", "id": 1, "method": "x"})

    assert exc.value.code == -32002
    await manager.stop()
