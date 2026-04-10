"""End-to-end coverage for the "Add manually" dialog payload shapes.

The frontend dialog (``frontend/src/components/AddManuallyDialog.tsx``)
posts to ``POST /admin/api/upstreams`` with a payload of the form
``{name, config, replace}`` where ``config`` is whatever the operator
filled in. The backend already has tests for the registration service
in isolation; this file pins the *exact* shapes the dialog produces so
a future refactor of the form schema can't silently desync from the
admin API.

Each test posts the JSON the dialog would build, asserts the
registration succeeds, and reads back the persisted config to confirm
every field landed where the runtime expects it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from mcpxy_proxy.config import (
    AppConfig,
    BasicAuthConfig,
    BearerAuthConfig,
    HttpUpstreamConfig,
    OAuth2AuthConfig,
    StdioUpstreamConfig,
)
from mcpxy_proxy.plugins.registry import PluginRegistry
from mcpxy_proxy.proxy.bridge import ProxyBridge
from mcpxy_proxy.proxy.manager import UpstreamManager
from mcpxy_proxy.secrets import SecretsManager
from mcpxy_proxy.server import AppState, create_app
from mcpxy_proxy.storage.config_store import open_store
from mcpxy_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline


HEADERS = {"Authorization": "Bearer test-admin-token"}


def _build_client(tmp_path: Path) -> tuple[TestClient, AppState]:
    """Spin up an authenticated TestClient with no upstreams seeded.

    Mirrors the production wiring (real ConfigStore, real SecretsManager,
    real OAuth manager) so the test exercises the same code path the
    dashboard hits.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    fernet = Fernet(Fernet.generate_key())
    store = open_store(f"sqlite:///{state_dir / 'mcpxy.db'}", fernet=fernet)

    raw: dict[str, Any] = {
        "auth": {"token": "test-admin-token", "token_env": None},
        "admin": {
            "mount_name": "__admin__",
            "enabled": True,
            "require_token": True,
            "allowed_clients": [],
        },
        "telemetry": {"enabled": True, "sink": "noop"},
        "upstreams": {},
    }
    store.save_active_config(raw, source="test.bootstrap")

    cfg = AppConfig.model_validate(raw)
    registry = PluginRegistry()
    registry.load_entry_points()
    secrets_manager = SecretsManager(state_dir=state_dir, config_store=store)
    manager = UpstreamManager(cfg.upstreams, registry)
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(sink=NoopTelemetrySink())
    state = AppState(
        cfg,
        raw,
        manager,
        bridge,
        telemetry,
        registry=registry,
        secrets_manager=secrets_manager,
        config_store=store,
    )
    app = create_app(state)
    return TestClient(app), state


