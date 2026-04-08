"""Tests for the first-run onboarding wizard.

Covers the whole backend surface:

- ``ConfigStore.ensure_onboarding_row`` + state transitions
- ``AuthConfig.token`` precedence in ``resolve_admin_token``
- Auth bypass on the onboarding endpoints while inactive vs. active
- ``/admin/api/onboarding/set_admin_token`` happy path + rejection
- ``/admin/api/onboarding/add_upstream`` (optional step)
- ``/admin/api/onboarding/finish`` (must come after set_admin_token)
- The "onboarding_required" 503 middleware on every *other* admin path
- 410 Gone after finish
- TTL expiry behaviour
- Loopback-only gating + override via MCPY_ONBOARDING_ALLOWED_CLIENTS
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from mcp_proxy.config import AppConfig, AuthConfig, resolve_admin_token
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.secrets import SecretsManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.storage.config_store import ConfigStore, open_store
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


# ---------------------------------------------------------------------------
# resolve_admin_token: direct vs env
# ---------------------------------------------------------------------------


def test_resolve_admin_token_prefers_direct() -> None:
    cfg = AuthConfig(token="literal", token_env="MCPY_SHOULD_NOT_READ")
    assert resolve_admin_token(cfg, env_lookup=lambda _: "env-value") == "literal"


def test_resolve_admin_token_falls_back_to_env() -> None:
    cfg = AuthConfig(token=None, token_env="TOK")
    assert resolve_admin_token(cfg, env_lookup={"TOK": "from-env"}.get) == "from-env"


def test_resolve_admin_token_returns_none_when_nothing_set() -> None:
    assert resolve_admin_token(AuthConfig(), env_lookup=lambda _: None) is None


def test_redact_masks_direct_token() -> None:
    from mcp_proxy.config import redact_secrets

    out = redact_secrets(
        {
            "auth": {"token": "hunter2", "token_env": "MCP_PROXY_TOKEN"},
            "upstreams": {},
        }
    )
    assert out["auth"]["token"] == "***REDACTED***"
    assert out["auth"]["token_env"] == "***REDACTED_ENV***"


# ---------------------------------------------------------------------------
# ConfigStore onboarding state
# ---------------------------------------------------------------------------


def _build_store(tmp_path: Path) -> ConfigStore:
    return open_store(
        f"sqlite:///{tmp_path / 'mcpy.db'}",
        fernet=Fernet(Fernet.generate_key()),
    )


def test_ensure_onboarding_row_is_idempotent(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    first = store.ensure_onboarding_row()
    second = store.ensure_onboarding_row()
    assert first.created_at == second.created_at
    assert first.admin_token_set_at is None
    store.close()


def test_stamp_admin_token_set_advances_row(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    store.ensure_onboarding_row()
    updated = store.stamp_admin_token_set()
    assert updated.admin_token_set_at is not None
    assert updated.completed_at is None
    store.close()


def test_finish_onboarding_is_terminal(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    store.ensure_onboarding_row()
    store.stamp_admin_token_set()
    done = store.finish_onboarding(completed_by="127.0.0.1")
    assert done.completed_at is not None
    assert done.is_complete()
    # A second finish is a no-op (the WHERE clause excludes the row).
    again = store.finish_onboarding(completed_by="127.0.0.1")
    assert again.completed_at == done.completed_at
    store.close()


def test_onboarding_public_dict_flags(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    state = store.ensure_onboarding_row()
    public = state.to_public_dict(ttl_s=600)
    assert public["active"] is True
    assert public["completed"] is False
    assert public["expired"] is False
    # Simulate an expired TTL by pretending time moved forward.
    public2 = state.to_public_dict(ttl_s=600, now=state.created_at + 601)
    assert public2["active"] is False
    assert public2["expired"] is True
    store.close()


# ---------------------------------------------------------------------------
# Admin API integration
# ---------------------------------------------------------------------------


def _build_app(
    tmp_path: Path,
    *,
    with_onboarding: bool = True,
    require_token: bool = False,
    admin_token: str | None = None,
) -> tuple[Any, ConfigStore]:
    """Spin up an AppState + FastAPI app wired to an isolated DB.

    Mirrors what ``cli.build_state`` does at bootstrap but lets each
    test choose whether the onboarding row is preseeded and whether
    the config already carries an admin token.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    fernet = Fernet(Fernet.generate_key())
    store = open_store(f"sqlite:///{state_dir / 'mcpy.db'}", fernet=fernet)

    raw: dict[str, Any] = {
        "auth": {"token": admin_token, "token_env": None},
        "admin": {
            "mount_name": "__admin__",
            "enabled": True,
            "require_token": require_token,
            "allowed_clients": [],
        },
        "telemetry": {"enabled": True, "sink": "noop"},
        "upstreams": {},
    }
    store.save_active_config(raw, source="test.bootstrap")
    if with_onboarding:
        store.ensure_onboarding_row()

    cfg = AppConfig.model_validate(raw)
    registry = PluginRegistry()
    registry.load_entry_points()
    secrets_manager = SecretsManager(
        state_dir=state_dir, config_store=store
    )
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
    return app, store


