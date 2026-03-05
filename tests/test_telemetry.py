import asyncio

import pytest

from mcp_proxy.telemetry.base import TelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


class CollectSink(TelemetrySink):
    def __init__(self):
        self.events = []

    async def start(self):
        return None

    async def stop(self):
        return None

    async def emit(self, event):
        self.events.append(event)

    def health(self):
        return {"ok": True}


@pytest.mark.asyncio
async def test_telemetry_queue_drop_newest() -> None:
    sink = CollectSink()
    pipe = TelemetryPipeline(sink, queue_max=1, drop_policy="drop_newest", batch_size=1, flush_interval_ms=1000)
    assert pipe.emit_nowait({"e": 1}) is True
    assert pipe.emit_nowait({"e": 2}) is False
    health = pipe.health()
    assert pipe.dropped_events == 1
    assert health["dropped"]["by_reason"] == {"queue_full": 1}
    assert health["dropped"]["by_policy"] == {"drop_newest": 1}


@pytest.mark.asyncio
async def test_telemetry_queue_drop_oldest() -> None:
    sink = CollectSink()
    pipe = TelemetryPipeline(sink, queue_max=1, drop_policy="drop_oldest", batch_size=1, flush_interval_ms=1000)
    assert pipe.emit_nowait({"e": 1}) is True
    assert pipe.emit_nowait({"e": 2}) is True
    assert pipe.queue.get_nowait() == {"e": 2}
    health = pipe.health()
    assert pipe.dropped_events == 1
    assert health["dropped"]["by_reason"] == {"queue_full": 1}
    assert health["dropped"]["by_policy"] == {"drop_oldest": 1}


@pytest.mark.asyncio
async def test_telemetry_flush() -> None:
    sink = CollectSink()
    pipe = TelemetryPipeline(sink, queue_max=10, batch_size=2, flush_interval_ms=50)
    await pipe.start()
    pipe.emit_nowait({"e": 1})
    await asyncio.sleep(0.1)
    await pipe.stop()
    assert sink.events
