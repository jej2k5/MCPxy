"""Bridge for forwarding JSON-RPC messages to resolved upstreams."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from mcp_proxy.jsonrpc import JsonRpcError, is_notification
from mcp_proxy.observability.traffic import TrafficRecord
from mcp_proxy.policy.engine import PolicyEngine
from mcp_proxy.proxy.manager import UpstreamManager


@dataclass(frozen=True)
class RequestContext:
    """Per-request context threaded from server → bridge → transport.

    Carries the authenticated principal and the raw incoming bearer
    token so the token transformation policy can map client identity
    to upstream credentials.
    """

    user_id: int | None = None
    email: str | None = None
    role: str | None = None
    incoming_bearer: str | None = None


class ProxyBridge:
    """Request forwarding bridge with backpressure controls."""

    def __init__(self, manager: UpstreamManager, queue_size: int = 1000) -> None:
        self.manager = manager
        self.queue: asyncio.Queue[int] = asyncio.Queue(maxsize=queue_size)
        self._shutdown_event = asyncio.Event()
        self._telemetry_emit: Any | None = None
        self._traffic_recorder: Callable[[TrafficRecord], None] | None = None
        self._policy_engine: PolicyEngine | None = None

    def set_telemetry_emitter(self, emit: Any) -> None:
        """Attach a telemetry emission callable."""
        self._telemetry_emit = emit

    def set_traffic_recorder(self, recorder: Callable[[TrafficRecord], None]) -> None:
        """Attach a traffic recording callable (called once per forwarded request)."""
        self._traffic_recorder = recorder

    def set_policy_engine(self, engine: PolicyEngine) -> None:
        """Attach a policy engine for per-request access control."""
        self._policy_engine = engine

    def start_shutdown(self) -> None:
        """Mark bridge as shutting down and reject new/in-flight forwards."""
        self._shutdown_event.set()

    def _emit(self, event: dict[str, Any]) -> None:
        if callable(self._telemetry_emit):
            self._telemetry_emit(event)

    def _build_record(
        self,
        *,
        upstream: str,
        message: dict[str, Any],
        status: str,
        started_at: float,
        request_bytes: int,
        client_ip: str | None,
        response: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> TrafficRecord:
        latency_ms = round((time.monotonic() - started_at) * 1000.0, 3)
        response_bytes = 0
        if response is not None:
            try:
                response_bytes = len(json.dumps(response).encode("utf-8"))
            except (TypeError, ValueError):
                response_bytes = 0
        return TrafficRecord(
            timestamp=time.time(),
            upstream=upstream,
            method=message.get("method") if isinstance(message, dict) else None,
            request_id=message.get("id") if isinstance(message, dict) else None,
            status=status,
            latency_ms=latency_ms,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            error_code=error_code,
            client_ip=client_ip,
        )

    def _emit_record(self, rec: TrafficRecord) -> None:
        if self._traffic_recorder is None:
            return
        try:
            self._traffic_recorder(rec)
        except Exception:
            # Never let observability break the request path.
            pass

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

    async def forward(
        self,
        upstream_name: str,
        message: dict[str, Any],
        *,
        request_bytes: int = 0,
        client_ip: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any] | None:
        """Forward a message to an upstream."""
        started_at = time.monotonic()

        def record_error(error_code: str) -> None:
            self._emit_record(
                self._build_record(
                    upstream=upstream_name,
                    message=message,
                    status="error",
                    started_at=started_at,
                    request_bytes=request_bytes,
                    client_ip=client_ip,
                    error_code=error_code,
                )
            )

        if self._shutdown_event.is_set():
            record_error("proxy_shutting_down")
            raise self._shutdown_error(message, upstream_name, "shutdown_reject_new")

        if self._policy_engine is not None:
            decision = self._policy_engine.check(
                upstream=upstream_name,
                message=message,
                request_bytes=request_bytes,
                client_ip=client_ip,
            )
            if not decision.allowed:
                reason = decision.reason or "policy_blocked"
                self._emit_record(
                    self._build_record(
                        upstream=upstream_name,
                        message=message,
                        status="denied",
                        started_at=started_at,
                        request_bytes=request_bytes,
                        client_ip=client_ip,
                        error_code=reason,
                    )
                )
                raise JsonRpcError(
                    -32003,
                    f"policy_blocked:{reason}",
                    request_id=message.get("id"),
                )

        try:
            self.queue.put_nowait(1)
        except asyncio.QueueFull as exc:
            record_error("proxy_overloaded")
            raise JsonRpcError(-32002, "proxy_overloaded", request_id=message.get("id")) from exc

        upstream = self.manager.get(upstream_name)
        if not upstream:
            self.queue.get_nowait()
            record_error("upstream_unavailable")
            raise JsonRpcError(-32001, "upstream_unavailable", request_id=message.get("id"))

        try:
            if self._shutdown_event.is_set():
                record_error("proxy_shutting_down")
                raise self._shutdown_error(message, upstream_name, "shutdown_reject_in_flight")
            if is_notification(message):
                await upstream.send_notification(message, context=context)
                self._emit_record(
                    self._build_record(
                        upstream=upstream_name,
                        message=message,
                        status="ok",
                        started_at=started_at,
                        request_bytes=request_bytes,
                        client_ip=client_ip,
                    )
                )
                return None

            request_task = asyncio.create_task(upstream.request(message, context=context))
            shutdown_waiter = asyncio.create_task(self._shutdown_event.wait())
            done, _ = await asyncio.wait({request_task, shutdown_waiter}, return_when=asyncio.FIRST_COMPLETED)
            if shutdown_waiter in done and self._shutdown_event.is_set():
                request_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await request_task
                record_error("proxy_shutting_down")
                raise self._shutdown_error(message, upstream_name, "shutdown_reject_in_flight")

            shutdown_waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await shutdown_waiter
            response = await request_task
            if response is None:
                record_error("upstream_unavailable")
                raise JsonRpcError(-32001, "upstream_unavailable", request_id=message.get("id"))

            is_error = isinstance(response, dict) and "error" in response
            self._emit_record(
                self._build_record(
                    upstream=upstream_name,
                    message=message,
                    status="error" if is_error else "ok",
                    started_at=started_at,
                    request_bytes=request_bytes,
                    client_ip=client_ip,
                    response=response,
                    error_code=(response.get("error") or {}).get("message") if is_error else None,
                )
            )
            return response
        finally:
            self.queue.get_nowait()
