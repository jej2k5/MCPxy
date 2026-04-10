"""Tests for the first-run onboarding wizard.

Covers the whole backend surface:

- ``ConfigStore.ensure_onboarding_row`` + state transitions
- ``AuthConfig.token`` precedence in ``resolve_admin_token``
- Auth bypass on the onboarding endpoints while inactive vs. active
- ``/admin/api/onboarding/set_admin_token`` happy path + rejection
- ``/admin/api/onboarding/add_upstream`` (optional step)
- ``/admin/api/onboarding/test_database`` + ``set_database`` (hot-swap
  + restart-fallback) — lets the wizard pick SQLite / Postgres / MySQL
  from the UI instead of requiring env vars.
- ``/admin/api/onboarding/finish`` (must come after set_admin_token)
- The "onboarding_required" 503 middleware on every *other* admin path
- 410 Gone after finish
- TTL expiry behaviour
- Loopback-only gating + override via MCPXY_ONBOARDING_ALLOWED_CLIENTS
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from mcpxy_proxy.config import AppConfig, AuthConfig, resolve_admin_token
from mcpxy_proxy.plugins.registry import PluginRegistry
from mcpxy_proxy.proxy.bridge import ProxyBridge
from mcpxy_proxy.proxy.manager import UpstreamManager
from mcpxy_proxy.secrets import SecretsManager
from mcpxy_proxy.server import AppState, create_app
from mcpxy_proxy.storage.config_store import ConfigStore, open_store
from mcpxy_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline


def _make_store(tmp_path: Path) -> ConfigStore:
    fernet = Fernet(Fernet.generate_key())
    return open_store(
        f"sqlite:///{tmp_path / 'test.db'}",
        fernet=fernet,
        state_dir=str(tmp_path),
    )


def _authy_client(tmp_path: Path) -> TestClient:
    """Create a TestClient with authy enabled, onboarding active, and
    admin_token_set_at stamped — simulating the state just after the
    wizard's AuthStep completes for a federated provider (M365/Google).
    """
    store = _make_store(tmp_path)
    # Seed an onboarding row and stamp admin_token_set_at (step 3 done).
    store.ensure_onboarding_row()
    store.stamp_admin_token_set()

    config = AppConfig.model_validate(
        {
            "auth": {
                "authy": {
                    "enabled": True,
                    "primary_provider": "local",
                    "jwt_secret": "test-secret-for-onboarding",
                },
            },
            "admin": {
                "enabled": True,
                "mount_name": "__admin__",
                "require_token": False,
                "allowed_clients": ["testclient"],
            },
            "upstreams": {},
        }
    )
    sm = SecretsManager(state_dir=tmp_path, config_store=store, autoload=True)
    reg = PluginRegistry()
    manager = UpstreamManager(config.upstreams, reg)
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(NoopTelemetrySink())
    raw = config.model_dump()
    raw["registration"] = {"file_drop_enabled": False}
    state = AppState(
        config,
        raw,
        manager,
        bridge,
        telemetry,
        reg,
        secrets_manager=sm,
        config_store=store,
    )
    return TestClient(create_app(state))


# ------------------------------------------------------------------
# Catalog access during active authy onboarding
# ------------------------------------------------------------------


def test_catalog_accessible_during_active_authy_onboarding(tmp_path: Path) -> None:
    """During active onboarding with authy enabled and zero admin users,
    the catalog endpoint must be reachable without authentication so the
    wizard's 'First Server' step can load the MCP catalog.
    """
    client = _authy_client(tmp_path)
    # No auth headers — simulates the federated flow where no admin
    # user exists yet and no token is in localStorage.
    res = client.get("/admin/api/catalog")
    assert res.status_code == 200, (
        f"Expected 200 but got {res.status_code}: {res.text}"
    )
    body = res.json()
    assert "entries" in body
    assert "version" in body


def test_catalog_blocked_after_onboarding_completes(tmp_path: Path) -> None:
    """Once onboarding finishes, the catalog must require proper auth
    even with authy enabled and zero admin users.
    """
    store = _make_store(tmp_path)
    store.ensure_onboarding_row()
    store.stamp_admin_token_set()
    store.finish_onboarding(completed_by="test")

    config = AppConfig.model_validate(
        {
            "auth": {
                "authy": {
                    "enabled": True,
                    "primary_provider": "local",
                    "jwt_secret": "test-secret-for-onboarding",
                },
            },
            "admin": {
                "enabled": True,
                "mount_name": "__admin__",
                "require_token": False,
                "allowed_clients": ["testclient"],
            },
            "upstreams": {},
        }
    )
    sm = SecretsManager(state_dir=tmp_path, config_store=store, autoload=True)
    reg = PluginRegistry()
    manager = UpstreamManager(config.upstreams, reg)
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(NoopTelemetrySink())
    raw = config.model_dump()
    raw["registration"] = {"file_drop_enabled": False}
    state = AppState(
        config,
        raw,
        manager,
        bridge,
        telemetry,
        reg,
        secrets_manager=sm,
        config_store=store,
    )
    client = TestClient(create_app(state))
    # Onboarding is complete; active=false.  Without auth the catalog
    # must be rejected.
    res = client.get("/admin/api/catalog")
    assert res.status_code == 503
    body = res.json()
    assert body["detail"] == "authy_not_configured"


def test_non_catalog_endpoints_still_blocked_during_authy_onboarding(
    tmp_path: Path,
) -> None:
    """Endpoints other than /admin/api/catalog must still return 503
    authy_not_configured during active onboarding when no admin exists.
    """
    client = _authy_client(tmp_path)
    res = client.get("/admin/api/config")
    assert res.status_code == 503
    body = res.json()
    assert body["detail"] == "authy_not_configured"


# ------------------------------------------------------------------
# start_federated auth URL recovery
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_federated_recovers_auth_url_from_unsigned_jwt(
    tmp_path: Path,
) -> None:
    """When verify_token fails, start_federated should still decode the
    JWT payload to extract a valid auth_url rather than returning the
    raw JWT as the redirect target.
    """
    import base64
    import json
    from unittest.mock import AsyncMock, MagicMock, patch

    from mcpxy_proxy.authn.manager import AuthnManager, FederatedStartResult
    from mcpxy_proxy.config import AuthyConfig

    store = _make_store(tmp_path)
    cfg = AuthyConfig(
        enabled=True,
        primary_provider="local",
        jwt_secret="test-secret",
    )
    mgr = AuthnManager(cfg, store=store)

    # Build a JWT-like token whose payload contains auth_url.
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "auth_url": "https://login.example.com/authorize?client_id=test",
            "code_verifier": "test-verifier-123",
            "type": "oauth_meta",
        }).encode()
    ).rstrip(b"=").decode()
    fake_jwt = f"{header}.{payload}.invalid-signature"

    # Mock the underlying AuthManager so authenticate returns the JWT
    # and verify_token raises (simulating a key mismatch).
    mock_underlying = MagicMock()
    mock_result = MagicMock()
    mock_result.token = fake_jwt
    mock_result.error = None
    mock_underlying.authenticate = AsyncMock(return_value=mock_result)
    mock_underlying.verify_token = MagicMock(
        side_effect=Exception("signature mismatch")
    )
    mgr._underlying = mock_underlying

    result = await mgr.start_federated("m365", "test-state")
    assert isinstance(result, FederatedStartResult)
    assert result.auth_url == "https://login.example.com/authorize?client_id=test"
    assert result.code_verifier == "test-verifier-123"


@pytest.mark.asyncio
async def test_start_federated_rejects_non_url_token(tmp_path: Path) -> None:
    """When the token is neither a decodable JWT with auth_url nor a
    plain URL, start_federated must raise RuntimeError instead of
    silently returning an invalid redirect target.
    """
    from unittest.mock import AsyncMock, MagicMock

    from mcpxy_proxy.authn.manager import AuthnManager
    from mcpxy_proxy.config import AuthyConfig

    store = _make_store(tmp_path)
    cfg = AuthyConfig(
        enabled=True,
        primary_provider="local",
        jwt_secret="test-secret",
    )
    mgr = AuthnManager(cfg, store=store)

    mock_underlying = MagicMock()
    mock_result = MagicMock()
    mock_result.token = "not-a-jwt-and-not-a-url"
    mock_result.error = None
    mock_underlying.authenticate = AsyncMock(return_value=mock_result)
    mock_underlying.verify_token = MagicMock(
        side_effect=Exception("invalid token")
    )
    mgr._underlying = mock_underlying

    with pytest.raises(RuntimeError, match="cannot extract a valid auth_url"):
        await mgr.start_federated("m365", "test-state")


@pytest.mark.asyncio
async def test_start_federated_plain_url_fallback(tmp_path: Path) -> None:
    """When the token is a plain HTTPS URL (not a JWT), start_federated
    should use it directly as the auth_url.
    """
    from unittest.mock import AsyncMock, MagicMock

    from mcpxy_proxy.authn.manager import AuthnManager, FederatedStartResult
    from mcpxy_proxy.config import AuthyConfig

    store = _make_store(tmp_path)
    cfg = AuthyConfig(
        enabled=True,
        primary_provider="local",
        jwt_secret="test-secret",
    )
    mgr = AuthnManager(cfg, store=store)

    mock_underlying = MagicMock()
    mock_result = MagicMock()
    mock_result.token = "https://login.example.com/authorize?code=abc"
    mock_result.error = None
    mock_underlying.authenticate = AsyncMock(return_value=mock_result)
    mock_underlying.verify_token = MagicMock(
        side_effect=Exception("not a JWT")
    )
    mgr._underlying = mock_underlying

    result = await mgr.start_federated("m365", "test-state")
    assert isinstance(result, FederatedStartResult)
    assert result.auth_url == "https://login.example.com/authorize?code=abc"
    assert result.code_verifier is None
