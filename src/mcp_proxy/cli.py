"""CLI entry point."""

from __future__ import annotations

import argparse
import json
import logging

import uvicorn

from mcp_proxy.config import load_config
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


def parse_listen(value: str) -> tuple[str, int]:
    """Parse host:port listen value."""
    host, sep, raw_port = value.rpartition(":")
    if not sep or not host or not raw_port:
        raise argparse.ArgumentTypeError("--listen must be in host:port format")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("listen port must be an integer") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("listen port must be between 1 and 65535")
    return host, port


def build_state(config_path: str) -> AppState:
    """Build app state from config file."""
    raw_config = json.loads(open(config_path, "r", encoding="utf-8").read())
    config = load_config(config_path)

    registry = PluginRegistry()
    registry.load_entry_points()

    manager = UpstreamManager(config.upstreams, registry)
    bridge = ProxyBridge(manager)

    sink_name = config.telemetry.sink
    sink_cls = registry.validate_telemetry_sink_type(sink_name)
    sink = sink_cls() if sink_name == "noop" else sink_cls(config.telemetry.model_dump())

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
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run MCP proxy server")
    serve.add_argument("--listen", type=parse_listen, default=parse_listen("127.0.0.1:8000"))
    serve.add_argument("--config", required=True)
    serve.add_argument("--log-level", default="info")
    serve.add_argument("--health-path", default="/health")
    serve.add_argument("--request-timeout", type=float, default=30.0)
    serve.add_argument("--idle-timeout", type=int, default=5)
    serve.add_argument("--max-queue", type=int, default=2048)
    serve.add_argument("--reload", action="store_true")

    args = parser.parse_args()

    if args.command != "serve":
        parser.error(f"unsupported command: {args.command}")

    logging.getLogger().setLevel(args.log_level.upper())

    state = build_state(args.config)
    app = create_app(state, health_path=args.health_path, request_timeout_s=args.request_timeout)
    host, port = args.listen
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=args.log_level,
        timeout_keep_alive=args.idle_timeout,
        backlog=args.max_queue,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
