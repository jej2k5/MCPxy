import pytest

from mcp_proxy.jsonrpc import JsonRpcError
from mcp_proxy.proxy.base import UpstreamTransport
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager


class DummyTransport(UpstreamTransport):
    def __init__(self, name, settings):
        self.restarted = False

    async def start(self):
        return None

    async def stop(self):
        return None

    async def restart(self):
        self.restarted = True

    async def request(self, message):
        return {"jsonrpc": "2.0", "id": message["id"], "result": "ok"}

    async def send_notification(self, message):
        return None

    def health(self):
        return {"ok": True}


@pytest.mark.asyncio
async def test_upstream_restart() -> None:
    reg = PluginRegistry()
    reg.upstreams["dummy"] = DummyTransport
    manager = UpstreamManager({"a": {"type": "dummy"}}, reg)
    await manager.start()
    assert await manager.restart("a") is True


@pytest.mark.asyncio
async def test_backpressure_behavior() -> None:
    reg = PluginRegistry()
    reg.upstreams["dummy"] = DummyTransport
    manager = UpstreamManager({"a": {"type": "dummy"}}, reg)
    await manager.start()
    bridge = ProxyBridge(manager, queue_size=1)
    bridge.queue.put_nowait(1)
    with pytest.raises(JsonRpcError) as exc:
        await bridge.forward("a", {"jsonrpc": "2.0", "id": 1, "method": "x"})
    assert exc.value.code == -32002


class FailingTransport(UpstreamTransport):
    def __init__(self, name, settings):
        self.settings = settings

    async def start(self):
        if self.settings.get("fail"):
            raise RuntimeError("boom")

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


@pytest.mark.asyncio
async def test_apply_diff_rolls_back_on_failure() -> None:
    reg = PluginRegistry()
    reg.upstreams["dummy"] = FailingTransport
    manager = UpstreamManager({"a": {"type": "dummy"}}, reg)
    await manager.start()

    with pytest.raises(RuntimeError):
        await manager.apply_diff({"a": {"type": "dummy", "fail": True}})

    assert manager.get("a") is not None
    await manager.stop()
