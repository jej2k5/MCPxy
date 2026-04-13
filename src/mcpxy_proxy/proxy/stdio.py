"""Stdio upstream transport plugin."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
from typing import Any

from mcpxy_proxy.proxy.base import UpstreamTransport

logger = logging.getLogger(__name__)


class StdioUpstreamTransport(UpstreamTransport):
    """Persistent stdio subprocess transport with NDJSON framing."""

    def __init__(self, name: str, settings: dict[str, Any]) -> None:
        self.name = name
        self.command = settings["command"]
        self.args = settings.get("args", [])
        # Per-upstream env overlay. Merged on top of the proxy's own env at
        # spawn time so PATH and friends still resolve, while secrets like
        # GITHUB_TOKEN or NOTION_API_KEY stay scoped to this single upstream
        # rather than leaking into siblings. Values are already expanded
        # (${env:FOO} / ${secret:NAME}) by load_config before we see them.
        self.env_overlay: dict[str, str] = {
            str(k): str(v) for k, v in (settings.get("env") or {}).items()
        }
        self.queue_size = int(settings.get("queue_size", 200))
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future[dict[str, Any] | None]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._running = False
        self._restart_attempts = 0
        self._last_error: str | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._spawn()
        self._running = True

    def _build_env(self) -> dict[str, str] | None:
        if not self.env_overlay:
            return None
        merged = dict(os.environ)
        merged.update(self.env_overlay)
        return merged

    async def _spawn(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            self._last_error = str(exc)
            logger.error(
                "spawn_failed upstream=%s command=%s error=%s",
                self.name, self.command, exc,
                extra={"upstream": self.name},
            )
            raise
        logger.info(
            "spawned upstream=%s pid=%d command=%s",
            self.name, self._proc.pid, self.command,
            extra={"upstream": self.name},
        )
        self._reader_task = asyncio.create_task(self._reader())
        self._stderr_task = asyncio.create_task(self._stderr_reader())

    async def stop(self) -> None:
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None

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
        exit_code = self._proc.returncode if self._proc else None
        self._restart_attempts += 1
        delay = min(5.0, 0.1 * (2**self._restart_attempts)) + random.random() * 0.1
        logger.warning(
            "restarting upstream=%s exit_code=%s attempt=%d delay=%.2fs last_error=%s",
            self.name, exit_code, self._restart_attempts, delay, self._last_error,
            extra={"upstream": self.name},
        )
        await asyncio.sleep(delay)
        try:
            await self._spawn()
        except Exception:
            pass  # _spawn already logged; next reader EOF will retry

    async def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        while self._running and self._proc and self._proc.stdout:
            line = await self._proc.stdout.readline()
            if not line:
                self._flush_pending()
                await self._maybe_restart()
                return
            try:
                msg = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning(
                    "invalid_json upstream=%s error=%s line=%r",
                    self.name, exc, line[:200],
                    extra={"upstream": self.name},
                )
                continue
            msg_id = msg.get("id")
            if msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if not fut.done():
                    fut.set_result(msg)

    async def _stderr_reader(self) -> None:
        """Drain subprocess stderr and log each line."""
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            self._last_error = text
            logger.warning(
                "upstream_stderr upstream=%s line=%s",
                self.name, text,
                extra={"upstream": self.name},
            )

    def _flush_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(None)
        self._pending.clear()

    async def request(self, message: dict[str, Any], context: Any = None) -> dict[str, Any] | None:
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

    async def send_notification(self, message: dict[str, Any], context: Any = None) -> None:
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
            "last_error": self._last_error,
        }
