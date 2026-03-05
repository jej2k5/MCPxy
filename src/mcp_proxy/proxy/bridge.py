"""Bridge for forwarding JSON-RPC messages to resolved upstreams."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp_proxy.jsonrpc import JsonRpcError, is_notification
from mcp_proxy.proxy.manager import UpstreamManager


class ProxyBridge:
    """Request forwarding bridge with backpressure controls."""

    def __init__(self, manager: UpstreamManager, queue_size: int = 1000) -> None:
        self.manager = manager
        self.queue: asyncio.Queue[int] = asyncio.Queue(maxsize=queue_size)

    async def forward(self, upstream_name: str, message: dict[str, Any]) -> dict[str, Any] | None:
        """Forward a message to an upstream."""
        try:
            self.queue.put_nowait(1)
        except asyncio.QueueFull as exc:
            raise JsonRpcError(-32002, "proxy_overloaded", request_id=message.get("id")) from exc

        upstream = self.manager.get(upstream_name)
        if not upstream:
            self.queue.get_nowait()
            raise JsonRpcError(-32001, "upstream_unavailable", request_id=message.get("id"))

        try:
            if is_notification(message):
                await upstream.send_notification(message)
                return None
            response = await upstream.request(message)
            if response is None:
                raise JsonRpcError(-32001, "upstream_unavailable", request_id=message.get("id"))
            return response
        finally:
            self.queue.get_nowait()
