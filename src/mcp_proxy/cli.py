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
from mcp_proxy.discovery.catalog import load_catalog
from mcp_proxy.discovery.importers import IMPORTERS, discover_all, get_importer
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
# Remote admin helpers (used by register/import/catalog subcommands)
# ---------------------------------------------------------------------------


def _remote_headers(token_env: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    env_name = token_env or "MCP_PROXY_TOKEN"
    token = __import__("os").getenv(env_name)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _remote_call(method: str, url: str, token_env: str | None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    import httpx

    with httpx.Client(timeout=30.0) as client:
        res = client.request(method, url, headers=_remote_headers(token_env), json=body)
    try:
        data = res.json() if res.content else {}
    except ValueError:
        data = {"raw": res.text}
    if res.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise SystemExit(
            f"admin request {method} {url} failed: {res.status_code} {detail or res.text}"
        )
    return data if isinstance(data, dict) else {"result": data}


def _parse_upstream_spec(body: str) -> dict[str, Any]:
    if body.startswith("stdio:"):
        parts = body[len("stdio:"):].split()
        if not parts:
            raise SystemExit("stdio: requires a command")
        return {"type": "stdio", "command": parts[0], "args": parts[1:]}
    if body.startswith("http:") or body.startswith("https:"):
        return {"type": "http", "url": body}
    raise SystemExit(f"unknown transport in {body!r} (use stdio: or http:)")


def _parse_variables(raw: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw or []:
        if "=" not in item:
            raise SystemExit(f"--var must be KEY=VALUE, got {item!r}")
        k, _, v = item.partition("=")
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# `register` / `unregister` — remote runtime registration
# ---------------------------------------------------------------------------


def cmd_register(args: argparse.Namespace) -> int:
    if args.stdio and args.http:
        print("pass only one of --stdio or --http", file=sys.stderr)
        return 2
    if args.stdio:
        definition = _parse_upstream_spec("stdio:" + args.stdio)
    elif args.http:
        definition = _parse_upstream_spec(args.http if args.http.startswith(("http:", "https:")) else "http:" + args.http)
    elif args.config_json:
        try:
            definition = json.loads(args.config_json)
        except json.JSONDecodeError as exc:
            print(f"--config-json is not valid JSON: {exc}", file=sys.stderr)
            return 2
    else:
        print("pass --stdio, --http, or --config-json", file=sys.stderr)
        return 2
    body = {"name": args.name, "config": definition, "replace": args.replace}
    result = _remote_call(
        "POST",
        args.url.rstrip("/") + "/admin/api/upstreams",
        args.token_env,
        body=body,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_unregister(args: argparse.Namespace) -> int:
    result = _remote_call(
        "DELETE",
        args.url.rstrip("/") + f"/admin/api/upstreams/{args.name}",
        args.token_env,
    )
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# `discover` / `import` — bring MCP servers in from other clients
# ---------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    # Local-only: read client config files directly without talking to a proxy.
    summary = discover_all()
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    for client in summary["clients"]:
        header = f"{client['display_name']} ({client['client_id']})"
        if not client["detected"]:
            print(f"{header}: not detected")
            continue
        print(f"{header}: {client['config_path']}")
        for upstream in client["upstreams"]:
            transport = upstream["config"].get("type", "?")
            print(f"  - {upstream['name']} [{transport}]")
        if not client["upstreams"]:
            print("  (no MCP servers configured)")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        importer = get_importer(args.client)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    discovered = importer.read()
    if not discovered:
        print(f"no MCP servers found in {args.client}", file=sys.stderr)
        return 1
    wanted = set(args.name or [])
    entries = [u for u in discovered if not wanted or u.name in wanted]
    if not entries:
        print(
            f"none of --name {sorted(wanted)} matched {args.client} servers "
            f"{[u.name for u in discovered]}",
            file=sys.stderr,
        )
        return 1
    if args.dry_run:
        print(json.dumps({"imported": [u.to_dict() for u in entries]}, indent=2))
        return 0
    result = _remote_call(
        "POST",
        args.url.rstrip("/") + "/admin/api/discovery/import",
        args.token_env,
        body={
            "client": args.client,
            "upstreams": [u.name for u in entries],
            "replace": args.replace,
        },
    )
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# `catalog` — browse + install bundled MCP servers
# ---------------------------------------------------------------------------


def cmd_catalog_list(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    entries = catalog.search(args.query or "", category=args.category)
    if args.json:
        print(json.dumps({"entries": [e.to_dict() for e in entries]}, indent=2))
        return 0
    if not entries:
        print("no matching catalog entries")
        return 0
    for entry in entries:
        print(f"{entry.id:<18} [{entry.category:<12}] {entry.name}")
        if entry.description:
            print(f"  {entry.description}")
    return 0


def cmd_catalog_install(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    entry = catalog.get(args.id)
    if entry is None:
        print(f"catalog entry {args.id!r} not found", file=sys.stderr)
        return 2
    variables = _parse_variables(args.var)
    try:
        name, definition = entry.materialize(name=args.name, variables=variables)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps({"name": name, "config": definition}, indent=2))
        return 0
    result = _remote_call(
        "POST",
        args.url.rstrip("/") + "/admin/api/catalog/install",
        args.token_env,
        body={
            "id": args.id,
            "name": args.name,
            "variables": variables,
            "replace": args.replace,
        },
    )
    print(json.dumps(result, indent=2))
    return 0


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

    # register / unregister --------------------------------------------------

    register = subparsers.add_parser(
        "register",
        help="Register a new upstream on a running MCPy proxy",
    )
    register.add_argument("--url", default="http://127.0.0.1:8000", help="MCPy proxy base URL")
    register.add_argument("--token-env", default=None, help="Env var with bearer token")
    register.add_argument("--name", required=True, help="Upstream name")
    register.add_argument("--stdio", default=None, help="stdio command line, e.g. 'python -m foo'")
    register.add_argument("--http", default=None, help="HTTP upstream URL")
    register.add_argument(
        "--config-json",
        default=None,
        help="Full upstream definition as a JSON string (alternative to --stdio/--http)",
    )
    register.add_argument("--replace", action="store_true", help="Overwrite if upstream exists")
    register.set_defaults(func=cmd_register)

    unregister = subparsers.add_parser(
        "unregister",
        help="Remove an upstream from a running MCPy proxy",
    )
    unregister.add_argument("--url", default="http://127.0.0.1:8000", help="MCPy proxy base URL")
    unregister.add_argument("--token-env", default=None, help="Env var with bearer token")
    unregister.add_argument("--name", required=True, help="Upstream name to remove")
    unregister.set_defaults(func=cmd_unregister)

    # discover / import ------------------------------------------------------

    discover = subparsers.add_parser(
        "discover",
        help="Scan local client configs (Claude Desktop/Code, Cursor, Windsurf, Continue) for MCP servers",
    )
    discover.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    discover.set_defaults(func=cmd_discover)

    importer = subparsers.add_parser(
        "import",
        help="Import MCP servers from another client into a running MCPy proxy",
    )
    importer.add_argument(
        "--client",
        required=True,
        choices=list(IMPORTERS.keys()),
        help="Source client to import from",
    )
    importer.add_argument(
        "--name",
        action="append",
        help="Only import upstreams with this name (repeatable). Default: import all.",
    )
    importer.add_argument("--url", default="http://127.0.0.1:8000", help="MCPy proxy base URL")
    importer.add_argument("--token-env", default=None, help="Env var with bearer token")
    importer.add_argument("--replace", action="store_true", help="Overwrite existing upstreams")
    importer.add_argument("--dry-run", action="store_true", help="Preview without calling the proxy")
    importer.set_defaults(func=cmd_import)

    # catalog ----------------------------------------------------------------

    catalog = subparsers.add_parser(
        "catalog",
        help="Browse and install MCP servers from the bundled catalog",
    )
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)

    catalog_list = catalog_sub.add_parser("list", help="List catalog entries")
    catalog_list.add_argument("--query", "-q", default="", help="Search query")
    catalog_list.add_argument("--category", default=None, help="Filter by category")
    catalog_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    catalog_list.set_defaults(func=cmd_catalog_list)

    catalog_install = catalog_sub.add_parser(
        "install", help="Install a catalog entry as an upstream on a running MCPy proxy"
    )
    catalog_install.add_argument("id", help="Catalog entry id (e.g. filesystem, github)")
    catalog_install.add_argument("--name", default=None, help="Upstream name (defaults to catalog id)")
    catalog_install.add_argument(
        "--var",
        action="append",
        metavar="KEY=VALUE",
        help="Set a catalog variable (repeatable)",
    )
    catalog_install.add_argument("--url", default="http://127.0.0.1:8000", help="MCPy proxy base URL")
    catalog_install.add_argument("--token-env", default=None, help="Env var with bearer token")
    catalog_install.add_argument("--replace", action="store_true", help="Overwrite if upstream exists")
    catalog_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the materialised upstream config without calling the proxy",
    )
    catalog_install.set_defaults(func=cmd_catalog_install)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
