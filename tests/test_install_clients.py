import json
from pathlib import Path

import pytest

from mcp_proxy.install.clients import (
    ChatGPTAdapter,
    ClaudeCodeAdapter,
    ClaudeDesktopAdapter,
    InstallOptions,
    get_adapter,
    list_clients,
)


def test_list_clients_known_set() -> None:
    clients = list_clients()
    assert "claude-desktop" in clients
    assert "claude-code" in clients
    assert "chatgpt" in clients


def test_get_adapter_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_adapter("nope")


# ---------- Claude Desktop ----------------------------------------------------


def test_claude_desktop_format_entry_uses_stdio_adapter_command() -> None:
    adapter = ClaudeDesktopAdapter()
    opts = InstallOptions(name="mcpy", url="http://127.0.0.1:8000", token_env="MCP_PROXY_TOKEN")
    entry = adapter.format_entry(opts)
    assert entry["command"] == "mcp-proxy"
    assert entry["args"][0] == "stdio"
    assert "--connect" in entry["args"]
    assert "http://127.0.0.1:8000" in entry["args"]
    assert "--token-env" in entry["args"]
    assert "MCP_PROXY_TOKEN" in entry["args"]
    # Token env is forwarded into the spawned subprocess.
    assert entry["env"] == {"MCP_PROXY_TOKEN": "${MCP_PROXY_TOKEN}"}


def test_claude_desktop_merge_creates_or_updates_entry_idempotently() -> None:
    adapter = ClaudeDesktopAdapter()
    opts = InstallOptions(name="mcpy", url="http://localhost:9000")

    # Empty file
    merged = adapter.merge(None, opts)
    assert "mcpServers" in merged
    assert "mcpy" in merged["mcpServers"]

    # Existing config with another server is preserved.
    existing = {"mcpServers": {"other": {"command": "x", "args": []}}}
    merged2 = adapter.merge(existing, opts)
    assert "other" in merged2["mcpServers"]
    assert "mcpy" in merged2["mcpServers"]

    # Reapply (idempotent — same shape).
    merged3 = adapter.merge(merged2, opts)
    assert merged3 == merged2


def test_claude_desktop_diff_shows_added_entry(tmp_path: Path) -> None:
    adapter = ClaudeDesktopAdapter()
    opts = InstallOptions(name="mcpy", url="http://localhost")
    diff = adapter.diff(None, adapter.merge(None, opts))
    assert "mcpServers" in diff
    assert "mcpy" in diff


# ---------- Claude Code -------------------------------------------------------


def test_claude_code_format_entry_uses_http_transport() -> None:
    adapter = ClaudeCodeAdapter()
    opts = InstallOptions(url="http://localhost:8000", token_env="MCP_PROXY_TOKEN")
    entry = adapter.format_entry(opts)
    assert entry["type"] == "http"
    assert entry["url"].startswith("http://localhost:8000")
    assert "Authorization" in entry["headers"]


def test_claude_code_format_entry_targets_specific_upstream() -> None:
    adapter = ClaudeCodeAdapter()
    opts = InstallOptions(url="http://h:1", upstream="git")
    entry = adapter.format_entry(opts)
    assert entry["url"].endswith("/mcp/git")


# ---------- ChatGPT -----------------------------------------------------------


def test_chatgpt_does_not_support_auto_install() -> None:
    adapter = ChatGPTAdapter()
    assert adapter.supports_auto_install() is False
    assert adapter.default_config_paths() == []


def test_chatgpt_format_entry_includes_authorization_when_token_set() -> None:
    adapter = ChatGPTAdapter()
    opts = InstallOptions(url="http://h:1", token_env="MCP_PROXY_TOKEN")
    entry = adapter.format_entry(opts)
    assert entry["transport"] == "http"
    assert "authorization" in entry
