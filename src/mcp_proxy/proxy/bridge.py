"""Bridge for forwarding JSON-RPC messages to resolved upstreams."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from mcp_proxy.jsonrpc import JsonRpcError, is_notification
from mcp_proxy.proxy.manager import UpstreamManager


class ProxyBridge:
    """Request forwarding bridge with backpressure controls."""

    def __init__(self, manager: UpstreamManager, queue_size: int = 1000) -> None:
        self.manager = manager
        self.queue: asyncio.Queue[int] = asyncio.Queue(maxsize=queue_size)
        self._shutdown_event = asyncio.Event()
        self._telemetry_emit: Any | None = None

    def set_telemetry_emitter(self, emit: Any) -> None:
        """Attach a telemetry emission callable."""
        self._telemetry_emit = emit

    def start_shutdown(self) -> None:
        """Mark bridge as shutting down and reject new/in-flight forwards."""
        self._shutdown_event.set()

    def _emit(self, event: dict[str, Any]) -> None:
        if callable(self._telemetry_emit):
            self._telemetry_emit(event)

    def _shutdown_error(self, message: dict[str, Any], upstream_name: str, reason: str) -> JsonRpcError:
        self._emit(
            {
                "event": "proxy_request_failed",
                "reason": reason,
                "upstream": upstream_name,
                "request_id": message.get("id"),
            }
        )
        return JsonRpcError(-32000, "proxy_shutting_down", request_id=message.get("id"))

    async def forward(self, upstream_name: str, message: dict[str, Any]) -> dict[str, Any] | None:
        """Forward a message to an upstream."""
        if self._shutdown_event.is_set():
            raise self._shutdown_error(message, upstream_name, "shutdown_reject_new")

        try:
            self.queue.put_nowait(1)
        except asyncio.QueueFull as exc:
            raise JsonRpcError(-32002, "proxy_overloaded", request_id=message.get("id")) from exc

        upstream = self.manager.get(upstream_name)
        if not upstream:
            self.queue.get_nowait()
            raise JsonRpcError(-32001, "upstream_unavailable", request_id=message.get("id"))

        try:
            if self._shutdown_event.is_set():
                raise self._shutdown_error(message, upstream_name, "shutdown_reject_in_flight")
            if is_notification(message):
                await upstream.send_notification(message)
                return None

            request_task = asyncio.create_task(upstream.request(message))
            shutdown_waiter = asyncio.create_task(self._shutdown_event.wait())
            done, _ = await asyncio.wait({request_task, shutdown_waiter}, return_when=asyncio.FIRST_COMPLETED)
            if shutdown_waiter in done and self._shutdown_event.is_set():
                request_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await request_task
                raise self._shutdown_error(message, upstream_name, "shutdown_reject_in_flight")

            shutdown_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await shutdown_waiter
            response = await request_task
            if response is None:
                raise JsonRpcError(-32001, "upstream_unavailable", request_id=message.get("id"))
            return response
        finally:
            self.queue.get_nowait()
