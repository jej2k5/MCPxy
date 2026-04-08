import pytest

from mcp_proxy.observability.traffic import TrafficRecord, TrafficRecorder
from mcp_proxy.proxy.base import UpstreamTransport
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager


class OkTransport(UpstreamTransport):
    def __init__(self, name, settings):
        pass

    async def start(self):
        return None

    async def stop(self):
        return None

    async def restart(self):
        return None

    async def request(self, message):
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}

    async def send_notification(self, message):
        return None

    def health(self):
        return {"ok": True}


class ErrorResponseTransport(UpstreamTransport):
    def __init__(self, name, settings):
        pass

    async def start(self):
        return None

    async def stop(self):
        return None

    async def restart(self):
        return None

    async def request(self, message):
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -1, "message": "boom"},
        }

    async def send_notification(self, message):
        return None

    def health(self):
        return {"ok": True}


async def _build_bridge(transport_cls: type[UpstreamTransport]) -> tuple[ProxyBridge, TrafficRecorder]:
    reg = PluginRegistry()
    reg.upstreams["dummy"] = transport_cls
    manager = UpstreamManager({"a": {"type": "dummy"}}, reg)
    await manager.start()
    bridge = ProxyBridge(manager)
    recorder = TrafficRecorder()
    bridge.set_traffic_recorder(recorder.record)
    return bridge, recorder


@pytest.mark.asyncio
async def test_forward_records_success_with_latency_and_bytes() -> None:
    bridge, recorder = await _build_bridge(OkTransport)
    resp = await bridge.forward(
        "a",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
        request_bytes=42,
        client_ip="1.2.3.4",
    )
    assert resp is not None and resp["result"] == {"ok": True}

    items = recorder.recent()
    assert len(items) == 1
    rec = items[0]
    assert rec["upstream"] == "a"
    assert rec["method"] == "tools/call"
    assert rec["status"] == "ok"
    assert rec["request_bytes"] == 42
    assert rec["response_bytes"] > 0
    assert rec["latency_ms"] >= 0
    assert rec["client_ip"] == "1.2.3.4"
    assert rec["error_code"] is None


@pytest.mark.asyncio
async def test_forward_records_error_when_response_contains_error() -> None:
    bridge, recorder = await _build_bridge(ErrorResponseTransport)
    await bridge.forward("a", {"jsonrpc": "2.0", "id": 1, "method": "x"})
    items = recorder.recent()
    assert len(items) == 1
    assert items[0]["status"] == "error"
    assert items[0]["error_code"] == "boom"


@pytest.mark.asyncio
async def test_forward_records_unavailable_upstream() -> None:
    reg = PluginRegistry()
    reg.upstreams["dummy"] = OkTransport
    manager = UpstreamManager({"a": {"type": "dummy"}}, reg)
    await manager.start()
    bridge = ProxyBridge(manager)
    recorder = TrafficRecorder()
    bridge.set_traffic_recorder(recorder.record)

    from mcp_proxy.jsonrpc import JsonRpcError

    with pytest.raises(JsonRpcError):
        await bridge.forward("missing", {"jsonrpc": "2.0", "id": 7, "method": "x"})

    items = recorder.recent()
    assert len(items) == 1
    assert items[0]["status"] == "error"
    assert items[0]["error_code"] == "upstream_unavailable"


@pytest.mark.asyncio
async def test_forward_records_overloaded_when_queue_full() -> None:
    reg = PluginRegistry()
    reg.upstreams["dummy"] = OkTransport
    manager = UpstreamManager({"a": {"type": "dummy"}}, reg)
    await manager.start()
    bridge = ProxyBridge(manager, queue_size=1)
    bridge.queue.put_nowait(1)  # simulate saturated queue
    recorder = TrafficRecorder()
    bridge.set_traffic_recorder(recorder.record)

    from mcp_proxy.jsonrpc import JsonRpcError

    with pytest.raises(JsonRpcError):
        await bridge.forward("a", {"jsonrpc": "2.0", "id": 1, "method": "x"})

    items = recorder.recent()
    assert len(items) == 1
    assert items[0]["error_code"] == "proxy_overloaded"
