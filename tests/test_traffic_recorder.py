import asyncio
import time

import pytest

from mcp_proxy.observability.traffic import TrafficRecord, TrafficRecorder


def _rec(
    *,
    upstream: str = "u",
    method: str = "tools/call",
    status: str = "ok",
    latency_ms: float = 10.0,
    request_bytes: int = 0,
    response_bytes: int = 0,
    error_code: str | None = None,
    timestamp: float | None = None,
) -> TrafficRecord:
    return TrafficRecord(
        timestamp=timestamp if timestamp is not None else time.time(),
        upstream=upstream,
        method=method,
        request_id=1,
        status=status,
        latency_ms=latency_ms,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        error_code=error_code,
    )


def test_record_appends_and_returns_recent_newest_first() -> None:
    tr = TrafficRecorder(maxlen=10)
    for i in range(5):
        tr.record(_rec(method=f"m{i}", latency_ms=float(i)))
    items = tr.recent(limit=3)
    assert [it["method"] for it in items] == ["m4", "m3", "m2"]


def test_recent_filters_by_upstream_method_status() -> None:
    tr = TrafficRecorder(maxlen=50)
    tr.record(_rec(upstream="a", method="x", status="ok"))
    tr.record(_rec(upstream="a", method="y", status="error"))
    tr.record(_rec(upstream="b", method="x", status="ok"))
    tr.record(_rec(upstream="b", method="y", status="denied"))

    assert len(tr.recent(upstream="a")) == 2
    assert len(tr.recent(method="x")) == 2
    assert len(tr.recent(status="denied")) == 1
    assert len(tr.recent(upstream="a", method="y")) == 1


def test_buffer_drops_oldest_when_full() -> None:
    tr = TrafficRecorder(maxlen=3)
    for i in range(5):
        tr.record(_rec(method=f"m{i}"))
    methods = [it["method"] for it in tr.recent(limit=10)]
    assert methods == ["m4", "m3", "m2"]


def test_metrics_aggregates_per_upstream_and_computes_percentiles() -> None:
    tr = TrafficRecorder(maxlen=100)
    now = time.time()
    for i in range(10):
        tr.record(
            _rec(
                upstream="a",
                status="ok" if i % 5 != 0 else "error",
                latency_ms=float(i * 10),
                timestamp=now,
            )
        )
    for i in range(5):
        tr.record(_rec(upstream="b", status="ok", latency_ms=float(i + 1), timestamp=now))

    m = tr.metrics()
    assert m["total"] == 15
    assert m["errors"] == 2
    assert round(m["error_rate"], 4) == round(2 / 15, 4)
    assert m["per_upstream"]["a"]["total"] == 10
    assert m["per_upstream"]["a"]["errors"] == 2
    assert m["per_upstream"]["a"]["latency_p95_ms"] >= m["per_upstream"]["a"]["latency_p50_ms"]
    assert m["per_upstream"]["b"]["total"] == 5


def test_metrics_respects_window() -> None:
    tr = TrafficRecorder(maxlen=100)
    tr.record(_rec(timestamp=time.time() - 600))  # outside 5 min window
    tr.record(_rec(timestamp=time.time()))
    assert tr.metrics()["total"] == 1


@pytest.mark.asyncio
async def test_subscribe_fanout_delivers_records() -> None:
    tr = TrafficRecorder(maxlen=10)
    sub = tr.subscribe()

    tr.record(_rec(method="live1"))
    tr.record(_rec(method="live2"))

    got1 = await asyncio.wait_for(sub.get(), timeout=0.5)
    got2 = await asyncio.wait_for(sub.get(), timeout=0.5)
    assert got1.method == "live1"
    assert got2.method == "live2"
    sub.close()


@pytest.mark.asyncio
async def test_subscribe_drops_events_for_slow_subscriber() -> None:
    tr = TrafficRecorder(maxlen=100, subscriber_queue_max=2)
    sub = tr.subscribe()

    for i in range(50):
        tr.record(_rec(method=f"m{i}"))

    # Subscriber queue is only 2 items; the remaining records are dropped
    # for the subscriber but still retained in the ring buffer.
    assert tr.metrics()["dropped_for_subscribers"] > 0
    assert tr.metrics()["buffer_size"] == 50

    received: list[str] = []
    for _ in range(2):
        rec = await asyncio.wait_for(sub.get(), timeout=0.5)
        received.append(rec.method or "")
    assert len(received) == 2
    sub.close()


def test_traffic_record_to_dict_is_json_compatible() -> None:
    rec = _rec(upstream="a", method="m", status="error", error_code="boom")
    d = rec.to_dict()
    assert d["upstream"] == "a"
    assert d["status"] == "error"
    assert d["error_code"] == "boom"
