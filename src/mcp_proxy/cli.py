"""CLI entry point."""

from __future__ import annotations

import argparse
import json

import uvicorn

from mcp_proxy.config import load_config
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.telemetry.http_sink import HttpTelemetrySink
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


def build_state(config_path: str) -> AppState:
    """Build app state from config file."""
    raw_config = json.loads(open(config_path, "r", encoding="utf-8").read())
    config = load_config(config_path)

    registry = PluginRegistry()
    registry.load_entry_points()

    manager = UpstreamManager(config.upstreams, registry)
    bridge = ProxyBridge(manager)

    sink_name = config.telemetry.sink
    sink_cls = registry.telemetry_sinks.get(sink_name)
    if sink_cls is None:
        sink = HttpTelemetrySink(config.telemetry.model_dump()) if sink_name == "http" else NoopTelemetrySink()
    else:
        sink = sink_cls(config.telemetry.model_dump()) if sink_name != "noop" else sink_cls()

    telemetry = TelemetryPipeline(
        sink=sink,
        queue_size=config.telemetry.queue_size,
        batch_size=config.telemetry.batch_size,
        flush_interval_ms=config.telemetry.flush_interval_ms,
    )
    return AppState(config, raw_config, manager, bridge, telemetry, registry=registry, config_path=config_path)


def main() -> None:
    """Run CLI server."""
    parser = argparse.ArgumentParser(description="MCP multi-upstream proxy")
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    state = build_state(args.config)
    app = create_app(state)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
