"""HTTP upstream transport plugin."""

from __future__ import annotations

from typing import Any

import httpx

from mcp_proxy.proxy.base import UpstreamTransport


class HttpUpstreamTransport(UpstreamTransport):
    """JSON-RPC transport over HTTP POST."""

    def __init__(self, name: str, settings: dict[str, Any]) -> None:
        self.name = name
        self.url = settings["url"]
        self.timeout_s = float(settings.get("timeout_s", 30.0))
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=self.timeout_s)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
        self._client = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not self._client:
            raise RuntimeError("http transport not started")
        resp = await self._client.post(self.url, json=message)
        if not resp.content:
            return None
        return resp.json()

    async def send_notification(self, message: dict[str, Any]) -> None:
        if not self._client:
            raise RuntimeError("http transport not started")
        await self._client.post(self.url, json=message)

    def health(self) -> dict[str, Any]:
        return {"type": "http", "url": self.url, "started": self._client is not None}
