"""Tests for client-config importers."""

import json
from pathlib import Path
from unittest.mock import patch

from mcpxy_proxy.discovery.importers import (
    ClaudeCodeImporter,
    ClaudeDesktopImporter,
    ContinueImporter,
    CursorImporter,
    IMPORTERS,
    WindsurfImporter,
    discover_all,
    get_importer,
)


def _write(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_get_importer_and_registry() -> None:
    assert set(IMPORTERS.keys()) >= {
        "claude-desktop",
        "claude-code",
        "cursor",
        "windsurf",
        "continue",
    }
    assert isinstance(get_importer("claude-desktop"), ClaudeDesktopImporter)


def test_claude_desktop_importer_extracts_stdio_and_http(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "claude_desktop_config.json",
        {
            "mcpServers": {
                "fs": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                    "env": {"A": "1"},
                },
                "remote": {"url": "https://example.com/mcp"},
                "broken": {"nothing": "here"},
            }
        },
    )
    importer = ClaudeDesktopImporter()
    with patch.object(importer, "candidate_paths", return_value=[cfg]):
        found = importer.read()
    names = {u.name: u for u in found}
    assert set(names.keys()) == {"fs", "remote"}
    fs = names["fs"].config
    assert fs["type"] == "stdio"
    assert fs["command"] == "npx"
    assert fs["args"][-1] == "/tmp"
    assert fs["env"] == {"A": "1"}
    remote = names["remote"].config
    assert remote == {"type": "http", "url": "https://example.com/mcp"}


def test_claude_code_importer_handles_nested_mcp_servers(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.json",
        {"mcp": {"servers": {"git": {"command": "uvx", "args": ["mcp-server-git"]}}}},
    )
    importer = ClaudeCodeImporter()
    with patch.object(importer, "candidate_paths", return_value=[cfg]):
        found = importer.read()
    assert len(found) == 1
    assert found[0].name == "git"
    assert found[0].config["command"] == "uvx"


def test_cursor_importer(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "mcp.json",
        {"mcpServers": {"memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]}}},
    )
    importer = CursorImporter()
    with patch.object(importer, "candidate_paths", return_value=[cfg]):
        found = importer.read()
    assert len(found) == 1
    assert found[0].source_client == "cursor"
    assert found[0].name == "memory"


def test_windsurf_importer(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "mcp_config.json",
        {"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}},
    )
    importer = WindsurfImporter()
    with patch.object(importer, "candidate_paths", return_value=[cfg]):
        found = importer.read()
    assert len(found) == 1
    assert found[0].name == "fetch"


def test_continue_importer_experimental_shape(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.json",
        {
            "experimental": {
                "modelContextProtocolServers": [
                    {
                        "name": "sqlite",
                        "transport": {
                            "type": "stdio",
                            "command": "uvx",
                            "args": ["mcp-server-sqlite", "--db-path", "/tmp/x.db"],
                        },
                    }
                ]
            }
        },
    )
    importer = ContinueImporter()
    with patch.object(importer, "candidate_paths", return_value=[cfg]):
        found = importer.read()
    assert len(found) == 1
    assert found[0].name == "sqlite"
    assert found[0].config["command"] == "uvx"


def test_importer_returns_nothing_when_config_missing(tmp_path: Path) -> None:
    importer = ClaudeDesktopImporter()
    with patch.object(importer, "candidate_paths", return_value=[tmp_path / "nope.json"]):
        assert importer.read() == []


def test_importer_invalid_json_surfaces_as_warning(tmp_path: Path) -> None:
    path = tmp_path / "claude_desktop_config.json"
    path.write_text("{not json", encoding="utf-8")
    importer = ClaudeDesktopImporter()
    with patch.object(importer, "candidate_paths", return_value=[path]):
        found = importer.read()
    assert len(found) == 1
    assert found[0].warnings
    assert "failed to read" in found[0].warnings[0]


def test_discover_all_covers_every_known_client(tmp_path: Path) -> None:
    # Point every importer at a missing path so the call succeeds without
    # depending on the host filesystem.
    missing = tmp_path / "nothing.json"
    with patch(
        "mcpxy_proxy.discovery.importers.ClaudeDesktopImporter.candidate_paths",
        return_value=[missing],
    ), patch(
        "mcpxy_proxy.discovery.importers.ClaudeCodeImporter.candidate_paths",
        return_value=[missing],
    ), patch(
        "mcpxy_proxy.discovery.importers.CursorImporter.candidate_paths",
        return_value=[missing],
    ), patch(
        "mcpxy_proxy.discovery.importers.WindsurfImporter.candidate_paths",
        return_value=[missing],
    ), patch(
        "mcpxy_proxy.discovery.importers.ContinueImporter.candidate_paths",
        return_value=[missing],
    ):
        result = discover_all()
    assert len(result["clients"]) == 5
    assert all(client["detected"] is False for client in result["clients"])