def test_status_reports_required_on_fresh_install(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/admin/api/onboarding/status")
        assert r.status_code == 200
        body = r.json()
        assert body["active"] is True
        assert body["completed"] is False
        assert body["required"] is True
        assert body["expired"] is False
    store.close()


def test_status_reports_not_required_when_no_row(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path, with_onboarding=False)
    with TestClient(app) as client:
        r = client.get("/admin/api/onboarding/status")
        assert r.status_code == 200
        body = r.json()
        assert body["active"] is False
        assert body["required"] is False
    store.close()


def test_middleware_returns_503_on_other_admin_paths_while_active(
    tmp_path: Path,
) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/admin/api/config")
        assert r.status_code == 503
        body = r.json()
        assert body["detail"] == "onboarding_required"
        assert body["onboarding"]["required"] is True
        # OAuth callback is deliberately left open so browsers hitting
        # the redirect target still work, even with onboarding active.
        r = client.get("/admin/api/oauth/callback")
        assert r.status_code != 503
    store.close()


def test_set_admin_token_happy_path(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/set_admin_token",
            json={"token": "a-very-long-and-random-token-value"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] is True
        assert body["onboarding"]["admin_token_set_at"] is not None

        # Subsequent admin API calls now work with the new token.
        headers = {"Authorization": "Bearer a-very-long-and-random-token-value"}
        # The middleware still blocks non-onboarding paths until the
        # operator actually calls /finish, so we test /status which is
        # always open.
        r = client.get("/admin/api/onboarding/status", headers=headers)
        assert r.status_code == 200
    store.close()


def test_set_admin_token_rejects_short_token(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/set_admin_token",
            json={"token": "short"},
        )
        assert r.status_code == 400
    store.close()


def test_finish_requires_token_first(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/admin/api/onboarding/finish")
        assert r.status_code == 400
        assert "admin token" in r.json()["detail"].lower()
    store.close()


def test_finish_happy_path_and_410_after(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/set_admin_token",
            json={"token": "a-very-long-and-random-token-value"},
        )
        assert r.status_code == 200
        r = client.post("/admin/api/onboarding/finish")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["completed"] is True
        assert body["active"] is False

        # Any subsequent onboarding mutation returns 410 Gone.
        r = client.post(
            "/admin/api/onboarding/set_admin_token",
            json={"token": "another-very-long-token-value"},
        )
        assert r.status_code == 410
        r = client.post("/admin/api/onboarding/finish")
        assert r.status_code == 410

        # Normal admin endpoints are now reachable (they still
        # require the token).
        headers = {"Authorization": "Bearer a-very-long-and-random-token-value"}
        r = client.get("/admin/api/config", headers=headers)
        assert r.status_code == 200, r.text
    store.close()


def test_add_upstream_during_onboarding_stamps_row(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/add_upstream",
            json={
                "name": "first",
                "config": {"type": "http", "url": "https://first.example/mcp"},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] is True
        assert body["onboarding"]["first_upstream_at"] is not None
    store.close()


def test_onboarding_loopback_only_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCPY_ONBOARDING_ALLOWED_CLIENTS", "203.0.113.9")
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/set_admin_token",
            json={"token": "a-very-long-and-random-token-value"},
        )
        # FastAPI's TestClient reports client.host == "testclient" which
        # we override to not be allowed, so we expect 403.
        assert r.status_code == 403
        assert "loopback-only" in r.json()["detail"]
    store.close()


def test_onboarding_expired_returns_410(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCPY_ONBOARDING_TTL_S", "60")
    app, store = _build_app(tmp_path)
    # Move the row's created_at into the past by rewriting the DB directly.
    from sqlalchemy import update

    from mcp_proxy.storage.schema import onboarding_table

    with store.engine.begin() as conn:
        long_ago = time.time() - 3600
        from datetime import datetime, timezone

        conn.execute(
            update(onboarding_table).values(
                created_at=datetime.fromtimestamp(long_ago, tz=timezone.utc)
            )
        )

    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/set_admin_token",
            json={"token": "a-very-long-and-random-token-value"},
        )
        assert r.status_code == 410
        assert "expired" in r.json()["detail"]
    store.close()


def test_oauth_callback_bypasses_onboarding_gate(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        # Callback without state/code returns 400 (not 503), proving the
        # onboarding gate let the request through.
        r = client.get("/admin/api/oauth/callback")
        assert r.status_code == 400
    store.close()


# ---------------------------------------------------------------------------
# Fail-closed middleware: the admin API must never be reachable with no token
# ---------------------------------------------------------------------------


def test_admin_api_blocked_when_no_token_configured(tmp_path: Path) -> None:
    """Baseline fail-closed case: no onboarding row, no admin token, no
    ``require_token``. The middleware must still refuse every
    non-onboarding admin API path with 503 ``admin_token_not_configured``.
    """
    app, store = _build_app(
        tmp_path,
        with_onboarding=False,
        require_token=False,
        admin_token=None,
    )
    with TestClient(app) as client:
        r = client.get("/admin/api/config")
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["detail"] == "admin_token_not_configured"
        # The onboarding projection is still included so the dashboard
        # can render the right empty-state.
        assert "onboarding" in body

        # Onboarding status is always reachable even without a token.
        r = client.get("/admin/api/onboarding/status")
        assert r.status_code == 200

        # OAuth callback is deliberately excluded from the gate so
        # browser redirects from the auth server can complete.
        r = client.get("/admin/api/oauth/callback")
        assert r.status_code == 400
    store.close()


def test_admin_api_stays_closed_after_expired_onboarding_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TTL-expiry hole the fail-closed gate exists to plug.

    Before the fix: ``public["required"]`` flips to ``False`` once the
    onboarding window lapses, which used to let the middleware fall
    through to a no-auth ``require_admin_auth`` and expose the admin
    API without a bearer. After the fix: the
    ``resolve_admin_token`` branch still returns 503 because the live
    config has neither ``auth.token`` nor a readable ``auth.token_env``.
    """
    monkeypatch.setenv("MCPY_ONBOARDING_TTL_S", "60")
    app, store = _build_app(
        tmp_path,
        with_onboarding=True,
        require_token=False,
        admin_token=None,
    )
    # Backdate the onboarding row far past the TTL, same trick
    # test_onboarding_expired_returns_410 uses.
    from datetime import datetime, timezone

    from sqlalchemy import update as sa_update

    from mcp_proxy.storage.schema import onboarding_table

    with store.engine.begin() as conn:
        conn.execute(
            sa_update(onboarding_table).values(
                created_at=datetime.fromtimestamp(
                    time.time() - 3600, tz=timezone.utc
                )
            )
        )

    with TestClient(app) as client:
        # This is the exact scenario the hole left open: onboarding
        # row present but expired AND no real token configured.
        r = client.get("/admin/api/config")
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["detail"] == "admin_token_not_configured"

        # Other admin endpoints must also stay closed, not just /config.
        for p in (
            "/admin/api/upstreams",
            "/admin/api/secrets",
            "/admin/api/policies",
            "/admin/api/traffic",
        ):
            r = client.get(p)
            assert r.status_code == 503, f"{p} should be gated, got {r.status_code}"
            assert r.json()["detail"] == "admin_token_not_configured"

        # Onboarding mutations still return 410 (expired) via their
        # own access-control helper — the fail-closed gate does NOT
        # replace that, they compose.
        r = client.post(
            "/admin/api/onboarding/set_admin_token",
            json={"token": "a-very-long-and-random-token-value"},
        )
        assert r.status_code == 410
    store.close()


def test_admin_api_open_once_token_is_configured(tmp_path: Path) -> None:
    """Sanity check the other direction: when a real token IS in the
    config, the fail-closed branch doesn't accidentally block valid
    requests. The normal ``require_admin_auth`` gate takes over.
    """
    app, store = _build_app(
        tmp_path,
        with_onboarding=False,
        require_token=True,
        admin_token="a-very-long-and-random-token-value",
    )
    with TestClient(app) as client:
        # No bearer → 401 from require_admin_auth, not 503.
        r = client.get("/admin/api/config")
        assert r.status_code == 401

        # With bearer → 200.
        r = client.get(
            "/admin/api/config",
            headers={"Authorization": "Bearer a-very-long-and-random-token-value"},
        )
        assert r.status_code == 200
    store.close()
