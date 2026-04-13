"""Runtime configuration application — DB-backed.

The runtime config manager owns the in-memory ``AppConfig`` and is the
single chokepoint for replacing it. Compared to the file-based
predecessor:

- The mtime-polling file watcher is **gone**. The DB is the source of
  truth, and admin API writes land there directly. Operators who used
  to ``vim config.json`` now go through the dashboard or the
  ``mcpxy-proxy`` CLI subcommands (``register``, ``catalog install``,
  ``config import``).
- ``apply()`` persists every successful swap to ``ConfigStore`` before
  acknowledging it. The history table gives us audit + rollback for
  free.
- ``ConfigStore`` is optional — tests that exercise rollback or hot
  reload semantics can construct a manager without one and the persist
  step is skipped. Production paths always supply one (the CLI builds
  it in ``cli.build_state``).

Everything else (atomicity, rollback on transport-start failure,
${secret:NAME} pre-validation, the post-apply hook used by AppState to
re-sync OAuthManager) is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Any, Callable

from mcpxy_proxy.config import (
    AppConfig,
    SecretResolver,
    _apply_expansions,
    find_secret_references,
    validate_config_payload,
)
from mcpxy_proxy.plugins.registry import PluginRegistry
from mcpxy_proxy.policy.engine import PolicyEngine
from mcpxy_proxy.proxy.manager import UpstreamManager
from mcpxy_proxy.storage.config_store import ConfigStore
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline

logger = logging.getLogger(__name__)


class RuntimeConfigManager:
    """Apply config updates atomically and persist them to the store.

    The historical ``config_path`` argument is preserved for backwards
    compatibility but is now informational only — the file is never
    polled and never written. Pass a ``store`` argument to actually
    persist applies; without one, the manager runs in "in-memory only"
    mode (used by tests that don't care about persistence).
    """

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
        store: ConfigStore | None = None,
    ) -> None:
        self.raw_config = raw_config
        self.config = config
        self.manager = manager
        self.telemetry = telemetry
        self.registry = registry
        # Kept around for diagnostics / migration logging only.
        self.config_path = config_path
        # Reserved for tests that still pass it; ignored by the runtime.
        self.poll_interval_s = poll_interval_s
        self.policy_engine = policy_engine
        # Resolver for ${secret:NAME} placeholders used at apply time.
        # Production callers wire this to ``store.get_secret`` so the
        # same ConfigStore that persists secrets also resolves them.
        self.secrets_resolver = secrets_resolver
        # Optional post-apply hook fired with the new AppConfig on
        # successful applies. AppState uses it to re-sync OAuthManager
        # upstream registrations across hot-reloads.
        self.on_config_applied: Callable[[AppConfig], None] | None = on_config_applied
        self.store = store
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Lifecycle hook kept for compatibility with the file-watcher era.

        With the DB as source of truth there's nothing to start watching:
        admin API writes land in the store directly, and other processes
        editing the same DB row aren't a use case the proxy supports.
        """
        return None

    async def stop(self) -> None:
        return None

    async def apply(
        self,
        candidate: dict[str, Any],
        dry_run: bool = False,
        source: str = "admin",
    ) -> dict[str, Any]:
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
        # TLS settings fundamentally cannot hot-reload: the uvicorn socket
        # was already created with (or without) an SSL context at startup.
        # Reject changes instead of silently ignoring them so operators get
        # a clear signal to restart.
        if self.config.tls != next_config.tls:
            return {
                "applied": False,
                "error": "tls config changes require a server restart",
                "rolled_back": True,
            }
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
                if self.store is not None:
                    # Persist the unexpanded payload so secret placeholders
                    # are preserved across restarts. The store re-syncs the
                    # denormalised upstreams table inside the same txn.
                    try:
                        version = self.store.save_active_config(
                            candidate, source=source
                        )
                        diff["version"] = version
                    except Exception as exc:  # pragma: no cover - defensive
                        # Persistence failure rolls the in-memory state back
                        # so the live config still matches the DB.
                        logger.error("config persist failed: %s", exc)
                        raise
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
                error_msg = f"{type(exc).__name__}: {exc}"
                return {"applied": False, "error": error_msg, "rolled_back": True, "diff": diff}

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
            "tls_changed": current.tls != nxt.tls,
            "upstreams": {
                "added": sorted([k for k in next_up if k not in current_up]),
                "removed": sorted([k for k in current_up if k not in next_up]),
                "restarted": sorted([k for k in next_up if k in current_up and next_up[k] != current_up[k]]),
            },
        }
