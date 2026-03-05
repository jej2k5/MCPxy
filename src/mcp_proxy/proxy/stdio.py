"""Stdio upstream transport plugin."""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from typing import Any

from mcp_proxy.proxy.base import UpstreamTransport


class StdioUpstreamTransport(UpstreamTransport):
    """Persistent stdio subprocess transport with NDJSON framing."""

    def __init__(self, name: str, settings: dict[str, Any]) -> None:
        self.name = name
        self.command = settings["command"]
        self.args = settings.get("args", [])
        self.queue_size = int(settings.get("queue_size", 200))
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future[dict[str, Any] | None]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._running = False
        self._restart_attempts = 0

    async def start(self) -> None:
        await self._spawn()
        self._running = True

    async def _spawn(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._reader())

    async def stop(self) -> None:
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        self._flush_pending()

        if self._proc and self._proc.stdin:
            self._proc.stdin.close()
            with contextlib.suppress(Exception):
                await self._proc.stdin.wait_closed()

        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=1.0)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def _maybe_restart(self) -> None:
        if not self._running:
            return
        self._restart_attempts += 1
        delay = min(5.0, 0.1 * (2**self._restart_attempts)) + random.random() * 0.1
        await asyncio.sleep(delay)
        await self._spawn()

    async def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        while self._running and self._proc and self._proc.stdout:
            line = await self._proc.stdout.readline()
            if not line:
                self._flush_pending()
                await self._maybe_restart()
                return
            msg = json.loads(line.decode("utf-8"))
            msg_id = msg.get("id")
            if msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if not fut.done():
                    fut.set_result(msg)

    def _flush_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(None)
        self._pending.clear()

    async def request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not self._proc or not self._proc.stdin:
            return None
        msg_id = message.get("id")
        if msg_id is None:
            await self.send_notification(message)
            return None
        fut: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        async with self._write_lock:
            self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        return await fut

    async def send_notification(self, message: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            return
        async with self._write_lock:
            self._proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()

    def health(self) -> dict[str, Any]:
        return {
            "type": "stdio",
            "running": bool(self._proc and self._proc.returncode is None),
            "restart_attempts": self._restart_attempts,
            "pending_requests": len(self._pending),
        }
