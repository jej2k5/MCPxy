"""Asynchronous telemetry pipeline."""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from typing import Any, Literal

from mcp_proxy.telemetry.base import TelemetrySink


class TelemetryPipeline:
    """Queue-based non-blocking telemetry pipeline with batch flush."""

    def __init__(
        self,
        sink: TelemetrySink,
        queue_max: int = 1000,
        drop_policy: Literal["drop_oldest", "drop_newest"] = "drop_newest",
        batch_size: int = 50,
        flush_interval_ms: int = 2000,
    ) -> None:
        self.sink = sink
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_max)
        self.drop_policy = drop_policy
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_ms / 1000
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self.dropped_events = 0
        self._dropped_by_reason: dict[str, int] = defaultdict(int)
        self._dropped_by_policy: dict[str, int] = defaultdict(int)

    async def start(self) -> None:
        await self.sink.start()
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.sink.stop()

    def _record_drop(self, reason: str, policy: str) -> None:
        self.dropped_events += 1
        self._dropped_by_reason[reason] += 1
        self._dropped_by_policy[policy] += 1

    def emit_nowait(self, event: dict[str, Any]) -> bool:
        """Try enqueuing event without blocking."""
        try:
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            if self.drop_policy == "drop_oldest":
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                else:
                    self._record_drop("queue_full", "drop_oldest")
                    self.queue.put_nowait(event)
                    return True

            self._record_drop("queue_full", "drop_newest")
            return False

    async def _run(self) -> None:
        while self._running:
            batch: list[dict[str, Any]] = []
            try:
                first = await asyncio.wait_for(self.queue.get(), timeout=self.flush_interval_s)
                batch.append(first)
            except TimeoutError:
                pass
            while len(batch) < self.batch_size and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            if not batch:
                continue
            emit_batch = getattr(self.sink, "emit_batch", None)
            if callable(emit_batch):
                await emit_batch(batch)
            else:
                for event in batch:
                    await self.sink.emit(event)

    def health(self) -> dict[str, Any]:
        return {
            "queue_size": self.queue.qsize(),
            "queue_max": self.queue.maxsize,
            "drop_policy": self.drop_policy,
            "dropped_events": self.dropped_events,
            "dropped": {
                "by_reason": dict(self._dropped_by_reason),
                "by_policy": dict(self._dropped_by_policy),
            },
            "sink": self.sink.health(),
        }
