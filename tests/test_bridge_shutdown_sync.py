import asyncio

from mcp_proxy.jsonrpc import JsonRpcError
from mcp_proxy.proxy.base import UpstreamTransport
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager


class SlowTransport(UpstreamTransport):
    def __init__(self, name, settings):
        self._release = asyncio.Event()

    async def start(self):
        return None

    async def stop(self):
        self._release.set()

    async def restart(self):
        return None

    async def request(self, message):
        await self._release.wait()
        return {"jsonrpc": "2.0", "id": message["id"], "result": "ok"}

    async def send_notification(self, message):
        return None

    def health(self):
        return {"ok": True}


def test_shutdown_rejects_in_flight_and_new_requests() -> None:
    async def _run() -> None:
        reg = PluginRegistry()
        reg.upstreams["dummy"] = SlowTransport
        manager = UpstreamManager({"a": {"type": "dummy"}}, reg)
        await manager.start()
        bridge = ProxyBridge(manager)
        seen_events = []
        bridge.set_telemetry_emitter(seen_events.append)

        task = asyncio.create_task(bridge.forward("a", {"jsonrpc": "2.0", "id": 1, "method": "x"}))
        await asyncio.sleep(0.02)
        bridge.start_shutdown()

        try:
            await task
            raise AssertionError("expected shutdown error")
        except JsonRpcError as exc:
            assert exc.code == -32000
            assert exc.message == "proxy_shutting_down"

        try:
            await bridge.forward("a", {"jsonrpc": "2.0", "id": 2, "method": "x"})
            raise AssertionError("expected shutdown error")
        except JsonRpcError as exc:
            assert exc.code == -32000

        reasons = [event["reason"] for event in seen_events if event.get("event") == "proxy_request_failed"]
        assert "shutdown_reject_in_flight" in reasons
        assert "shutdown_reject_new" in reasons
        await manager.stop()

    asyncio.run(_run())
