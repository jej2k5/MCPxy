"""Upstream and plugin manager."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from mcp_proxy.proxy.base import UpstreamTransport
from mcp_proxy.proxy.http import HttpUpstreamTransport
from mcp_proxy.proxy.stdio import StdioUpstreamTransport
from mcp_proxy.telemetry.http_sink import HttpTelemetrySink
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink


class PluginRegistry:
    """Registry for built-in and entry-point plugins."""

    def __init__(self) -> None:
        self.upstreams: dict[str, type[UpstreamTransport]] = {
            "stdio": StdioUpstreamTransport,
            "http": HttpUpstreamTransport,
        }
        self.telemetry_sinks: dict[str, Any] = {"http": HttpTelemetrySink, "noop": NoopTelemetrySink}

    def load_entry_points(self) -> None:
        """Load plugin entry points from installed distributions."""
        for ep in entry_points(group="mcp_proxy.upstreams"):
            self.upstreams[ep.name] = ep.load()
        for ep in entry_points(group="mcp_proxy.telemetry_sinks"):
            self.telemetry_sinks[ep.name] = ep.load()


class UpstreamManager:
    """Manage lifecycle and routing for upstream transports."""

    def __init__(self, config_upstreams: dict[str, dict[str, Any]], registry: PluginRegistry) -> None:
        self._config_upstreams = config_upstreams
        self._registry = registry
        self._upstreams: dict[str, UpstreamTransport] = {}

    async def start(self) -> None:
        """Start all configured upstream transports."""
        for name, settings in self._config_upstreams.items():
            t_name = settings.get("type")
            cls = self._registry.upstreams.get(t_name)
            if cls is None:
                raise ValueError(f"Unknown upstream type: {t_name}")
            transport = cls(name, settings)
            await transport.start()
            self._upstreams[name] = transport

    async def stop(self) -> None:
        """Stop all upstreams."""
        for upstream in self._upstreams.values():
            await upstream.stop()

    async def apply_diff(self, next_upstreams: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
        """Apply upstream configuration changes with rollback on failure."""
        current_config = {name: dict(settings) for name, settings in self._config_upstreams.items()}
        current_upstreams = dict(self._upstreams)

        to_remove = [name for name in current_config if name not in next_upstreams]
        to_add = [name for name in next_upstreams if name not in current_config]
        to_restart = [
            name
            for name in next_upstreams
            if name in current_config and current_config[name] != next_upstreams[name]
        ]

        started_new: dict[str, UpstreamTransport] = {}
        stopped_previous: dict[str, UpstreamTransport] = {}
        try:
            # Create new/replaced upstreams before touching old ones.
            for name in to_add + to_restart:
                settings = next_upstreams[name]
                t_name = settings.get("type")
                cls = self._registry.upstreams.get(t_name)
                if cls is None:
                    raise ValueError(f"Unknown upstream type: {t_name}")
                transport = cls(name, settings)
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
            return False
        await upstream.restart()
        return True

    def health(self) -> dict[str, Any]:
        """Return health for all upstreams."""
        return {name: up.health() for name, up in self._upstreams.items()}
