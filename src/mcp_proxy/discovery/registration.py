"""Runtime upstream registration.

Three entry points all flow through :class:`RegistrationService`, which
builds a new candidate config on top of the current one and hands it to
:class:`RuntimeConfigManager` for the usual atomic apply + rollback:

- ``POST /admin/api/upstreams`` from the dashboard / CLI / API clients.
- ``DELETE /admin/api/upstreams/{name}``.
- ``FileDropWatcher`` — drop a JSON file into ``~/.mcpy/upstreams.d/``
  and the running proxy picks it up on the next poll. Files are keyed
  by stem (filename without ``.json``) and cleaned up when the file is
  deleted.

Each JSON file in the drop directory contains a single upstream
definition. Either shape is accepted:

    {"name": "foo", "config": {"type": "stdio", "command": "bar"}}
    {"type": "stdio", "command": "bar"}  # name derived from filename

``.disabled`` files are ignored so users can temporarily pause an
upstream without deleting its definition.
"""

from __future__ import annotations

import asyncio
import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp_proxy.runtime import RuntimeConfigManager

logger = logging.getLogger(__name__)

DEFAULT_DROP_DIR = Path.home() / ".mcpy" / "upstreams.d"


class RegistrationError(ValueError):
    """Raised when a registration request is structurally invalid."""


class RegistrationService:
    """Add, remove, and list upstreams against the live :class:`RuntimeConfigManager`."""

    def __init__(self, runtime_config: RuntimeConfigManager) -> None:
        self.runtime_config = runtime_config

    def snapshot(self) -> dict[str, Any]:
        raw = self.runtime_config.raw_config.get("upstreams") or {}
        return {"upstreams": deepcopy(raw)}

    async def add(
        self,
        name: str,
        definition: dict[str, Any],
        *,
        replace: bool = False,
        source: str = "admin.register",
    ) -> dict[str, Any]:
        if not name or not isinstance(name, str):
            raise RegistrationError("upstream name must be a non-empty string")
        if not isinstance(definition, dict):
            raise RegistrationError("upstream definition must be an object")
        if "type" not in definition:
            raise RegistrationError("upstream definition missing 'type'")

        candidate = deepcopy(self.runtime_config.raw_config)
        upstreams = candidate.setdefault("upstreams", {})
        if not replace and name in upstreams:
            raise RegistrationError(f"upstream '{name}' already exists (pass replace=True to override)")
        upstreams[name] = deepcopy(definition)
        return await self.runtime_config.apply(candidate, source=source)

    async def remove(self, name: str, *, source: str = "admin.unregister") -> dict[str, Any]:
        if not name:
            raise RegistrationError("upstream name required")
        candidate = deepcopy(self.runtime_config.raw_config)
        upstreams = candidate.get("upstreams") or {}
        if name not in upstreams:
            raise RegistrationError(f"upstream '{name}' not found")
        upstreams.pop(name)
        candidate["upstreams"] = upstreams
        if candidate.get("default_upstream") == name:
            candidate["default_upstream"] = None
        return await self.runtime_config.apply(candidate, source=source)

    async def bulk_add(
        self,
        entries: list[tuple[str, dict[str, Any]]],
        *,
        replace: bool = False,
        source: str = "admin.bulk_register",
    ) -> dict[str, Any]:
        """Add multiple upstreams in a single atomic apply.

        ``entries`` is a list of ``(name, definition)`` pairs. Either all
        are applied or none (on validation failure the existing config is
        left untouched).
        """
        candidate = deepcopy(self.runtime_config.raw_config)
        upstreams = candidate.setdefault("upstreams", {})
        for name, definition in entries:
            if not name:
                raise RegistrationError("entry with empty name")
            if not isinstance(definition, dict) or "type" not in definition:
                raise RegistrationError(f"entry '{name}' missing 'type' or not an object")
            if not replace and name in upstreams:
                raise RegistrationError(f"upstream '{name}' already exists")
            upstreams[name] = deepcopy(definition)
        return await self.runtime_config.apply(candidate, source=source)


class FileDropWatcher:
    """Watch a directory of JSON files, each describing a single upstream.

    This is a lightweight mirror of the main :class:`RuntimeConfigManager`
    file watcher, but scoped to individual upstream definitions so
    operators can drop/remove files without touching the main config.
    """

    def __init__(
        self,
        service: RegistrationService,
        directory: Path | str = DEFAULT_DROP_DIR,
        *,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.service = service
        self.directory = Path(directory)
        self.poll_interval_s = poll_interval_s
        self._task: asyncio.Task[None] | None = None
        self._known: dict[str, float] = {}  # stem -> mtime
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=self.poll_interval_s * 3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        # Emit one initial scan immediately so dropped files are picked
        # up on startup without waiting a full poll interval.
        await self._scan_once()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_s)
                return
            except asyncio.TimeoutError:
                pass
            await self._scan_once()

    async def _scan_once(self) -> None:
        try:
            present = {
                p.stem: p.stat().st_mtime
                for p in self.directory.glob("*.json")
                if p.is_file()
            }
        except FileNotFoundError:
            return

        # Deletions: files removed from disk since last scan.
        for stem in list(self._known):
            if stem not in present:
                self._known.pop(stem, None)
                try:
                    await self.service.remove(stem, source="file_drop.delete")
                    logger.info("file-drop: removed upstream '%s'", stem)
                except RegistrationError:
                    pass
                except Exception as exc:  # pragma: no cover - defensive
                    logger.error("file-drop removal for '%s' failed: %s", stem, exc)

        # Additions + changes.
        for stem, mtime in present.items():
            if self._known.get(stem) == mtime:
                continue
            path = self.directory / f"{stem}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("file-drop: could not read %s: %s", path, exc)
                continue
            name, definition = _normalize_drop_payload(stem, payload)
            if definition is None:
                logger.error("file-drop: %s is not a valid upstream definition", path)
                continue
            try:
                result = await self.service.add(
                    name,
                    definition,
                    replace=True,
                    source="file_drop.add",
                )
            except RegistrationError as exc:
                logger.error("file-drop: registration error for '%s': %s", name, exc)
                continue
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("file-drop: apply error for '%s': %s", name, exc)
                continue
            if result.get("applied"):
                self._known[stem] = mtime
                logger.info("file-drop: registered upstream '%s' from %s", name, path)
            else:
                logger.error(
                    "file-drop: registration for '%s' rejected: %s",
                    name,
                    result.get("error", "unknown error"),
                )


def _normalize_drop_payload(
    stem: str,
    payload: Any,
) -> tuple[str, dict[str, Any] | None]:
    if not isinstance(payload, dict):
        return stem, None
    if "type" in payload and "config" not in payload:
        return stem, payload
    name = str(payload.get("name") or stem)
    config = payload.get("config")
    if isinstance(config, dict):
        return name, config
    return name, None
