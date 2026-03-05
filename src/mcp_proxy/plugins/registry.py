"""Plugin registry for upstream transports and telemetry sinks."""

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
        self.upstreams: dict[str, type[UpstreamTransport]] = {}
        self.telemetry_sinks: dict[str, Any] = {}
        self._upstream_sources: dict[str, str] = {}
        self._telemetry_sink_sources: dict[str, str] = {}

        self.register_upstream("stdio", StdioUpstreamTransport, source="built-in")
        self.register_upstream("http", HttpUpstreamTransport, source="built-in")
        self.register_telemetry_sink("http", HttpTelemetrySink, source="built-in")
        self.register_telemetry_sink("noop", NoopTelemetrySink, source="built-in")

    def register_upstream(
        self,
        name: str,
        upstream: type[UpstreamTransport],
        *,
        allow_override: bool = False,
        source: str = "runtime",
    ) -> None:
        """Register an upstream transport plugin."""
        self._register(
            registry=self.upstreams,
            sources=self._upstream_sources,
            plugin_kind="upstream",
            name=name,
            plugin=upstream,
            allow_override=allow_override,
            source=source,
        )

    def register_telemetry_sink(
        self,
        name: str,
        sink: Any,
        *,
        allow_override: bool = False,
        source: str = "runtime",
    ) -> None:
        """Register a telemetry sink plugin."""
        self._register(
            registry=self.telemetry_sinks,
            sources=self._telemetry_sink_sources,
            plugin_kind="telemetry sink",
            name=name,
            plugin=sink,
            allow_override=allow_override,
            source=source,
        )

    def load_entry_points(self, *, allow_overrides: bool = False) -> None:
        """Load plugin entry points from installed distributions."""
        for ep in entry_points(group="mcp_proxy.upstreams"):
            self.register_upstream(ep.name, ep.load(), allow_override=allow_overrides, source=f"entry point '{ep.value}'")
        for ep in entry_points(group="mcp_proxy.telemetry_sinks"):
            self.register_telemetry_sink(
                ep.name,
                ep.load(),
                allow_override=allow_overrides,
                source=f"entry point '{ep.value}'",
            )

    def validate_upstream_type(self, upstream_type: str | None) -> type[UpstreamTransport]:
        """Return upstream class for a configured type or raise a detailed error."""
        if not upstream_type:
            raise ValueError(
                f"Missing upstream type in config. Available upstream types: {self._available(self.upstreams)}"
            )
        cls = self.upstreams.get(upstream_type)
        if cls is None:
            raise ValueError(
                f"Unknown upstream type '{upstream_type}'. "
                f"Available upstream types: {self._available(self.upstreams)}"
            )
        return cls

    def validate_telemetry_sink_type(self, sink_type: str | None) -> Any:
        """Return sink class for a configured type or raise a detailed error."""
        if not sink_type:
            raise ValueError(
                "Missing telemetry sink type in config. "
                f"Available telemetry sinks: {self._available(self.telemetry_sinks)}"
            )
        sink_cls = self.telemetry_sinks.get(sink_type)
        if sink_cls is None:
            raise ValueError(
                f"Unknown telemetry sink type '{sink_type}'. "
                f"Available telemetry sinks: {self._available(self.telemetry_sinks)}"
            )
        return sink_cls

    @staticmethod
    def _available(registry: dict[str, Any]) -> str:
        return ", ".join(sorted(registry)) if registry else "none"

    @staticmethod
    def _register(
        registry: dict[str, Any],
        sources: dict[str, str],
        plugin_kind: str,
        name: str,
        plugin: Any,
        allow_override: bool,
        source: str,
    ) -> None:
        if name in registry and not allow_override:
            existing_source = sources.get(name, "unknown")
            raise ValueError(
                f"Duplicate {plugin_kind} plugin key '{name}' from {source}; already registered from "
                f"{existing_source}. Pass allow_override=True to replace it."
            )
        registry[name] = plugin
        sources[name] = source