def _post(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    res = client.post("/admin/api/upstreams", headers=HEADERS, json=body)
    assert res.status_code == 200, res.text
    payload = res.json()
    assert payload.get("applied") is True, payload
    return payload


# ---------------------------------------------------------------------------
# stdio upstreams: command + args + env + queue_size
# ---------------------------------------------------------------------------


def test_manual_add_stdio_minimal(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        _post(
            client,
            {
                "name": "fs",
                "config": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
                },
                "replace": False,
            },
        )
        live = state.runtime_config.config.upstreams["fs"]
        assert isinstance(live, StdioUpstreamConfig)
        assert live.command == "npx"
        assert live.args == ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
        assert live.env == {}


def test_manual_add_stdio_with_env_and_queue_size(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        # Operators routinely set per-upstream env vars to inject
        # API keys without leaking them into sibling subprocesses,
        # and bump queue_size for chatty servers.
        _post(
            client,
            {
                "name": "github",
                "config": {
                    "type": "stdio",
                    "command": "uvx",
                    "args": ["mcp-server-github"],
                    "env": {"GITHUB_TOKEN": "ghp_literal_for_test", "LOG_LEVEL": "debug"},
                    "queue_size": 500,
                },
                "replace": False,
            },
        )
        live = state.runtime_config.config.upstreams["github"]
        assert isinstance(live, StdioUpstreamConfig)
        assert live.env == {"GITHUB_TOKEN": "ghp_literal_for_test", "LOG_LEVEL": "debug"}
        assert live.queue_size == 500


# ---------------------------------------------------------------------------
# http upstreams: url + headers + auth taxonomy
# ---------------------------------------------------------------------------


def test_manual_add_http_minimal(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        _post(
            client,
            {
                "name": "remote",
                "config": {"type": "http", "url": "https://api.example.com/mcp"},
                "replace": False,
            },
        )
        live = state.runtime_config.config.upstreams["remote"]
        assert isinstance(live, HttpUpstreamConfig)
        assert live.url == "https://api.example.com/mcp"
        assert live.auth is None
        assert live.headers == {}


def test_manual_add_http_with_static_headers(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        _post(
            client,
            {
                "name": "remote",
                "config": {
                    "type": "http",
                    "url": "https://api.example.com/mcp",
                    "headers": {"X-Workspace-Id": "wk_42", "User-Agent": "mcpxy-test/1"},
                    "timeout_s": 60,
                },
                "replace": False,
            },
        )
        live = state.runtime_config.config.upstreams["remote"]
        assert isinstance(live, HttpUpstreamConfig)
        assert live.headers["X-Workspace-Id"] == "wk_42"
        assert live.headers["User-Agent"] == "mcpxy-test/1"
        assert live.timeout_s == 60


def test_manual_add_http_bearer_auth(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        _post(
            client,
            {
                "name": "linear",
                "config": {
                    "type": "http",
                    "url": "https://api.linear.app/mcp",
                    "auth": {"type": "bearer", "token": "lin_live_xxx"},
                },
                "replace": False,
            },
        )
        live = state.runtime_config.config.upstreams["linear"]
        assert isinstance(live, HttpUpstreamConfig)
        assert isinstance(live.auth, BearerAuthConfig)
        assert live.auth.token == "lin_live_xxx"


def test_manual_add_http_api_key_auth(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        _post(
            client,
            {
                "name": "notion",
                "config": {
                    "type": "http",
                    "url": "https://api.notion.example/mcp",
                    "auth": {
                        "type": "api_key",
                        "header": "X-Api-Key",
                        "value": "ntn_live_xxx",
                    },
                },
                "replace": False,
            },
        )
        live = state.runtime_config.config.upstreams["notion"]
        assert live.auth.type == "api_key"  # type: ignore[union-attr]
        assert live.auth.header == "X-Api-Key"  # type: ignore[union-attr]
        assert live.auth.value == "ntn_live_xxx"  # type: ignore[union-attr]


def test_manual_add_http_basic_auth(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        _post(
            client,
            {
                "name": "internal",
                "config": {
                    "type": "http",
                    "url": "https://internal.example/mcp",
                    "auth": {
                        "type": "basic",
                        "username": "alice",
                        "password": "s3cr3t",
                    },
                },
                "replace": False,
            },
        )
        live = state.runtime_config.config.upstreams["internal"]
        assert isinstance(live.auth, BasicAuthConfig)
        assert live.auth.username == "alice"
        assert live.auth.password == "s3cr3t"


def test_manual_add_http_oauth2_with_issuer(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        _post(
            client,
            {
                "name": "stripe",
                "config": {
                    "type": "http",
                    "url": "https://stripe.example/mcp",
                    "auth": {
                        "type": "oauth2",
                        "issuer": "https://auth.stripe.example",
                        "client_id": "ca_static_client",
                        "client_secret": "sk_test_secret",
                        "scopes": ["read", "write"],
                        "dynamic_registration": False,
                    },
                },
                "replace": False,
            },
        )
        live = state.runtime_config.config.upstreams["stripe"]
        assert isinstance(live.auth, OAuth2AuthConfig)
        assert live.auth.issuer == "https://auth.stripe.example"
        assert live.auth.client_id == "ca_static_client"
        assert live.auth.scopes == ["read", "write"]


def test_manual_add_http_oauth2_dynamic_registration(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        # Dynamic registration mode lets the operator skip the
        # client_id field — the proxy will register itself at the
        # auth server's registration_endpoint.
        _post(
            client,
            {
                "name": "dyn",
                "config": {
                    "type": "http",
                    "url": "https://dyn.example/mcp",
                    "auth": {
                        "type": "oauth2",
                        "issuer": "https://auth.dyn.example",
                        "dynamic_registration": True,
                    },
                },
                "replace": False,
            },
        )
        live = state.runtime_config.config.upstreams["dyn"]
        assert isinstance(live.auth, OAuth2AuthConfig)
        assert live.auth.dynamic_registration is True
        assert live.auth.client_id is None


def test_manual_add_http_oauth2_explicit_endpoints(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        # Operators against an auth server that doesn't publish
        # RFC 8414 metadata supply both endpoints by hand.
        _post(
            client,
            {
                "name": "manual_oauth",
                "config": {
                    "type": "http",
                    "url": "https://x.example/mcp",
                    "auth": {
                        "type": "oauth2",
                        "authorization_endpoint": "https://x.example/oauth/authorize",
                        "token_endpoint": "https://x.example/oauth/token",
                        "client_id": "x-cid",
                    },
                },
                "replace": False,
            },
        )
        live = state.runtime_config.config.upstreams["manual_oauth"]
        assert live.auth.authorization_endpoint == "https://x.example/oauth/authorize"  # type: ignore[union-attr]
        assert live.auth.token_endpoint == "https://x.example/oauth/token"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Validation + integration with the rest of the admin API surface
# ---------------------------------------------------------------------------


def test_manual_add_rejects_bad_name(tmp_path: Path) -> None:
    client, _ = _build_client(tmp_path)
    with client:
        res = client.post(
            "/admin/api/upstreams",
            headers=HEADERS,
            json={
                "name": "has spaces",
                "config": {"type": "http", "url": "http://x"},
                "replace": False,
            },
        )
        # The dialog also enforces this in the frontend, but the
        # registration service rejects it as the source of truth.
        assert res.status_code == 400


def test_manual_add_replace_overwrites_existing(tmp_path: Path) -> None:
    client, state = _build_client(tmp_path)
    with client:
        _post(
            client,
            {
                "name": "remote",
                "config": {"type": "http", "url": "https://v1.example/mcp"},
                "replace": False,
            },
        )
        # Without replace=True the second call must fail.
        res = client.post(
            "/admin/api/upstreams",
            headers=HEADERS,
            json={
                "name": "remote",
                "config": {"type": "http", "url": "https://v2.example/mcp"},
                "replace": False,
            },
        )
        assert res.status_code == 400
        # With replace=True it succeeds and the live config swaps.
        _post(
            client,
            {
                "name": "remote",
                "config": {"type": "http", "url": "https://v2.example/mcp"},
                "replace": True,
            },
        )
        assert state.runtime_config.config.upstreams["remote"].url == "https://v2.example/mcp"  # type: ignore[union-attr]


def test_manual_add_secret_reference_resolves_at_apply_time(tmp_path: Path) -> None:
    """Pinning the most operator-relevant feature: a ${secret:NAME}
    placeholder in a manually-added bearer token is resolved against
    the live SecretsManager when the runtime applies the new config.
    """
    client, state = _build_client(tmp_path)
    with client:
        # Pre-populate a secret the dialog might reference.
        r = client.post(
            "/admin/api/secrets",
            headers=HEADERS,
            json={"name": "linear_token", "value": "lin_live_real"},
        )
        assert r.status_code == 200, r.text

        _post(
            client,
            {
                "name": "linear",
                "config": {
                    "type": "http",
                    "url": "https://api.linear.app/mcp",
                    "auth": {
                        "type": "bearer",
                        "token": "${secret:linear_token}",
                    },
                },
                "replace": False,
            },
        )
        # The live AppConfig has the EXPANDED value, not the placeholder.
        live = state.runtime_config.config.upstreams["linear"]
        assert isinstance(live.auth, BearerAuthConfig)
        assert live.auth.token == "lin_live_real"
