"""Layer 0 plumbing: per-upstream env vars (stdio) and headers (http).

These tests are deliberately narrow — they verify that config values flow
all the way through to subprocess env and httpx client headers, and that
the redaction step masks anything secret-shaped so the Config page can't
leak tokens back to the dashboard.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from mcpxy_proxy.config import (
    AppConfig,
    HttpUpstreamConfig,
    StdioUpstreamConfig,
    load_config,
    redact_secrets,
)
from mcpxy_proxy.proxy.http import HttpUpstreamTransport
from mcpxy_proxy.proxy.stdio import StdioUpstreamTransport


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_stdio_upstream_accepts_env_dict() -> None:
    cfg = StdioUpstreamConfig(
        type="stdio",
        command="/bin/true",
        args=[],
        env={"GITHUB_TOKEN": "gh_abc", "MODE": "debug"},
    )
    assert cfg.env == {"GITHUB_TOKEN": "gh_abc", "MODE": "debug"}


def test_http_upstream_accepts_headers_dict() -> None:
    cfg = HttpUpstreamConfig(
        type="http",
        url="https://example.com/mcp",
        headers={"Authorization": "Bearer xyz", "X-Workspace": "wk_1"},
    )
    assert cfg.headers == {"Authorization": "Bearer xyz", "X-Workspace": "wk_1"}


def test_config_env_expansion_fills_upstream_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Verify the pre-existing ${env:FOO} expansion reaches upstream env values.
    monkeypatch.setenv("UPSTREAM_ENV_GH_TOKEN", "live_token_value")
    raw = {
        "upstreams": {
            "gh": {
                "type": "stdio",
                "command": "/bin/true",
                "args": [],
                "env": {"GITHUB_TOKEN": "${env:UPSTREAM_ENV_GH_TOKEN}"},
            }
        }
    }
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_config(cfg_path)
    stdio_cfg = loaded.upstreams["gh"]
    assert isinstance(stdio_cfg, StdioUpstreamConfig)
    assert stdio_cfg.env["GITHUB_TOKEN"] == "live_token_value"


# ---------------------------------------------------------------------------
# Stdio transport: subprocess actually receives the env
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stdio_transport_passes_env_to_child(tmp_path: Path) -> None:
    # A tiny child that prints its env back as an MCP-shaped reply once we
    # send it a single JSON-RPC line, then exits.
    child_script = tmp_path / "echo_env.py"
    child_script.write_text(
        "import json, os, sys\n"
        "sys.stdin.readline()\n"
        'resp = {"jsonrpc": "2.0", "id": 1, "result": {\n'
        '    "upstream_env": os.environ.get("MCPXY_TEST_TOKEN", "<unset>"),\n'
        '    "proxy_env":    os.environ.get("PATH", "<unset>")[:3],\n'
        "}}\n"
        'sys.stdout.write(json.dumps(resp) + "\\n")\n'
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    transport = StdioUpstreamTransport(
        "echo",
        {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(child_script)],
            "env": {"MCPXY_TEST_TOKEN": "s3cret-value"},
        },
    )
    await transport.start()
    try:
        reply = await asyncio.wait_for(
            transport.request({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            timeout=5.0,
        )
    finally:
        await transport.stop()

    assert reply is not None, "child produced no reply"
    assert reply["result"]["upstream_env"] == "s3cret-value", (
        "config-supplied env must reach the child process"
    )
    # PATH should still be inherited from the proxy so npx/uvx still work.
    assert reply["result"]["proxy_env"] != "<unset>"


@pytest.mark.asyncio
async def test_stdio_transport_without_env_inherits_cleanly(tmp_path: Path) -> None:
    # When env overlay is empty, _build_env returns None so create_subprocess_exec
    # passes env=None (full inherit). Verify PATH is still visible to the child.
    child_script = tmp_path / "echo_path.py"
    child_script.write_text(
        "import json, os, sys\n"
        "sys.stdin.readline()\n"
        'print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"path_set": bool(os.environ.get("PATH"))}}))\n'
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    transport = StdioUpstreamTransport(
        "noenv",
        {"type": "stdio", "command": sys.executable, "args": [str(child_script)]},
    )
    await transport.start()
    try:
        reply = await asyncio.wait_for(
            transport.request({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            timeout=5.0,
        )
    finally:
        await transport.stop()
    assert reply is not None
    assert reply["result"]["path_set"] is True


# ---------------------------------------------------------------------------
# HTTP transport: httpx client actually sends the headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_transport_attaches_static_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    transport = HttpUpstreamTransport(
        "hdr",
        {
            "type": "http",
            "url": "https://example.invalid/mcp",
            "headers": {
                "Authorization": "Bearer hunter2",
                "X-Workspace": "wk_42",
            },
        },
    )
    await transport.start()
    # Substitute the httpx client with a MockTransport-backed one that keeps
    # the configured headers.
    assert transport._client is not None
    await transport._client.aclose()
    transport._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers=transport.static_headers,
    )
    try:
        reply = await transport.request({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    finally:
        await transport.stop()

    assert reply == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    assert captured["headers"].get("authorization") == "Bearer hunter2"
    assert captured["headers"].get("x-workspace") == "wk_42"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_secrets_masks_upstream_env_and_headers() -> None:
    payload = {
        "upstreams": {
            "gh": {
                "type": "stdio",
                "command": "node",
                "args": ["server.js"],
                "env": {
                    "GITHUB_TOKEN": "gh_abc123",
                    "LOG_LEVEL": "debug",
                    "API_KEY": "sk_live_xxx",
                },
            },
            "notion": {
                "type": "http",
                "url": "https://api.notion.example/mcp",
                "headers": {
                    "Authorization": "Bearer nsecret",
                    "X-Request-Id": "abc-123",
                    "X-Api-Key": "nk_live_xxx",
                },
            },
        }
    }
    out = redact_secrets(payload)
    assert out["upstreams"]["gh"]["env"]["GITHUB_TOKEN"] == "***REDACTED***"
    assert out["upstreams"]["gh"]["env"]["API_KEY"] == "***REDACTED***"
    # Non-secret-shaped keys pass through.
    assert out["upstreams"]["gh"]["env"]["LOG_LEVEL"] == "debug"
    assert out["upstreams"]["notion"]["headers"]["Authorization"] == "***REDACTED***"
    assert out["upstreams"]["notion"]["headers"]["X-Api-Key"] == "***REDACTED***"
    assert out["upstreams"]["notion"]["headers"]["X-Request-Id"] == "abc-123"
    # Original payload must not be mutated (redaction returns a deepcopy).
    assert payload["upstreams"]["gh"]["env"]["GITHUB_TOKEN"] == "gh_abc123"
