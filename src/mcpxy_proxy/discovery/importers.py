"""Import MCP server definitions from installed client config files.

Supports Claude Desktop, Claude Code, Cursor, Windsurf, and Continue.
Each importer reads the client's well-known config file, extracts MCP
server entries, and converts them into MCPxy-compatible upstream dicts.
This is a read-only preview; nothing is written back to the client
config and nothing is applied to MCPxy's live config until the user
explicitly imports via ``POST /admin/api/discovery/import`` or
``mcpxy-proxy import``.
"""

from __future__ import annotations

import json
import os
import platform
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DiscoveredUpstream:
    """An upstream definition discovered in a client config."""

    source_client: str
    name: str
    config: dict[str, Any]
    origin_path: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_client": self.source_client,
            "name": self.name,
            "config": self.config,
            "origin_path": self.origin_path,
            "warnings": list(self.warnings),
        }


class ClientImporter(ABC):
    """Read-only importer for a specific MCP client's config file."""

    client_id: str = ""
    display_name: str = ""

    @abstractmethod
    def candidate_paths(self) -> list[Path]:
        """Return well-known config file paths for this client, per OS."""

    def find_config(self) -> Path | None:
        for candidate in self.candidate_paths():
            if candidate.is_file():
                return candidate
        return None

    def read(self) -> list[DiscoveredUpstream]:
        path = self.find_config()
        if path is None:
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [
                DiscoveredUpstream(
                    source_client=self.client_id,
                    name="__error__",
                    config={},
                    origin_path=str(path),
                    warnings=[f"failed to read: {exc}"],
                )
            ]
        return list(self._extract(data, path))

    @abstractmethod
    def _extract(self, data: dict[str, Any], path: Path) -> list[DiscoveredUpstream]:
        """Parse the client-specific config shape into DiscoveredUpstream objects."""


def _home() -> Path:
    return Path.home()


def _mcp_servers_to_upstreams(
    client_id: str,
    servers: dict[str, Any],
    origin: Path,
) -> list[DiscoveredUpstream]:
    """Convert a ``mcpServers``-style dict (Claude Desktop / Cursor / Windsurf) to upstreams."""
    out: list[DiscoveredUpstream] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        warnings: list[str] = []
        # Both shapes are in the wild: stdio with "command"/"args", and http with "url".
        if "command" in entry:
            config: dict[str, Any] = {
                "type": "stdio",
                "command": str(entry["command"]),
                "args": [str(a) for a in entry.get("args", []) or []],
            }
            env = entry.get("env")
            if isinstance(env, dict) and env:
                config["env"] = {str(k): str(v) for k, v in env.items()}
        elif "url" in entry:
            config = {"type": "http", "url": str(entry["url"])}
        elif "type" in entry and entry.get("type") in {"stdio", "http"}:
            # Already a MCPxy-style config.
            config = dict(entry)
        else:
            warnings.append("unrecognised entry shape; skipping")
            continue
        out.append(
            DiscoveredUpstream(
                source_client=client_id,
                name=name,
                config=config,
                origin_path=str(origin),
                warnings=warnings,
            )
        )
    return out


class ClaudeDesktopImporter(ClientImporter):
    client_id = "claude-desktop"
    display_name = "Claude Desktop"

    def candidate_paths(self) -> list[Path]:
        home = _home()
        system = platform.system()
        if system == "Darwin":
            return [home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]
        if system == "Windows":
            appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
            return [appdata / "Claude" / "claude_desktop_config.json"]
        return [
            home / ".config" / "Claude" / "claude_desktop_config.json",
            home / ".config" / "claude" / "claude_desktop_config.json",
        ]

    def _extract(self, data: dict[str, Any], path: Path) -> list[DiscoveredUpstream]:
        servers = data.get("mcpServers") or {}
        if not isinstance(servers, dict):
            return []
        return _mcp_servers_to_upstreams(self.client_id, servers, path)


class ClaudeCodeImporter(ClientImporter):
    client_id = "claude-code"
    display_name = "Claude Code"

    def candidate_paths(self) -> list[Path]:
        home = _home()
        return [
            home / ".config" / "claude-code" / "config.json",
            home / ".claude-code" / "config.json",
            home / ".config" / "claude" / "claude_code_config.json",
        ]

    def _extract(self, data: dict[str, Any], path: Path) -> list[DiscoveredUpstream]:
        # Claude Code supports both mcpServers and a top-level "mcp.servers".
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            mcp = data.get("mcp")
            if isinstance(mcp, dict):
                servers = mcp.get("servers")
        if not isinstance(servers, dict):
            return []
        return _mcp_servers_to_upstreams(self.client_id, servers, path)


