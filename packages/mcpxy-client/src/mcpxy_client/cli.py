"""Lightweight CLI for MCPxy client tools.

Provides ``install``, ``stdio``, and ``list-clients`` subcommands without
pulling in the full MCPxy server stack (FastAPI, SQLAlchemy, etc.).

Usage::

    mcpxy-client install --client claude-desktop --url https://proxy.example.com:8000
    mcpxy-client stdio --connect https://proxy.example.com:8000
    mcpxy-client list-clients
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from mcpxy_client.install.clients import InstallOptions, get_adapter, list_clients
from mcpxy_client.stdio_adapter import run_stdio_adapter


# ---------------------------------------------------------------------------
# Subcommand handlers
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
        proxy_command=args.proxy_command or "mcpxy-client",
    )

    if not adapter.supports_auto_install():
        snippet = adapter.format_entry(opts)
        print(
            "This client does not support automatic installation. "
            "Paste the following entry into the client's connector configuration:"
        )
        print(json.dumps(snippet, indent=2))
        return 0

    config_path = adapter.resolve_config_path(args.config_path)
    if config_path is None:
        print(
            f"could not determine config path for client {args.client!r}",
            file=sys.stderr,
        )
        return 2

    existing: dict[str, Any] | None = None
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            print(
                f"existing config at {config_path} is not valid JSON: {exc}",
                file=sys.stderr,
            )
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


def cmd_stdio(args: argparse.Namespace) -> int:
    return asyncio.run(
        run_stdio_adapter(
            url=args.connect,
            token_env=args.token_env,
            upstream=args.upstream,
            mount_path=args.mount_path,
        )
    )


def cmd_list_clients(args: argparse.Namespace) -> int:
    for name in list_clients():
        print(name)
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcpxy-client",
        description="MCPxy client tools — register MCPxy as an MCP server in AI clients",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- install -----------------------------------------------------------
    install = subparsers.add_parser(
        "install",
        help="Install MCPxy as an MCP server in a client app (Claude Desktop / Code / ChatGPT)",
    )
    install.add_argument(
        "--client",
        required=True,
        choices=list_clients(),
        help="Target client app",
    )
    install.add_argument("--name", default="mcpxy", help="Entry name shown in the client")
    install.add_argument("--url", default="http://127.0.0.1:8000", help="MCPxy proxy base URL")
    install.add_argument("--token-env", default=None, help="Env var holding the bearer token")
    install.add_argument("--upstream", default=None, help="Pin to a specific upstream")
    install.add_argument(
        "--proxy-command",
        default=None,
        help="Override the command path used by stdio adapters (default: mcpxy-client)",
    )
    install.add_argument("--config-path", default=None, help="Override client config file path")
    install.add_argument("--dry-run", action="store_true", help="Show diff without writing")
    install.set_defaults(func=cmd_install)

    # -- stdio -------------------------------------------------------------
    stdio = subparsers.add_parser(
        "stdio",
        help="Run as a stdio MCP server forwarding to a running MCPxy HTTP proxy",
    )
    stdio.add_argument("--connect", required=True, help="MCPxy proxy base URL")
    stdio.add_argument("--token-env", default=None, help="Env var with bearer token to forward")
    stdio.add_argument("--upstream", default=None, help="Target upstream name")
    stdio.add_argument("--mount-path", default="/mcp", help="Proxy mount path")
    stdio.set_defaults(func=cmd_stdio)

    # -- list-clients ------------------------------------------------------
    lc = subparsers.add_parser("list-clients", help="List supported AI clients")
    lc.set_defaults(func=cmd_list_clients)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
