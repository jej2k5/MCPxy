"""Stdio MCP server that forwards JSON-RPC to a running MCPxy HTTP proxy.

Used by Claude Desktop and other stdio-only MCP clients to talk to MCPxy
without needing a separate process or HTTP transport in the client.

Reads NDJSON from stdin, posts each line to MCPxy's /mcp (or /mcp/{upstream})
endpoint, and writes responses as NDJSON to stdout. Notifications produce
no output. The bearer token is read from the env var named via --token-env.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

import httpx

_LOG = logging.getLogger(__name__)


async def _read_lines() -> "asyncio.Queue[str]":
    """Spawn a background reader for stdin and yield decoded lines."""
    queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _reader() -> None:
        try:
            for raw in sys.stdin:
                if not raw:
                    break
                line = raw.rstrip("\n")
                if not line:
                    continue
                loop.call_soon_threadsafe(queue.put_nowait, line)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, "")

    import threading

    threading.Thread(target=_reader, name="mcpxy-stdio-reader", daemon=True).start()
    return queue


def _build_endpoint(url: str, mount_path: str, upstream: str | None) -> str:
    base = url.rstrip("/")
    if upstream:
        return f"{base}{mount_path.rstrip('/')}/{upstream}"
    return f"{base}{mount_path}"


def _bearer_headers(token_env: str | None) -> dict[str, str]:
    if not token_env:
        return {}
    token = os.environ.get(token_env)
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


async def _forward(
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict[str, str],
    line: str,
) -> dict[str, Any] | None:
    """Forward a single JSON-RPC line and return the parsed response.

    The MCPxy proxy answers JSON-RPC requests as NDJSON (one response per
    line), so we accept either a single object or the first line of an
    NDJSON body. Notifications return None.
    """
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        _LOG.error("invalid_json line=%r error=%s", line, exc)
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": f"parse_error:{exc}"},
        }
    is_notification = isinstance(message, dict) and "id" not in message

    try:
        resp = await client.post(
            endpoint,
            content=json.dumps(message),
            headers={"Content-Type": "application/json", **headers},
        )
    except httpx.HTTPError as exc:
        _LOG.error("forward_failed error=%s", exc)
        if is_notification:
            return None
        return {
            "jsonrpc": "2.0",
            "id": (message or {}).get("id"),
            "error": {"code": -32000, "message": f"transport_error:{exc}"},
        }

    if resp.status_code == 202 or is_notification:
        return None

    if resp.status_code >= 400:
        return {
            "jsonrpc": "2.0",
            "id": (message or {}).get("id"),
            "error": {
                "code": -32000,
                "message": f"http_{resp.status_code}",
                "data": resp.text[:512],
            },
        }

    body = resp.text.strip()
    if not body:
        return None
    # /mcp returns NDJSON; pick the first line corresponding to this request.
    first = body.split("\n", 1)[0]
    try:
        return json.loads(first)
    except json.JSONDecodeError:
        return {
            "jsonrpc": "2.0",
            "id": (message or {}).get("id"),
            "error": {
                "code": -32603,
                "message": "invalid_response_from_proxy",
                "data": first[:512],
            },
        }


async def run_stdio_adapter(
    *,
    url: str,
    token_env: str | None = None,
    upstream: str | None = None,
    mount_path: str = "/mcp",
) -> int:
    """Main entry point for the stdio adapter loop. Returns an exit code."""
    endpoint = _build_endpoint(url, mount_path, upstream)
    headers = _bearer_headers(token_env)
    queue = await _read_lines()
    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            line = await queue.get()
            if line == "":  # EOF sentinel from the reader thread
                return 0
            response = await _forward(client, endpoint, headers, line)
            if response is None:
                continue
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
