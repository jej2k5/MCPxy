"""Tests for the `init` and `install` CLI subcommands."""

import json
from pathlib import Path

import pytest

from mcpxy_proxy.cli import main


def test_init_writes_starter_config(tmp_path: Path) -> None:
    out = tmp_path / "config.json"
    rc = main(["init", "--output", str(out)])
    assert rc == 0
    assert out.exists()
    data = json.loads(out.read_text())
    assert "upstreams" in data
    assert "admin" in data
    assert data["admin"]["enabled"] is True


def test_init_refuses_to_overwrite_without_force(tmp_path: Path, capsys) -> None:
    out = tmp_path / "config.json"
    out.write_text("{}")
    rc = main(["init", "--output", str(out)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "refusing to overwrite" in captured.err


def test_init_with_upstreams_creates_entries(tmp_path: Path) -> None:
    out = tmp_path / "c.json"
    rc = main(
        [
            "init",
            "--output",
            str(out),
            "--upstream",
            "git=stdio:python -m my.git",
            "--upstream",
            "search=https://example.com/mcp",
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["upstreams"]["git"]["type"] == "stdio"
    assert data["upstreams"]["git"]["command"] == "python"
    assert data["upstreams"]["git"]["args"] == ["-m", "my.git"]
    assert data["upstreams"]["search"]["type"] == "http"
    assert data["upstreams"]["search"]["url"] == "https://example.com/mcp"


def test_install_dry_run_for_claude_desktop(tmp_path: Path, capsys) -> None:
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"existing": {"command": "x", "args": []}}}))
    rc = main(
        [
            "install",
            "--client",
            "claude-desktop",
            "--config-path",
            str(config),
            "--url",
            "http://localhost:9000",
            "--dry-run",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "would write" in captured.out
    assert "mcpxy" in captured.out
    # Original file untouched
    assert "existing" in config.read_text()
    assert "mcpxy" not in config.read_text()


def test_install_writes_and_creates_backup(tmp_path: Path) -> None:
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"old": {"command": "x", "args": []}}}))
    rc = main(
        [
            "install",
            "--client",
            "claude-desktop",
            "--config-path",
            str(config),
            "--url",
            "http://localhost:9000",
        ]
    )
    assert rc == 0
    data = json.loads(config.read_text())
    assert "mcpxy" in data["mcpServers"]
    assert "old" in data["mcpServers"]
    backups = list(tmp_path.glob("claude_desktop_config.json.bak.*"))
    assert backups, "expected a backup file to be created"


def test_install_idempotent_does_not_duplicate(tmp_path: Path) -> None:
    config = tmp_path / "claude_desktop_config.json"
    config.write_text("{}")
    main(
        [
            "install",
            "--client",
            "claude-desktop",
            "--config-path",
            str(config),
            "--url",
            "http://localhost:9000",
        ]
    )
    main(
        [
            "install",
            "--client",
            "claude-desktop",
            "--config-path",
            str(config),
            "--url",
            "http://localhost:9000",
        ]
    )
    data = json.loads(config.read_text())
    assert list(data["mcpServers"].keys()).count("mcpxy") == 1


def test_install_chatgpt_prints_snippet(capsys) -> None:
    rc = main(
        [
            "install",
            "--client",
            "chatgpt",
            "--url",
            "http://h:1",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "transport" in captured.out
    assert "http://h:1" in captured.out
