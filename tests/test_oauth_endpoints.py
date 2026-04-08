"""Admin-API level tests for the OAuth authorize/callback/status flow.

Ensures the FastAPI layer wires OAuthManager correctly:

- GET  /admin/api/oauth                       — list upstreams
- GET  /admin/api/oauth/{upstream}/status     — per-upstream status
- POST /admin/api/oauth/{upstream}/start      — begin flow
- GET  /admin/api/oauth/callback              — PKCE round-trip finish
- DELETE /admin/api/oauth/{upstream}/token    — revoke

We reuse the same in-process MockAuthServer as test_oauth_flow.py, but
this time the OAuth manager is reached via the running TestClient so
auth gating and URL routing are exercised.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_proxy.auth.oauth import OAuthManager
from mcp_proxy.config import AppConfig
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.secrets import SecretsManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline

from tests.test_oauth_flow import MockAuthServer, _build_transport_router


def _build_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_oauth_upstream: bool = True
) -> tuple[Any, MockAuthServer, SecretsManager]:
    monkeypatch.setenv("MCP_PROXY_TOKEN", "admin-test-token")
    auth = MockAuthServer()
    raw: dict[str, Any] = {
        "auth": {"token_env": "MCP_PROXY_TOKEN"},
        "admin": {
            "mount_name": "__admin__",
            "enabled": True,
            "require_token": True,
            "allowed_clients": [],
        },
        "telemetry": {"enabled": True, "sink": "noop"},
        "upstreams": {},
    }
    if with_oauth_upstream:
        raw["upstreams"]["notion"] = {
            "type": "http",
            "url": "https://api.notion.example/mcp",
            "auth": {
                "type": "oauth2",
                "issuer": auth.issuer,
                "client_id": "static_client",
            },
        }
    cfg = AppConfig.model_validate(raw)
    registry = PluginRegistry()
    registry.load_entry_points()
    secrets = SecretsManager(
        state_dir=tmp_path / "state", key_override=SecretsManager.generate_key()
    )
    oauth_manager = OAuthManager(
        secrets=secrets,
        http_client=httpx.AsyncClient(transport=_build_transport_router(auth)),
    )
    manager = UpstreamManager(cfg.upstreams, registry, oauth_manager=oauth_manager)
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(sink=NoopTelemetrySink())
    state = AppState(
        cfg,
        raw,
        manager,
        bridge,
        telemetry,
        registry=registry,
        secrets_manager=secrets,
        oauth_manager=oauth_manager,
    )
    app = create_app(state)
    return app, auth, secrets


def test_oauth_list_returns_registered_upstreams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _ = _build_app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer admin-test-token"}
    with TestClient(app) as client:
        r = client.get("/admin/api/oauth", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["upstreams"]) == 1
        assert body["upstreams"][0]["upstream"] == "notion"
        assert body["upstreams"][0]["has_token"] is False


def test_oauth_start_returns_authorization_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, auth, _ = _build_app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer admin-test-token"}
    with TestClient(app) as client:
        r = client.post("/admin/api/oauth/notion/start", headers=headers, json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["upstream"] == "notion"
        parsed = urlparse(body["authorization_url"])
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        assert params["response_type"] == "code"
        assert params["code_challenge_method"] == "S256"
        assert params["client_id"] == "static_client"


def test_oauth_callback_completes_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, auth, secrets = _build_app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer admin-test-token"}
    with TestClient(app) as client:
        r = client.post("/admin/api/oauth/notion/start", headers=headers, json={})
        body = r.json()
        parsed = urlparse(body["authorization_url"])
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        # Simulate auth-server consent: issue a code bound to the state.
        code = auth.simulate_authorize(
            client_id=params["client_id"],
            redirect_uri=params["redirect_uri"],
            state=params["state"],
            code_challenge=params["code_challenge"],
        )

        # The callback is INTENTIONALLY unauthenticated (the user's
        # browser hits it after redirect, with no bearer token). The
        # opaque ``state`` is the CSRF binding.
        r = client.get(
            "/admin/api/oauth/callback",
            params={"code": code, "state": params["state"]},
        )
        assert r.status_code == 200, r.text
        assert "Authorization complete" in r.text

        # Status now reports has_token=true.
        r = client.get("/admin/api/oauth/notion/status", headers=headers)
        status = r.json()
        assert status["has_token"] is True
        assert status["refresh_available"] is True

        # The token is persisted in the secrets store under the internal key.
        assert secrets.exists("__oauth_token__notion")
        # And it is hidden from list_public / the /admin/api/secrets response.
        r = client.get("/admin/api/secrets", headers=headers)
        assert r.status_code == 200
        names = [s["name"] for s in r.json()["secrets"]]
        assert "__oauth_token__notion" not in names


def test_oauth_callback_rejects_unknown_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _ = _build_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.get(
            "/admin/api/oauth/callback",
            params={"code": "random", "state": "never-started"},
        )
        assert r.status_code == 400
        assert "no pending authorization" in r.text.lower() or "Authorization failed" in r.text


def test_oauth_callback_handles_error_query_param(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _ = _build_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.get(
            "/admin/api/oauth/callback",
            params={"error": "access_denied"},
        )
        assert r.status_code == 400
        assert "access_denied" in r.text


def test_oauth_start_requires_admin_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _ = _build_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.post("/admin/api/oauth/notion/start", json={})
        assert r.status_code == 401


def test_oauth_revoke_drops_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, auth, secrets = _build_app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer admin-test-token"}
    with TestClient(app) as client:
        r = client.post("/admin/api/oauth/notion/start", headers=headers, json={})
        body = r.json()
        parsed = urlparse(body["authorization_url"])
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        code = auth.simulate_authorize(
            client_id=params["client_id"],
            redirect_uri=params["redirect_uri"],
            state=params["state"],
            code_challenge=params["code_challenge"],
        )
        r = client.get(
            "/admin/api/oauth/callback",
            params={"code": code, "state": params["state"]},
        )
        assert r.status_code == 200

        r = client.delete("/admin/api/oauth/notion/token", headers=headers)
        assert r.status_code == 200
        assert r.json() == {"revoked": True, "upstream": "notion"}
        assert not secrets.exists("__oauth_token__notion")

        r = client.get("/admin/api/oauth/notion/status", headers=headers)
        assert r.json()["has_token"] is False


def test_runtime_config_reload_reregisters_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Start with no oauth upstream, then hot-apply a config that adds one.
    # The on_config_applied hook must register it with OAuthManager so
    # subsequent /oauth calls can find it.
    app, auth, _ = _build_app(tmp_path, monkeypatch, with_oauth_upstream=False)
    headers = {"Authorization": "Bearer admin-test-token"}
    new_cfg = {
        "auth": {"token_env": "MCP_PROXY_TOKEN"},
        "admin": {"mount_name": "__admin__", "enabled": True, "require_token": True, "allowed_clients": []},
        "telemetry": {"enabled": True, "sink": "noop"},
        "upstreams": {
            "linear": {
                "type": "http",
                "url": "https://api.linear.example/mcp",
                "auth": {
                    "type": "oauth2",
                    "issuer": auth.issuer,
                    "client_id": "static_client",
                },
            }
        },
    }
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/config", headers=headers, json={"config": new_cfg}
        )
        assert r.status_code == 200, r.text
        assert r.json()["applied"] is True

        r = client.post(
            "/admin/api/oauth/linear/start", headers=headers, json={}
        )
        assert r.status_code == 200, r.text
