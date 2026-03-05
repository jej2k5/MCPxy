import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from mcp_proxy.config import AppConfig
from mcp_proxy.proxy.base import UpstreamTransport
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


class TimedDummyTransport(UpstreamTransport):
    def __init__(self, _name: str, _settings: dict):
        self.notifications: list[dict] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def restart(self) -> None:
        return None

    async def request(self, message):
        if message.get("method") == "slow":
            await asyncio.sleep(0.25)
        return {"jsonrpc": "2.0", "id": message["id"], "result": message["method"]}

    async def send_notification(self, message):
        self.notifications.append(message)

    def health(self):
        return {"status": "ok"}


def _build_client() -> TestClient:
    registry = PluginRegistry()
    registry.upstreams["timed"] = TimedDummyTransport
    config = AppConfig.model_validate({"default_upstream": "a", "upstreams": {"a": {"type": "timed"}}})
    manager = UpstreamManager(config.upstreams, registry)
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(NoopTelemetrySink())
    app = create_app(AppState(config, config.model_dump(), manager, bridge, telemetry, registry))
    return TestClient(app)


def _build_app():
    registry = PluginRegistry()
    registry.upstreams["timed"] = TimedDummyTransport
    config = AppConfig.model_validate({"default_upstream": "a", "upstreams": {"a": {"type": "timed"}}})
    manager = UpstreamManager(config.upstreams, registry)
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(NoopTelemetrySink())
    return create_app(AppState(config, config.model_dump(), manager, bridge, telemetry, registry))


@pytest.mark.anyio
async def test_ndjson_streams_first_chunk_before_slow_request_finishes() -> None:
    app = _build_app()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        await asyncio.sleep(0.01)

    payload = (
        '{"jsonrpc":"2.0","id":1,"method":"fast"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"slow"}\n'
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            async with client.stream(
                "POST",
                f"http://127.0.0.1:{port}/mcp",
                content=payload,
                headers={"content-type": "application/x-ndjson"},
            ) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                started = time.perf_counter()
                first = json.loads(await anext(lines))
                first_elapsed = time.perf_counter() - started
                second = json.loads(await anext(lines))
                total_elapsed = time.perf_counter() - started
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    assert first == {"jsonrpc": "2.0", "id": 1, "result": "fast"}
    assert second == {"jsonrpc": "2.0", "id": 2, "result": "slow"}
    assert first_elapsed < 0.15
    assert total_elapsed >= 0.24


def test_json_batch_keeps_request_order_and_response_correlation() -> None:
    with _build_client() as client:
        payload = [
            {"jsonrpc": "2.0", "id": 11, "method": "fast"},
            {"jsonrpc": "2.0", "id": 22, "method": "slow"},
        ]

        with client.stream("POST", "/mcp", json=payload) as response:
            assert response.status_code == 200
            responses = [json.loads(line) for line in response.iter_lines() if line]

    assert [item["id"] for item in responses] == [11, 22]
    assert [item["result"] for item in responses] == ["fast", "slow"]


def test_notification_only_requests_return_202() -> None:
    with _build_client() as client:
        payload = '{"jsonrpc":"2.0","method":"notify_only"}\n'
        response = client.post("/mcp", content=payload, headers={"content-type": "application/x-ndjson"})

    assert response.status_code == 202
    assert response.content == b""
