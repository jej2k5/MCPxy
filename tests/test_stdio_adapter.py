"""Tests for the stdio adapter mode that forwards JSON-RPC to MCPxy HTTP."""

import asyncio
import json

import httpx
import pytest

from mcpxy_proxy.stdio_adapter import _build_endpoint, _bearer_headers, _forward


def test_build_endpoint_default_mount_path() -> None:
    assert _build_endpoint("http://h:1", "/mcp", None) == "http://h:1/mcp"


def test_build_endpoint_with_upstream() -> None:
    assert _build_endpoint("http://h:1/", "/mcp", "git") == "http://h:1/mcp/git"


def test_bearer_headers_returns_empty_when_token_unset(monkeypatch) -> None:
    monkeypatch.delenv("MY_TOKEN", raising=False)
    assert _bearer_headers("MY_TOKEN") == {}


def test_bearer_headers_reads_env_var(monkeypatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "secret")
    headers = _bearer_headers("MY_TOKEN")
    assert headers == {"Authorization": "Bearer secret"}


def test_bearer_headers_handles_none() -> None:
    assert _bearer_headers(None) == {}


@pytest.mark.asyncio
async def test_forward_sends_json_and_returns_first_ndjson_line() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/x-ndjson"},
            content=b'{"jsonrpc":"2.0","id":1,"result":"ok"}\n',
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _forward(
            client,
            "http://h:1/mcp",
            {"Authorization": "Bearer t"},
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        )

    assert result is not None
    assert result["result"] == "ok"
    assert captured["body"]["method"] == "tools/list"
    assert captured["headers"]["authorization"] == "Bearer t"


@pytest.mark.asyncio
async def test_forward_returns_none_for_notifications() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _forward(
            client,
            "http://h:1/mcp",
            {},
            json.dumps({"jsonrpc": "2.0", "method": "notification"}),
        )
    assert result is None


@pytest.mark.asyncio
async def test_forward_returns_jsonrpc_error_on_http_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _forward(
            client,
            "http://h:1/mcp",
            {},
            json.dumps({"jsonrpc": "2.0", "id": 7, "method": "x"}),
        )
    assert result is not None
    assert result["error"]["message"] == "http_500"
    assert result["id"] == 7


@pytest.mark.asyncio
async def test_forward_returns_parse_error_for_invalid_json() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _forward(client, "http://h:1/mcp", {}, "not json")
    assert result is not None
    assert result["error"]["code"] == -32700