class CursorImporter(ClientImporter):
    client_id = "cursor"
    display_name = "Cursor"

    def candidate_paths(self) -> list[Path]:
        home = _home()
        return [
            home / ".cursor" / "mcp.json",
            home / ".config" / "Cursor" / "mcp.json",
        ]

    def _extract(self, data: dict[str, Any], path: Path) -> list[DiscoveredUpstream]:
        servers = data.get("mcpServers") or {}
        if not isinstance(servers, dict):
            return []
        return _mcp_servers_to_upstreams(self.client_id, servers, path)


class WindsurfImporter(ClientImporter):
    client_id = "windsurf"
    display_name = "Windsurf"

    def candidate_paths(self) -> list[Path]:
        home = _home()
        return [
            home / ".codeium" / "windsurf" / "mcp_config.json",
            home / ".config" / "windsurf" / "mcp_config.json",
        ]

    def _extract(self, data: dict[str, Any], path: Path) -> list[DiscoveredUpstream]:
        servers = data.get("mcpServers") or {}
        if not isinstance(servers, dict):
            return []
        return _mcp_servers_to_upstreams(self.client_id, servers, path)


class ContinueImporter(ClientImporter):
    client_id = "continue"
    display_name = "Continue"

    def candidate_paths(self) -> list[Path]:
        home = _home()
        return [
            home / ".continue" / "config.json",
            home / ".continue" / "mcp.json",
        ]

    def _extract(self, data: dict[str, Any], path: Path) -> list[DiscoveredUpstream]:
        # Continue uses an "experimental.modelContextProtocolServers" array.
        experimental = data.get("experimental") or {}
        servers_list: list[Any] = []
        if isinstance(experimental, dict):
            maybe = experimental.get("modelContextProtocolServers")
            if isinstance(maybe, list):
                servers_list = maybe
        if not servers_list:
            maybe = data.get("mcpServers")
            if isinstance(maybe, list):
                servers_list = maybe
            elif isinstance(maybe, dict):
                return _mcp_servers_to_upstreams(self.client_id, maybe, path)
        out: list[DiscoveredUpstream] = []
        for entry in servers_list:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("transport", {}).get("name") or "unnamed")
            transport = entry.get("transport") or entry
            if isinstance(transport, dict) and transport.get("type") == "stdio":
                config = {
                    "type": "stdio",
                    "command": str(transport.get("command", "")),
                    "args": [str(a) for a in transport.get("args", []) or []],
                }
            elif isinstance(transport, dict) and transport.get("type") == "http":
                config = {"type": "http", "url": str(transport.get("url", ""))}
            else:
                continue
            out.append(
                DiscoveredUpstream(
                    source_client=self.client_id,
                    name=name,
                    config=config,
                    origin_path=str(path),
                )
            )
        return out


IMPORTERS: dict[str, type[ClientImporter]] = {
    ClaudeDesktopImporter.client_id: ClaudeDesktopImporter,
    ClaudeCodeImporter.client_id: ClaudeCodeImporter,
    CursorImporter.client_id: CursorImporter,
    WindsurfImporter.client_id: WindsurfImporter,
    ContinueImporter.client_id: ContinueImporter,
}


def get_importer(client_id: str) -> ClientImporter:
    cls = IMPORTERS.get(client_id)
    if cls is None:
        raise KeyError(f"unknown client importer '{client_id}'")
    return cls()


def discover_all() -> dict[str, Any]:
    """Run every importer and return a summary suitable for JSON serialization."""
    out: dict[str, Any] = {"clients": []}
    for cls in IMPORTERS.values():
        importer = cls()
        path = importer.find_config()
        found = importer.read() if path is not None else []
        out["clients"].append(
            {
                "client_id": importer.client_id,
                "display_name": importer.display_name,
                "config_path": str(path) if path else None,
                "detected": path is not None,
                "upstreams": [u.to_dict() for u in found],
            }
        )
    return out
