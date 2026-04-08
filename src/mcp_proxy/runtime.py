"""Runtime configuration reload and change application."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from mcp_proxy.config import (
    AppConfig,
    SecretResolver,
    _apply_expansions,
    find_secret_references,
    validate_config_payload,
)
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.policy.engine import PolicyEngine
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.telemetry.pipeline import TelemetryPipeline

logger = logging.getLogger(__name__)


class RuntimeConfigManager:
    """Apply config updates atomically and optionally watch a config file for changes."""

    def __init__(
        self,
        raw_config: dict[str, Any],
        config: AppConfig,
        manager: UpstreamManager,
        telemetry: TelemetryPipeline,
        registry: PluginRegistry,
        config_path: str | None = None,
        poll_interval_s: float = 0.5,
        policy_engine: PolicyEngine | None = None,
        secrets_resolver: SecretResolver | None = None,
        on_config_applied: Callable[[AppConfig], None] | None = None,
    ) -> None:
        self.raw_config = raw_config
        self.config = config
        self.manager = manager
        self.telemetry = telemetry
        self.registry = registry
        self.config_path = config_path
        self.poll_interval_s = poll_interval_s
        self.policy_engine = policy_engine
        # Resolver for ${secret:NAME} placeholders used at apply time. The
        # server's AppState wires this to SecretsManager.get; tests and
        # simple call sites can leave it None and lose only secret support.
        self.secrets_resolver = secrets_resolver
        # Optional post-apply hook: called with the *new* AppConfig after
        # a successful apply. Used by AppState to sync dependent state
        # (e.g. OAuthManager upstream registrations) without needing the
        # runtime manager to know about auth.
        self.on_config_applied: Callable[[AppConfig], None] | None = on_config_applied
        self._lock = asyncio.Lock()
        self._watch_task: asyncio.Task[None] | None = None
        self._last_mtime_ns: int | None = None

    async def start(self) -> None:
        if not self.config_path:
            return
        path = Path(self.config_path)
        self._last_mtime_ns = path.stat().st_mtime_ns if path.exists() else None
        self._watch_task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        if not self._watch_task:
            return
        self._watch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._watch_task

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_s)
            path = Path(self.config_path or "")
            try:
                new_mtime = path.stat().st_mtime_ns
            except FileNotFoundError:
                continue
            if self._last_mtime_ns == new_mtime:
                continue
            self._last_mtime_ns = new_mtime
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error("Failed to parse config file reload: %s", exc)
                continue
            result = await self.apply(payload, source="file_watcher")
            if not result.get("applied"):
                logger.error("Config reload rejected: %s", result.get("error", "unknown error"))
            else:
                logger.info("Config reload applied via file watcher")

    async def apply(self, candidate: dict[str, Any], dry_run: bool = False, source: str = "admin") -> dict[str, Any]:
        # Fail fast on unresolved ${secret:NAME} references: the expansion
        # layer would otherwise silently substitute an empty string, which
        # turns into a very confusing "my upstream just rejects all
        # requests" bug far away from this call site.
        if self.secrets_resolver is not None:
            missing: list[str] = []
            for name in find_secret_references(candidate):
                if self.secrets_resolver(name) is None:
                    missing.append(name)
            if missing:
                return {
                    "applied": False,
                    "error": (
                        "missing secret(s): "
                        + ", ".join(missing)
                        + ". Create them via POST /admin/api/secrets "
                          "before applying this config."
                    ),
                    "rolled_back": True,
                }

        ok, error = validate_config_payload(candidate, secrets=self.secrets_resolver)
        if not ok:
            return {"applied": False, "error": error, "rolled_back": True}

        expanded = _apply_expansions(deepcopy(candidate), secrets=self.secrets_resolver)
        next_config = AppConfig.model_validate(expanded)
        diff = self._compute_diff(self.config, next_config)
        if dry_run:
            return {"applied": False, "dry_run": True, "rolled_back": False, "diff": diff}

        async with self._lock:
            backup_raw = deepcopy(self.raw_config)
            backup_config = self.config
            backup_telemetry = self.telemetry
            try:
                await self._apply_telemetry_if_needed(next_config)
                upstream_diff = await self.manager.apply_diff(next_config.upstreams)

                self.raw_config.clear()
                self.raw_config.update(deepcopy(candidate))
                self.config = next_config
                if self.policy_engine is not None:
                    self.policy_engine.replace_config(next_config)
                if self.on_config_applied is not None:
                    try:
                        self.on_config_applied(next_config)
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning(
                            "on_config_applied hook raised: %s", exc
                        )
                diff["upstreams"] = upstream_diff
                diff["policies_changed"] = backup_config.policies != next_config.policies
                return {"applied": True, "rolled_back": False, "diff": diff, "source": source}
            except Exception as exc:
                self.raw_config.clear()
                self.raw_config.update(backup_raw)
                self.config = backup_config
                if self.telemetry is not backup_telemetry:
                    self.telemetry = backup_telemetry
                if self.policy_engine is not None:
                    self.policy_engine.replace_config(backup_config)
                return {"applied": False, "error": str(exc), "rolled_back": True, "diff": diff}

    async def _apply_telemetry_if_needed(self, next_config: AppConfig) -> None:
        if self.config.telemetry == next_config.telemetry:
            return
        await self.telemetry.stop()
        sink_name = next_config.telemetry.sink
        sink_cls = self.registry.validate_telemetry_sink_type(sink_name)
        sink = sink_cls() if sink_name == "noop" else sink_cls(next_config.telemetry.model_dump())

        new_pipeline = TelemetryPipeline(
            sink=sink,
            queue_max=next_config.telemetry.queue_max,
            drop_policy=next_config.telemetry.drop_policy,
            batch_size=next_config.telemetry.batch_size,
            flush_interval_ms=next_config.telemetry.flush_interval_ms,
        )
        await new_pipeline.start()
        self.telemetry = new_pipeline

    @staticmethod
    def _compute_diff(current: AppConfig, nxt: AppConfig) -> dict[str, Any]:
        current_up = current.upstreams
        next_up = nxt.upstreams
        return {
            "default_upstream_changed": current.default_upstream != nxt.default_upstream,
            "telemetry_changed": current.telemetry != nxt.telemetry,
            "upstreams": {
                "added": sorted([k for k in next_up if k not in current_up]),
                "removed": sorted([k for k in current_up if k not in next_up]),
                "restarted": sorted([k for k in next_up if k in current_up and next_up[k] != current_up[k]]),
            },
        }
