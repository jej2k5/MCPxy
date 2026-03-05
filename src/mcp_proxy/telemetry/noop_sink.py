"""No-op telemetry sink plugin."""

from __future__ import annotations

from typing import Any

from mcp_proxy.telemetry.base import TelemetrySink


class NoopTelemetrySink(TelemetrySink):
    """Sink that discards events."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def emit(self, event: dict[str, Any]) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {"type": "noop", "ok": True}
