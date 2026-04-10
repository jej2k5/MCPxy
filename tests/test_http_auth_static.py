"""Layer 2a: static HTTP auth strategies (bearer / api_key / basic / none).

OAuth2 is covered separately in ``test_oauth_flow.py`` because it needs a
mock auth server and a secrets store backing the token persistence.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from mcpxy_proxy.auth.strategies import (
    BasicAuthStrategy,
    BearerAuthStrategy,
    HeaderAuthStrategy,
    NoAuthStrategy,
    build_strategy,
)
from mcpxy_proxy.config import (
    ApiKeyAuthConfig,
    BasicAuthConfig,
    BearerAuthConfig,
    HttpUpstreamConfig,
    NoAuthConfig,
    OAuth2AuthConfig,
)
from mcpxy_proxy.proxy.http import HttpUpstreamTransport


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_http_upstream_defaults_to_no_auth() -> None:
    cfg = HttpUpstreamConfig(type="http", url="https://example.com/mcp")
    assert cfg.auth is None


def test_bearer_auth_config() -> None:
    cfg = HttpUpstreamConfig(
        type="http",
        url="https://x",
        auth={"type": "bearer", "token": "tkn_abc"},
    )
    assert isinstance(cfg.auth, BearerAuthConfig)
    assert cfg.auth.token == "tkn_abc"


def test_api_key_auth_config_with_defaults() -> None:
    cfg = HttpUpstreamConfig(
        type="http",
        url="https://x",
        auth={"type": "api_key", "value": "k_live"},
    )
    assert isinstance(cfg.auth, ApiKeyAuthConfig)
    assert cfg.auth.header == "X-Api-Key"
    assert cfg.auth.value == "k_live"


def test_api_key_auth_config_custom_header() -> None:
    cfg = HttpUpstreamConfig(
        type="http",
        url="https://x",
        auth={"type": "api_key", "header": "X-Linear-Token", "value": "lin_xxx"},
    )
    assert cfg.auth.header == "X-Linear-Token"  # type: ignore[union-attr]


def test_basic_auth_config() -> None:
    cfg = HttpUpstreamConfig(
        type="http",
        url="https://x",
        auth={"type": "basic", "username": "alice", "password": "secret"},
    )
    assert isinstance(cfg.auth, BasicAuthConfig)


def test_bearer_requires_non_empty_token() -> None:
    with pytest.raises(ValidationError):
        HttpUpstreamConfig(
            type="http", url="https://x", auth={"type": "bearer", "token": ""}
        )


def test_oauth2_config_requires_issuer_or_endpoints() -> None:
    with pytest.raises(ValidationError):
        HttpUpstreamConfig(
            type="http",
            url="https://x",
            auth={"type": "oauth2", "client_id": "c"},
        )


def test_oauth2_config_accepts_manual_endpoints() -> None:
    cfg = HttpUpstreamConfig(
        type="http",
        url="https://x",
        auth={
            "type": "oauth2",
            "authorization_endpoint": "https://auth.example/oauth/authorize",
            "token_endpoint": "https://auth.example/oauth/token",
            "client_id": "c",
        },
    )
    assert isinstance(cfg.auth, OAuth2AuthConfig)


def test_oauth2_config_accepts_issuer_only() -> None:
    cfg = HttpUpstreamConfig(
        type="http",
        url="https://x",
        auth={
            "type": "oauth2",
            "issuer": "https://auth.example",
            "client_id": "c",
        },
    )
    assert cfg.auth.issuer == "https://auth.example"  # type: ignore[union-attr]


def test_oauth2_config_dynamic_registration_skips_client_id_requirement() -> None:
    # With dynamic_registration=True we don't need a pre-issued client_id.
    cfg = HttpUpstreamConfig(
        type="http",
        url="https://x",
        auth={
            "type": "oauth2",
            "issuer": "https://auth.example",
            "dynamic_registration": True,
        },
    )
    assert cfg.auth.dynamic_registration is True  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# build_strategy: config -> runtime strategy
# ---------------------------------------------------------------------------


def test_build_strategy_none_config() -> None:
    assert isinstance(build_strategy(None), NoAuthStrategy)
    assert isinstance(build_strategy(NoAuthConfig()), NoAuthStrategy)


def test_build_strategy_bearer() -> None:
    strat = build_strategy(BearerAuthConfig(type="bearer", token="tkn"))
    assert isinstance(strat, BearerAuthStrategy)
    assert strat.static_headers() == {"Authorization": "Bearer tkn"}


def test_build_strategy_api_key_default_header() -> None:
    strat = build_strategy(ApiKeyAuthConfig(type="api_key", value="k"))
    assert isinstance(strat, HeaderAuthStrategy)
    assert strat.static_headers() == {"X-Api-Key": "k"}


def test_build_strategy_api_key_custom_header() -> None:
    strat = build_strategy(
        ApiKeyAuthConfig(type="api_key", header="X-Linear-Token", value="lin_xxx")
    )
    assert strat.static_headers() == {"X-Linear-Token": "lin_xxx"}


def test_build_strategy_basic() -> None:
    strat = build_strategy(
        BasicAuthConfig(type="basic", username="alice", password="s3cr3t")
    )
    headers = strat.static_headers()
    assert "Authorization" in headers
    prefix, _, encoded = headers["Authorization"].partition(" ")
    assert prefix == "Basic"
    assert base64.b64decode(encoded) == b"alice:s3cr3t"


def test_build_strategy_oauth2_raises_not_implemented() -> None:
    cfg = OAuth2AuthConfig(
        type="oauth2",
        authorization_endpoint="https://a/authorize",
        token_endpoint="https://a/token",
        client_id="c",
    )
    with pytest.raises(NotImplementedError):
        build_strategy(cfg)


# ---------------------------------------------------------------------------
# Transport integration: static auth flows through to httpx headers
# ---------------------------------------------------------------------------


async def _capture_headers_through_transport(
    settings: dict[str, Any],
) -> dict[str, str]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    transport = HttpUpstreamTransport("t", settings)
    await transport.start()
    assert transport._client is not None
    # Rebuild the client on top of MockTransport while preserving the
    # merged auth headers we just computed.
    live_headers = dict(transport._client.headers)
    await transport._client.aclose()
    transport._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=live_headers
    )
    try:
        await transport.request({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    finally:
        await transport.stop()
    return captured["headers"]


@pytest.mark.asyncio
async def test_bearer_flows_to_request() -> None:
    headers = await _capture_headers_through_transport(
        {
            "type": "http",
            "url": "https://x.invalid/mcp",
            "auth": {"type": "bearer", "token": "tkn_live"},
        }
    )
    assert headers["authorization"] == "Bearer tkn_live"


@pytest.mark.asyncio
async def test_api_key_flows_to_request() -> None:
    headers = await _capture_headers_through_transport(
        {
            "type": "http",
            "url": "https://x.invalid/mcp",
            "auth": {
                "type": "api_key",
                "header": "X-Linear-Token",
                "value": "lin_xxx",
            },
        }
    )
    assert headers["x-linear-token"] == "lin_xxx"


@pytest.mark.asyncio
async def test_basic_flows_to_request() -> None:
    headers = await _capture_headers_through_transport(
        {
            "type": "http",
            "url": "https://x.invalid/mcp",
            "auth": {"type": "basic", "username": "alice", "password": "p"},
        }
    )
    assert headers["authorization"].startswith("Basic ")
    encoded = headers["authorization"].split(" ", 1)[1]
    assert base64.b64decode(encoded) == b"alice:p"


@pytest.mark.asyncio
async def test_auth_headers_merge_with_explicit_headers() -> None:
    headers = await _capture_headers_through_transport(
        {
            "type": "http",
            "url": "https://x.invalid/mcp",
            "headers": {"X-Request-Source": "mcpxy-test"},
            "auth": {"type": "bearer", "token": "tkn"},
        }
    )
    assert headers["x-request-source"] == "mcpxy-test"
    assert headers["authorization"] == "Bearer tkn"


@pytest.mark.asyncio
async def test_no_auth_sends_no_auth_header() -> None:
    headers = await _capture_headers_through_transport(
        {"type": "http", "url": "https://x.invalid/mcp"}
    )
    assert "authorization" not in headers


@pytest.mark.asyncio
async def test_oauth2_transport_requires_oauth_manager() -> None:
    # Without _oauth_manager wired in, transport.start() must refuse.
    transport = HttpUpstreamTransport(
        "oauth_up",
        {
            "type": "http",
            "url": "https://x.invalid/mcp",
            "auth": {
                "type": "oauth2",
                "authorization_endpoint": "https://a/authorize",
                "token_endpoint": "https://a/token",
                "client_id": "c",
            },
        },
    )
    with pytest.raises(RuntimeError, match="oauth2 auth requires an OAuthManager"):
        await transport.start()
