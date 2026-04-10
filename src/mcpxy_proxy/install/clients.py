"""Client adapters for installing MCPxy into MCP-aware applications.

Each adapter knows:
- The well-known config file paths for that client (per OS).
- How to format an MCP server entry the client understands.
- How to merge that entry into an existing config file idempotently.

Currently shipped adapters:
- ClaudeDesktopAdapter — Anthropic's Claude Desktop app, which only
  supports stdio MCP servers. We register `mcpxy-proxy stdio --connect URL`
  as the stdio command so end users do not need a separate shim.
- ClaudeCodeAdapter — Claude Code CLI, which supports HTTP transport.
- ChatGPTAdapter — copy-paste only (no auto-write).
"""

from __future__ import annotations

import difflib
import json
import os
import platform
import shutil
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class InstallOptions:
    """User-supplied options for an install command."""

    name: str = "mcpxy"
    url: str = "http://127.0.0.1:8000"
    mount_path: str = "/mcp"
    token_env: str | None = None
    upstream: str | None = None  # If set, target /mcp/{upstream} instead of /mcp
    proxy_command: str | None = None  # Override for `mcpxy-proxy` binary path

    def endpoint(self) -> str:
        base = self.url.rstrip("/")
        if self.upstream:
            return f"{base}{self.mount_path.rstrip('/')}/{self.upstream}"
        return f"{base}{self.mount_path}"


class ClientAdapter(ABC):
    """Base class for client install adapters."""

    name: str

    @abstractmethod
    def default_config_paths(self) -> list[Path]:
        """Return likely config file locations for the current OS."""

    @abstractmethod
    def format_entry(self, opts: InstallOptions) -> dict[str, Any]:
        """Format the MCP server entry as a plain JSON-serializable dict."""

    @abstractmethod
    def merge(
        self,
        existing: dict[str, Any] | None,
        opts: InstallOptions,
    ) -> dict[str, Any]:
        """Return a new config dict with the MCPxy entry merged in."""

    def supports_auto_install(self) -> bool:
        """Return True if `install --client <name>` should write a config file."""
        return True

    def resolve_config_path(self, override: str | None = None) -> Path | None:
        if override:
            return Path(override).expanduser()
        for candidate in self.default_config_paths():
            if candidate.exists():
                return candidate
        # No file exists yet — fall back to the first known path so callers can
        # create it.
        paths = self.default_config_paths()
        return paths[0] if paths else None

    @staticmethod
    def diff(before: dict[str, Any] | None, after: dict[str, Any]) -> str:
        a = json.dumps(before or {}, indent=2, sort_keys=True).splitlines(keepends=True)
        b = json.dumps(after, indent=2, sort_keys=True).splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(a, b, fromfile="before", tofile="after", lineterm="")
        )

    @staticmethod
    def backup(path: Path) -> Path | None:
        if not path.exists():
            return None
        backup = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
        shutil.copy2(path, backup)
        return backup


# ---------------------------------------------------------------------------
# Claude Desktop (stdio only)
# ---------------------------------------------------------------------------


class ClaudeDesktopAdapter(ClientAdapter):
    name = "claude-desktop"

    def default_config_paths(self) -> list[Path]:
        home = Path.home()
        system = platform.system()
        if system == "Darwin":
            return [home / "Library/Application Support/Claude/claude_desktop_config.json"]
        if system == "Windows":
            appdata = os.environ.get("APPDATA", str(home / "AppData/Roaming"))
            return [Path(appdata) / "Claude" / "claude_desktop_config.json"]
        return [home / ".config/Claude/claude_desktop_config.json"]

    def format_entry(self, opts: InstallOptions) -> dict[str, Any]:
        cmd = opts.proxy_command or "mcpxy-proxy"
        args = ["stdio", "--connect", opts.url]
        if opts.upstream:
            args.extend(["--upstream", opts.upstream])
        if opts.token_env:
            args.extend(["--token-env", opts.token_env])
        entry: dict[str, Any] = {"command": cmd, "args": args}
        if opts.token_env:
            # Forward the token env var into the spawned subprocess so the
            # adapter can read it from os.environ.
            entry["env"] = {opts.token_env: f"${{{opts.token_env}}}"}
        return entry

    def merge(
        self,
        existing: dict[str, Any] | None,
        opts: InstallOptions,
    ) -> dict[str, Any]:
        cfg = dict(existing or {})
        servers = dict(cfg.get("mcpServers") or {})
        servers[opts.name] = self.format_entry(opts)
        cfg["mcpServers"] = servers
        return cfg


# ---------------------------------------------------------------------------
# Claude Code (HTTP transport)
# ---------------------------------------------------------------------------


class ClaudeCodeAdapter(ClientAdapter):
    name = "claude-code"

    def default_config_paths(self) -> list[Path]:
        home = Path.home()
        # Claude Code reads project-local .mcp.json or user-level config. We
        # target the user-level config under ~/.claude/mcp.json which mirrors
        # Anthropic's published guidance.
        return [
            home / ".claude" / "mcp.json",
            home / ".config" / "claude-code" / "config.json",
        ]

    def format_entry(self, opts: InstallOptions) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "type": "http",
            "url": opts.endpoint(),
        }
        if opts.token_env:
            # The recommended way to attach an Authorization header in Claude
            # Code is via the `headers` field, with `${env:VAR}` substitution.
            entry["headers"] = {"Authorization": f"Bearer ${{env:{opts.token_env}}}"}
        return entry

    def merge(
        self,
        existing: dict[str, Any] | None,
        opts: InstallOptions,
    ) -> dict[str, Any]:
        cfg = dict(existing or {})
        servers = dict(cfg.get("mcpServers") or {})
        servers[opts.name] = self.format_entry(opts)
        cfg["mcpServers"] = servers
        return cfg


# ---------------------------------------------------------------------------
# ChatGPT (no stable on-disk config — copy-paste only)
# ---------------------------------------------------------------------------


class ChatGPTAdapter(ClientAdapter):
    name = "chatgpt"

    def default_config_paths(self) -> list[Path]:
        return []

    def supports_auto_install(self) -> bool:
        return False

    def format_entry(self, opts: InstallOptions) -> dict[str, Any]:
        # ChatGPT's connector model expects an MCP HTTP endpoint URL plus an
        # optional bearer token. We surface a generic shape that the user can
        # paste into the connectors UI.
        entry: dict[str, Any] = {
            "name": opts.name,
            "transport": "http",
            "url": opts.endpoint(),
        }
        if opts.token_env:
            entry["authorization"] = f"Bearer ${{env:{opts.token_env}}}"
        return entry

    def merge(
        self,
        existing: dict[str, Any] | None,
        opts: InstallOptions,
    ) -> dict[str, Any]:
        return {opts.name: self.format_entry(opts)}


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


_ADAPTERS: dict[str, type[ClientAdapter]] = {
    ClaudeDesktopAdapter.name: ClaudeDesktopAdapter,
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    ChatGPTAdapter.name: ChatGPTAdapter,
}


def get_adapter(name: str) -> ClientAdapter:
    cls = _ADAPTERS.get(name)
    if cls is None:
        raise KeyError(f"Unknown client '{name}'. Known: {', '.join(sorted(_ADAPTERS))}")
    return cls()


def list_clients() -> list[str]:
    return sorted(_ADAPTERS.keys())
