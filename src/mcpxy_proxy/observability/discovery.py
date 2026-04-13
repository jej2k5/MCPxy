"""Opportunistic route discovery: ask each upstream for its tool list."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from mcpxy_proxy.proxy.manager import UpstreamManager

_LOG = logging.getLogger(__name__)


class RouteDiscoverer:
    """Periodically queries each upstream for its advertised MCP methods.

    Uses the MCP `tools/list` method, which most MCP servers implement.
    Results are cached and surfaced via `snapshot()` for the dashboard's
    Routes page.
    """

    def __init__(self, manager: UpstreamManager, interval_s: float = 60.0) -> None:
        self._manager = manager
        self._interval_s = interval_s
        self._cache: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Kick off the background discovery loop."""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop(), name="route-discoverer")

    async def stop(self) -> None:
        """Stop the background loop."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def refresh_now(self) -> None:
        """Force a single pass of discovery for all upstreams."""
        names = list(self._manager.health().keys())
        for name in names:
            await self._probe(name)

    async def _loop(self) -> None:
        # First pass immediately, then sleep.
        while not self._stop_event.is_set():
            try:
                await self.refresh_now()
            except Exception as exc:  # pragma: no cover - defensive
                _LOG.debug("route_discovery_pass_failed error=%s", exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return

    async def _probe(self, name: str) -> None:
        upstream = self._manager.get(name)
        if upstream is None:
            self._cache.pop(name, None)
            return
        probe_message = {
            "jsonrpc": "2.0",
            "id": f"route-discover-{name}-{int(time.time())}",
            "method": "tools/list",
            "params": {},
        }
        try:
            response = await asyncio.wait_for(upstream.request(probe_message), timeout=5.0)
        except asyncio.TimeoutError:
            _LOG.warning(
                "discovery_timeout upstream=%s", name,
                extra={"upstream": name},
            )
            self._cache[name] = {
                "updated_at": time.time(),
                "ok": False,
                "error": f"tools/list timed out after 5s",
                "tools": [],
            }
            return
        except Exception as exc:
            _LOG.warning(
                "discovery_failed upstream=%s error=%s",
                name, exc,
                extra={"upstream": name},
            )
            self._cache[name] = {
                "updated_at": time.time(),
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "tools": [],
            }
            return

        if response is None:
            self._cache[name] = {
                "updated_at": time.time(),
                "ok": False,
                "error": "upstream returned no response (process may have crashed)",
                "tools": [],
            }
            return

        if isinstance(response, dict) and "error" in response:
            rpc_err = response["error"]
            err_msg = rpc_err.get("message", str(rpc_err)) if isinstance(rpc_err, dict) else str(rpc_err)
            self._cache[name] = {
                "updated_at": time.time(),
                "ok": False,
                "error": f"JSON-RPC error: {err_msg}",
                "tools": [],
            }
            return

        tools: list[dict[str, Any]] = []
        if isinstance(response, dict) and "result" in response:
            result = response["result"]
            if isinstance(result, dict) and isinstance(result.get("tools"), list):
                for tool in result["tools"]:
                    if isinstance(tool, dict):
                        tools.append(
                            {
                                "name": tool.get("name"),
                                "description": tool.get("description"),
                            }
                        )
        self._cache[name] = {
            "updated_at": time.time(),
            "ok": True,
            "error": None,
            "tools": tools,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return the cached discovery snapshot keyed by upstream name."""
        health = self._manager.health()
        out: dict[str, Any] = {}
        for name, info in health.items():
            cached = self._cache.get(name)
            out[name] = {
                "health": info,
                "discovery": cached or {"updated_at": None, "ok": None, "tools": []},
            }
        return out
