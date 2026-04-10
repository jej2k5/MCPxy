"""Traffic recording and fan-out for live dashboard streaming."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Iterable


@dataclass
class TrafficRecord:
    """Metadata-only record of a single forwarded JSON-RPC request.

    Bodies are never stored. Method name and transport metadata only.
    """

    timestamp: float
    upstream: str
    method: str | None
    request_id: Any
    status: str  # "ok" | "error" | "timeout" | "denied"
    latency_ms: float
    request_bytes: int = 0
    response_bytes: int = 0
    error_code: str | None = None
    client_ip: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TrafficRecorder:
    """In-memory traffic ring buffer with SSE fan-out.

    Uses a bounded deque for recent history and a set of per-subscriber
    asyncio.Queues for live streaming. Slow subscribers lose events instead
    of blocking the producing request path.
    """

    def __init__(self, maxlen: int = 2000, subscriber_queue_max: int = 256) -> None:
        self._buffer: deque[TrafficRecord] = deque(maxlen=maxlen)
        self._subscribers: set[asyncio.Queue[TrafficRecord]] = set()
        self._subscriber_queue_max = subscriber_queue_max
        self._dropped_for_subscribers = 0

    def record(self, rec: TrafficRecord) -> None:
        """Append a record and fan-out to subscribers. Never blocks."""
        self._buffer.append(rec)
        for q in list(self._subscribers):
            try:
                q.put_nowait(rec)
            except asyncio.QueueFull:
                self._dropped_for_subscribers += 1

    def recent(
        self,
        limit: int = 200,
        upstream: str | None = None,
        method: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent records as dicts, newest first, filtered."""
        items: Iterable[TrafficRecord] = reversed(self._buffer)

        def keep(rec: TrafficRecord) -> bool:
            if upstream and rec.upstream != upstream:
                return False
            if method and rec.method != method:
                return False
            if status and rec.status != status:
                return False
            return True

        out: list[dict[str, Any]] = []
        for rec in items:
            if keep(rec):
                out.append(rec.to_dict())
                if len(out) >= limit:
                    break
        return out

    def subscribe(self) -> "TrafficSubscription":
        """Register a subscriber and return a handle.

        The handle is registered synchronously so callers do not need to
        advance an async iterator before the first `record()` will be seen.
        Use `async for rec in handle:` or `await handle.get()` to consume.
        """
        q: asyncio.Queue[TrafficRecord] = asyncio.Queue(maxsize=self._subscriber_queue_max)
        self._subscribers.add(q)
        return TrafficSubscription(q, lambda: self._subscribers.discard(q))

    async def subscribe_iter(self) -> AsyncIterator[TrafficRecord]:
        """Back-compat async iterator. Registers synchronously."""
        sub = self.subscribe()
        try:
            while True:
                yield await sub.get()
        finally:
            sub.close()

    def metrics(self, window_s: float = 300.0) -> dict[str, Any]:
        """Return rolling per-upstream aggregates.

        Window defaults to the last 5 minutes. Percentiles are computed from
        the records currently in the buffer that fall inside the window.
        """
        now = time.time()
        cutoff = now - window_s
        per_upstream: dict[str, dict[str, Any]] = {}
        global_latencies: list[float] = []
        total = 0
        errors = 0

        for rec in self._buffer:
            if rec.timestamp < cutoff:
                continue
            total += 1
            is_error = rec.status in ("error", "timeout", "denied")
            if is_error:
                errors += 1
            global_latencies.append(rec.latency_ms)
            bucket = per_upstream.setdefault(
                rec.upstream,
                {"total": 0, "errors": 0, "_latencies": [], "by_status": {}},
            )
            bucket["total"] += 1
            if is_error:
                bucket["errors"] += 1
            bucket["_latencies"].append(rec.latency_ms)
            bucket["by_status"][rec.status] = bucket["by_status"].get(rec.status, 0) + 1

        def percentile(values: list[float], p: float) -> float:
            if not values:
                return 0.0
            values_sorted = sorted(values)
            k = max(0, min(len(values_sorted) - 1, int(round((p / 100.0) * (len(values_sorted) - 1)))))
            return round(values_sorted[k], 3)

        for name, bucket in per_upstream.items():
            lats = bucket.pop("_latencies")
            bucket["latency_p50_ms"] = percentile(lats, 50)
            bucket["latency_p95_ms"] = percentile(lats, 95)
            bucket["latency_p99_ms"] = percentile(lats, 99)

        return {
            "window_s": window_s,
            "total": total,
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "latency_p50_ms": percentile(global_latencies, 50),
            "latency_p95_ms": percentile(global_latencies, 95),
            "latency_p99_ms": percentile(global_latencies, 99),
            "per_upstream": per_upstream,
            "subscribers": len(self._subscribers),
            "dropped_for_subscribers": self._dropped_for_subscribers,
            "buffer_size": len(self._buffer),
            "buffer_max": self._buffer.maxlen,
        }

    def clear(self) -> None:
        """Clear the buffer (for tests)."""
        self._buffer.clear()


class TrafficSubscription:
    """Handle returned by :meth:`TrafficRecorder.subscribe`."""

    def __init__(self, queue: asyncio.Queue[TrafficRecord], closer: Any) -> None:
        self._queue = queue
        self._closer = closer

    async def get(self) -> TrafficRecord:
        return await self._queue.get()

    def close(self) -> None:
        try:
            self._closer()
        except Exception:
            pass

    def __aiter__(self) -> "TrafficSubscription":
        return self

    async def __anext__(self) -> TrafficRecord:
        return await self._queue.get()
