"""Upstream manager."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

from mcpxy_proxy.plugins.registry import PluginRegistry
from mcpxy_proxy.proxy.base import UpstreamTransport


def _as_dict(settings: Any) -> dict[str, Any]:
    """Coerce a settings object to a plain dict.

    Accepts either a raw dict (legacy) or a Pydantic model (when called from
    AppConfig.upstreams which validates upstream entries into typed models).
    """
    if isinstance(settings, BaseModel):
        return settings.model_dump()
    return dict(settings)


class UpstreamManager:
    """Manage lifecycle and routing for upstream transports."""

    def __init__(
        self,
        config_upstreams: dict[str, Any],
        registry: PluginRegistry,
        oauth_manager: Any | None = None,
        config_store: Any | None = None,
    ) -> None:
        self._config_upstreams: dict[str, dict[str, Any]] = {
            name: _as_dict(settings) for name, settings in config_upstreams.items()
        }
        self._registry = registry
        self._oauth_manager = oauth_manager
        self._config_store = config_store
        self._upstreams: dict[str, UpstreamTransport] = {}

    def _instantiate(self, name: str, settings: dict[str, Any]) -> UpstreamTransport:
        """Build a transport instance, injecting runtime dependencies that
        don't belong in the persisted config dict (OAuth manager).

        The returned transport sees ``settings`` with any needed
        ``_oauth_manager`` side channel added, but the manager's own
        ``_config_upstreams`` entry stays pristine so diff comparisons
        don't see spurious changes.
        """
        cls = self._registry.validate_upstream_type(settings.get("type"))
        settings_with_runtime = dict(settings)
        if self._oauth_manager is not None:
            settings_with_runtime["_oauth_manager"] = self._oauth_manager
        if self._config_store is not None:
            settings_with_runtime["_config_store"] = self._config_store
        return cls(name, settings_with_runtime)

    async def start(self) -> None:
        """Start all configured upstream transports."""
        for name, settings in self._config_upstreams.items():
            logger.info("starting upstream=%s type=%s", name, settings.get("type"))
            transport = self._instantiate(name, settings)
            await transport.start()
            self._upstreams[name] = transport
        logger.info("all_upstreams_started count=%d", len(self._upstreams))

    async def stop(self) -> None:
        """Stop all upstreams."""
        for upstream in self._upstreams.values():
            await upstream.stop()

    async def apply_diff(self, next_upstreams: dict[str, Any]) -> dict[str, list[str]]:
        """Apply upstream configuration changes with rollback on failure."""
        current_config = {name: dict(settings) for name, settings in self._config_upstreams.items()}
        current_upstreams = dict(self._upstreams)
        # Normalize incoming entries to plain dicts so equality comparisons
        # against current_config (also dicts) work and downstream `.get()`
        # access keeps working when callers pass Pydantic-validated configs.
        next_upstreams = {name: _as_dict(settings) for name, settings in next_upstreams.items()}

        to_remove = [name for name in current_config if name not in next_upstreams]
        to_add = [name for name in next_upstreams if name not in current_config]
        to_restart = [
            name
            for name in next_upstreams
            if name in current_config and current_config[name] != next_upstreams[name]
        ]
        if to_add or to_remove or to_restart:
            logger.info(
                "apply_diff adding=%s removing=%s restarting=%s",
                to_add, to_remove, to_restart,
            )

        started_new: dict[str, UpstreamTransport] = {}
        stopped_previous: dict[str, UpstreamTransport] = {}
        try:
            # Create new/replaced upstreams before touching old ones.
            for name in to_add + to_restart:
                transport = self._instantiate(name, next_upstreams[name])
                await transport.start()
                started_new[name] = transport

            # Stop removed/replaced upstreams.
            for name in to_remove + to_restart:
                previous = self._upstreams.get(name)
                if previous is None:
                    continue
                stopped_previous[name] = previous
                await previous.stop()

            # Commit new state.
            new_live = {name: up for name, up in self._upstreams.items() if name not in (set(to_remove) | set(to_restart))}
            new_live.update(started_new)
            self._upstreams = new_live
            self._config_upstreams = {name: dict(settings) for name, settings in next_upstreams.items()}
            return {"added": to_add, "removed": to_remove, "restarted": to_restart}
        except Exception:
            logger.error(
                "apply_diff_failed rolling_back added=%s removed=%s restarted=%s",
                list(started_new.keys()), to_remove, to_restart,
            )
            for upstream in started_new.values():
                try:
                    await upstream.stop()
                except Exception:
                    pass
            # Restore previous state. Already stopped previous/replaced upstreams are restarted.
            for name, upstream in stopped_previous.items():
                try:
                    await upstream.start()
                except Exception:
                    pass
            self._upstreams = current_upstreams
            self._config_upstreams = current_config
            raise

    def get(self, name: str) -> UpstreamTransport | None:
        """Get a named upstream."""
        return self._upstreams.get(name)

    async def restart(self, name: str) -> bool:
        """Restart named upstream if it exists."""
        upstream = self._upstreams.get(name)
        if not upstream:
            logger.warning("restart_unknown upstream=%s", name)
            return False
        logger.info("restart_upstream upstream=%s", name)
        await upstream.restart()
        return True

    def health(self) -> dict[str, Any]:
        """Return health for all upstreams."""
        return {name: up.health() for name, up in self._upstreams.items()}
