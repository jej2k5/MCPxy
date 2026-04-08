"""CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import uvicorn

from mcp_proxy.config import load_config
from mcp_proxy.install.clients import InstallOptions, get_adapter, list_clients
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.stdio_adapter import run_stdio_adapter
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
        queue_max=config.telemetry.queue_max,
        drop_policy=config.telemetry.drop_policy,
        batch_size=config.telemetry.batch_size,
        flush_interval_ms=config.telemetry.flush_interval_ms,
    )
    return AppState(config, raw_config, manager, bridge, telemetry, registry=registry, config_path=config_path)


# ---------------------------------------------------------------------------
# `serve`
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
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
    return 0


# ---------------------------------------------------------------------------
# `init`
# ---------------------------------------------------------------------------


def _starter_config(upstream_specs: list[str]) -> dict[str, Any]:
    """Build a starter config dict from CLI --upstream specs of the form
    NAME=stdio:CMD ARG ARG  or  NAME=http:URL.
    """
    upstreams: dict[str, dict[str, Any]] = {}
    for spec in upstream_specs or []:
        if "=" not in spec:
            raise argparse.ArgumentTypeError(
                f"--upstream must be NAME=stdio:CMD or NAME=http:URL, got {spec!r}"
            )
        name, _, body = spec.partition("=")
        if body.startswith("stdio:"):
            parts = body[len("stdio:"):].split()
            if not parts:
                raise argparse.ArgumentTypeError(f"--upstream {name!r}: missing stdio command")
            upstreams[name] = {"type": "stdio", "command": parts[0], "args": parts[1:]}
        elif body.startswith("http:") or body.startswith("https:"):
            upstreams[name] = {"type": "http", "url": body}
        else:
            raise argparse.ArgumentTypeError(
                f"--upstream {name!r}: unknown transport (use stdio: or http:)"
            )

    return {
        "default_upstream": next(iter(upstreams), None),
        "auth": {"token_env": "MCP_PROXY_TOKEN"},
        "admin": {
            "mount_name": "__admin__",
            "enabled": True,
            "require_token": True,
            "allowed_clients": ["127.0.0.1"],
        },
        "telemetry": {"enabled": True, "sink": "noop"},
        "upstreams": upstreams,
    }


def cmd_init(args: argparse.Namespace) -> int:
    out_path = Path(args.output).expanduser()
    if out_path.exists() and not args.force:
        print(f"refusing to overwrite existing {out_path} (use --force)", file=sys.stderr)
        return 2
    cfg = _starter_config(args.upstream or [])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


# ---------------------------------------------------------------------------
# `install`
# ---------------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> int:
    try:
        adapter = get_adapter(args.client)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    opts = InstallOptions(
        name=args.name,
        url=args.url,
        token_env=args.token_env,
        upstream=args.upstream,
        proxy_command=args.proxy_command,
    )

    if not adapter.supports_auto_install():
        # Copy-paste only.
        snippet = adapter.format_entry(opts)
        print(
            "This client does not support automatic installation. "
            "Paste the following entry into the client's connector configuration:"
        )
        print(json.dumps(snippet, indent=2))
        return 0

    config_path = adapter.resolve_config_path(args.config_path)
    if config_path is None:
        print(f"could not determine config path for client {args.client!r}", file=sys.stderr)
        return 2

    existing: dict[str, Any] | None = None
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            print(f"existing config at {config_path} is not valid JSON: {exc}", file=sys.stderr)
            return 2

    merged = adapter.merge(existing, opts)
    diff = adapter.diff(existing, merged)

    if args.dry_run:
        print(f"# would write to {config_path}")
        print(diff or "(no changes)")
        return 0

    backup = adapter.backup(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if backup:
        print(f"wrote {config_path} (backup: {backup})")
    else:
        print(f"wrote {config_path}")
    return 0


# ---------------------------------------------------------------------------
# `stdio` (adapter mode for stdio-only clients)
# ---------------------------------------------------------------------------


def cmd_stdio(args: argparse.Namespace) -> int:
    return asyncio.run(
        run_stdio_adapter(
            url=args.connect,
            token_env=args.token_env,
            upstream=args.upstream,
            mount_path=args.mount_path,
        )
    )


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCPy multi-upstream MCP proxy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the MCP proxy server")
    serve.add_argument("--listen", type=parse_listen, default=parse_listen("127.0.0.1:8000"))
    serve.add_argument("--config", required=True)
    serve.add_argument("--log-level", default="info")
    serve.add_argument("--health-path", default="/health")
    serve.add_argument("--request-timeout", type=float, default=30.0)
    serve.add_argument("--idle-timeout", type=int, default=5)
    serve.add_argument("--max-queue", type=int, default=2048)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    init = subparsers.add_parser("init", help="Generate a starter MCPy config file")
    init.add_argument("--output", default="config.json")
    init.add_argument("--force", action="store_true", help="Overwrite an existing file")
    init.add_argument(
        "--upstream",
        action="append",
        metavar="NAME=stdio:CMD or NAME=http:URL",
        help="Define an upstream entry (repeatable)",
    )
    init.set_defaults(func=cmd_init)

    install = subparsers.add_parser(
        "install",
        help="Install MCPy as an MCP server in a client app (Claude Desktop / Code / ChatGPT)",
    )
    install.add_argument(
        "--client",
        required=True,
        choices=list_clients(),
        help="Target client app",
    )
    install.add_argument("--name", default="mcpy", help="Entry name shown in the client")
    install.add_argument("--url", default="http://127.0.0.1:8000", help="MCPy proxy base URL")
    install.add_argument("--token-env", default=None, help="Env var holding the bearer token")
    install.add_argument("--upstream", default=None, help="Pin to a specific upstream")
    install.add_argument(
        "--proxy-command",
        default=None,
        help="Override the `mcp-proxy` command path used by stdio adapters",
    )
    install.add_argument("--config-path", default=None, help="Override client config file path")
    install.add_argument("--dry-run", action="store_true", help="Show diff without writing")
    install.set_defaults(func=cmd_install)

    stdio = subparsers.add_parser(
        "stdio",
        help="Run as a stdio MCP server forwarding to a running MCPy HTTP proxy",
    )
    stdio.add_argument("--connect", required=True, help="MCPy proxy base URL")
    stdio.add_argument("--token-env", default=None, help="Env var with bearer token to forward")
    stdio.add_argument("--upstream", default=None, help="Target upstream name")
    stdio.add_argument("--mount-path", default="/mcp", help="Proxy mount path")
    stdio.set_defaults(func=cmd_stdio)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
