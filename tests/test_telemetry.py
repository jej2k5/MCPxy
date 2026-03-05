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
async def test_telemetry_queue_drop() -> None:
    sink = CollectSink()
    pipe = TelemetryPipeline(sink, queue_size=1, batch_size=1, flush_interval_ms=1000)
    assert pipe.emit_nowait({"e": 1}) is True
    assert pipe.emit_nowait({"e": 2}) is False
    assert pipe.dropped_events == 1


@pytest.mark.asyncio
async def test_telemetry_flush() -> None:
    sink = CollectSink()
    pipe = TelemetryPipeline(sink, queue_size=10, batch_size=2, flush_interval_ms=50)
    await pipe.start()
    pipe.emit_nowait({"e": 1})
    await asyncio.sleep(0.1)
    await pipe.stop()
    assert sink.events
