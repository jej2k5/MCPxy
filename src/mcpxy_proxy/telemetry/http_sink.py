"""HTTP telemetry sink plugin."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from mcpxy_proxy.telemetry.base import TelemetrySink


class HttpTelemetrySink(TelemetrySink):
    """Send telemetry batches to an HTTP endpoint."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.endpoint = settings.get("endpoint")
        self.headers = settings.get("headers", {})
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=10)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
        self._client = None

    async def emit(self, event: dict[str, Any]) -> None:
        await self.emit_batch([event])

    async def emit_batch(self, events: list[dict[str, Any]]) -> None:
        if not self._client or not self.endpoint:
            return
        for attempt in range(4):
            try:
                await self._client.post(self.endpoint, json={"events": events}, headers=self.headers)
                return
            except Exception:
                await asyncio.sleep((2**attempt) * 0.1 + random.random() * 0.05)

    def health(self) -> dict[str, Any]:
        return {"type": "http", "endpoint": self.endpoint, "started": self._client is not None}
