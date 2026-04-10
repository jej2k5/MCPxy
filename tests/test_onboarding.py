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


# ---------------------------------------------------------------------------
# resolve_admin_token: direct vs env
# ---------------------------------------------------------------------------


def test_resolve_admin_token_prefers_direct() -> None:
    cfg = AuthConfig(token="literal", token_env="MCPXY_SHOULD_NOT_READ")
    assert resolve_admin_token(cfg, env_lookup=lambda _: "env-value") == "literal"


def test_resolve_admin_token_falls_back_to_env() -> None:
    cfg = AuthConfig(token=None, token_env="TOK")
    assert resolve_admin_token(cfg, env_lookup={"TOK": "from-env"}.get) == "from-env"


def test_resolve_admin_token_returns_none_when_nothing_set() -> None:
    assert resolve_admin_token(AuthConfig(), env_lookup=lambda _: None) is None


def test_resolve_admin_token_treats_empty_env_as_unset() -> None:
    """Docker Compose expands ``${MCP_PROXY_TOKEN:-}`` to an empty string
    when the operator hasn't populated ``.env``, so the container env has
    ``MCP_PROXY_TOKEN=""`` — set but empty. Must be treated identically to
    a completely unset env var, otherwise the fail-closed middleware and
    the onboarding gate disagree on whether a bearer is configured.
    """
    cfg = AuthConfig(token=None, token_env="MCP_PROXY_TOKEN")
    assert resolve_admin_token(cfg, env_lookup={"MCP_PROXY_TOKEN": ""}.get) is None


def test_redact_masks_direct_token() -> None:
    from mcpxy_proxy.config import redact_secrets

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
        f"sqlite:///{tmp_path / 'mcpxy.db'}",
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
    store = open_store(f"sqlite:///{state_dir / 'mcpxy.db'}", fernet=fernet)

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
    monkeypatch.setenv("MCPXY_ONBOARDING_ALLOWED_CLIENTS", "203.0.113.9")
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


def test_parse_allowed_clients_splits_literals_and_networks() -> None:
    """The parser keeps sentinel strings like ``testclient`` / ``localhost``
    as literals while coercing valid IPs/CIDRs into network objects.
    Bare IPs become /32 or /128 networks via ``strict=False``.
    """
    from mcpxy_proxy.server import _parse_allowed_clients

    literals, networks = _parse_allowed_clients(
        [
            "127.0.0.1",
            "::1",
            "localhost",
            "testclient",
            "172.66.0.0/16",
            "",  # blank entries are ignored
            "garbage!",
        ]
    )
    assert literals == {"localhost", "testclient", "garbage!"}
    assert len(networks) == 3
    rendered = sorted(str(n) for n in networks)
    assert rendered == ["127.0.0.1/32", "172.66.0.0/16", "::1/128"]


def test_client_ip_allowed_matches_cidr_and_literal() -> None:
    """The allowed-check honours both literal exact matches (for
    ``testclient``/``localhost`` sentinels) and CIDR membership (for
    Docker NAT ranges like 172.66.0.0/16).
    """
    from mcpxy_proxy.server import _client_ip_allowed, _parse_allowed_clients

    literals, networks = _parse_allowed_clients(
        ["127.0.0.1", "testclient", "172.66.0.0/16"]
    )
    # Literal sentinel match (TestClient's client.host value).
    assert _client_ip_allowed("testclient", literals, networks) is True
    # Loopback IP literal → /32 network match.
    assert _client_ip_allowed("127.0.0.1", literals, networks) is True
    # Docker Desktop NAT IP inside the /16 range.
    assert _client_ip_allowed("172.66.0.243", literals, networks) is True
    # Outside the range → rejected.
    assert _client_ip_allowed("172.67.0.1", literals, networks) is False
    # Unparseable sentinel not in literals → rejected.
    assert _client_ip_allowed("unknown", literals, networks) is False


def test_onboarding_cidr_override_admits_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the Docker Desktop for Mac failure mode:
    the port-forwarded TCP peer lives in a 172.66.x.x NAT subnet that
    the operator cannot enumerate by exact IP (Docker Desktop can pick
    different addresses across reboots). Setting
    ``MCPXY_ONBOARDING_ALLOWED_CLIENTS`` to a CIDR like ``172.66.0.0/16``
    must admit every address in that block. Before CIDR support, the
    parser did exact string matching only and the env var was useless
    for anything but pinned single IPs.
    """
    from unittest.mock import patch

    monkeypatch.setenv(
        "MCPXY_ONBOARDING_ALLOWED_CLIENTS",
        "127.0.0.1,::1,172.66.0.0/16",
    )
    app, store = _build_app(tmp_path)

    with patch("mcpxy_proxy.server._client_ip", return_value="172.66.0.243"):
        with TestClient(app) as client:
            r = client.post(
                "/admin/api/onboarding/set_admin_token",
                json={"token": "a-very-long-and-random-token-value"},
            )
            assert r.status_code == 200, r.text
    store.close()


def test_onboarding_expired_returns_410(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCPXY_ONBOARDING_TTL_S", "60")
    app, store = _build_app(tmp_path)
    # Move the row's created_at into the past by rewriting the DB directly.
    from sqlalchemy import update

    from mcpxy_proxy.storage.schema import onboarding_table

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
    monkeypatch.setenv("MCPXY_ONBOARDING_TTL_S", "60")
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

    from mcpxy_proxy.storage.schema import onboarding_table

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


# ---------------------------------------------------------------------------
# /admin/api/onboarding/status ``database`` block
# ---------------------------------------------------------------------------


def test_status_includes_database_block(tmp_path: Path) -> None:
    """The wizard's Storage step reads these fields to pick defaults,
    populate the "currently using" line, and disable Postgres/MySQL
    options when the driver isn't installed.
    """
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/admin/api/onboarding/status")
        assert r.status_code == 200
        body = r.json()
        assert "database" in body
        db = body["database"]
        assert "current_url_masked" in db
        assert "current_dialect" in db
        assert db["current_dialect"] == "sqlite"
        assert "available_dialects" in db
        assert "sqlite" in db["available_dialects"]
        assert db["bootstrap_file_present"] is False
    store.close()


def test_status_database_block_present_even_without_row(tmp_path: Path) -> None:
    """Status is unauthenticated and must be reachable during the
    pre-first-run window when the onboarding row hasn't been seeded
    yet — the database block must still render so the UI can fall
    back to a sensible empty state.
    """
    app, store = _build_app(tmp_path, with_onboarding=False)
    with TestClient(app) as client:
        r = client.get("/admin/api/onboarding/status")
        assert r.status_code == 200
        body = r.json()
        assert "database" in body
        assert body["database"]["current_dialect"] == "sqlite"
    store.close()


# ---------------------------------------------------------------------------
# /admin/api/onboarding/test_database
# ---------------------------------------------------------------------------


def test_test_database_with_valid_sqlite_url(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    target = tmp_path / "target.db"
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/test_database",
            json={"url": f"sqlite:///{target}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["dialect"] == "sqlite"
        assert body["url_masked"].startswith("sqlite:")
    store.close()


def test_test_database_with_structured_body_rejects_missing_dialect(
    tmp_path: Path,
) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/admin/api/onboarding/test_database", json={"host": "h"})
        assert r.status_code == 400
        assert "dialect" in r.json()["detail"] or "url" in r.json()["detail"]
    store.close()


def test_test_database_rejects_in_memory_sqlite(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/test_database",
            json={"url": "sqlite:///:memory:"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert ":memory:" in body["error"]
    store.close()


def test_test_database_rejects_newline_in_url(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/test_database",
            json={"url": "sqlite:///x\ny.db"},
        )
        assert r.status_code == 400
    store.close()


def test_test_database_reports_error_without_raising(tmp_path: Path) -> None:
    """Even for nonsense URLs the endpoint must return ok=false with a
    message — it never 500s and never crashes the onboarding gate.
    """
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/test_database",
            json={"url": "not-a-url-at-all"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert body["error"]
    store.close()


def test_test_database_blocked_after_finish(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    target = tmp_path / "target.db"
    with TestClient(app) as client:
        client.post(
            "/admin/api/onboarding/set_admin_token",
            json={"token": "a-very-long-and-random-token-value"},
        )
        client.post("/admin/api/onboarding/finish")
        r = client.post(
            "/admin/api/onboarding/test_database",
            json={"url": f"sqlite:///{target}"},
        )
        assert r.status_code == 410
    store.close()


# ---------------------------------------------------------------------------
# /admin/api/onboarding/set_database
# ---------------------------------------------------------------------------


def test_set_database_rejects_dialect_swap_without_ack(tmp_path: Path) -> None:
    """Switching dialect without acknowledging the Fernet-key caveat
    is a footgun; the backend must refuse it even if the UI somehow
    manages to skip the checkbox.
    """
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/set_database",
            json={
                "url": "postgresql://u:p@example.invalid:5432/db",
                "secrets_key_ack": False,
            },
        )
        # 400 if psycopg2 is installed (driver check passes, ack check
        # fires) or 400 regardless of driver state because ack fires
        # before probing. Either way it's a 400.
        assert r.status_code == 400
        assert "secrets_key_ack" in r.json()["detail"]
    store.close()


def test_set_database_same_sqlite_url_is_noop_hot_swap(tmp_path: Path) -> None:
    """Switching to the SQLite file the proxy is already using must
    still succeed and report ``mode: hot_swap`` — same dialect → no
    ack required, and the in-memory config survives unchanged.
    """
    app, store = _build_app(tmp_path)
    current_url = str(store.engine.url)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/set_database",
            json={"url": current_url},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["mode"] == "hot_swap"


def test_set_database_hot_swap_to_fresh_sqlite_file(tmp_path: Path) -> None:
    """Full end-to-end hot-swap: point the wizard at a brand-new
    SQLite file, confirm the bootstrap file is written, the onboarding
    row is re-seeded on the target, and the proxy's live store now
    answers reads from the new DB.
    """
    from mcpxy_proxy.storage.bootstrap import load_bootstrap

    app, store = _build_app(tmp_path)
    target = tmp_path / "state" / "new-target.db"
    target_url = f"sqlite:///{target}"
    with TestClient(app) as client:
        # Same dialect → no ack needed.
        r = client.post(
            "/admin/api/onboarding/set_database",
            json={"url": target_url},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["mode"] == "hot_swap"
        # bootstrap.json is written with the new URL.
        bootstrap = load_bootstrap(tmp_path / "state")
        assert bootstrap is not None
        assert bootstrap.db_url == target_url
        # The target file exists and has the schema + config row.
        assert target.exists()
        # The next /status reflects the new current URL.
        r = client.get("/admin/api/onboarding/status")
        assert r.status_code == 200
        db_block = r.json()["database"]
        assert "new-target.db" in db_block["current_url_masked"]
        assert db_block["bootstrap_file_present"] is True
        # The wizard can still continue: set_admin_token works against
        # the new store.
        r = client.post(
            "/admin/api/onboarding/set_admin_token",
            json={"token": "a-very-long-and-random-token-value"},
        )
        assert r.status_code == 200, r.text


def test_set_database_blocked_after_finish(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        client.post(
            "/admin/api/onboarding/set_admin_token",
            json={"token": "a-very-long-and-random-token-value"},
        )
        client.post("/admin/api/onboarding/finish")
        r = client.post(
            "/admin/api/onboarding/set_database",
            json={"url": f"sqlite:///{tmp_path / 'whatever.db'}"},
        )
        assert r.status_code == 410
    store.close()


def test_set_database_refuses_invalid_url(tmp_path: Path) -> None:
    app, store = _build_app(tmp_path)
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/onboarding/set_database",
            json={"url": "not-a-url"},
        )
        assert r.status_code == 400
    store.close()


def test_bootstrap_file_resolves_to_new_url_on_restart(tmp_path: Path) -> None:
    """Simulate a restart: write bootstrap.json then reopen the store
    with no MCPXY_DB_URL set. The new store must land on the bootstrap
    URL, proving the second-boot path works end-to-end.
    """
    from mcpxy_proxy.storage.bootstrap import BootstrapConfig, write_bootstrap
    from mcpxy_proxy.storage.db import resolve_database_url

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "after-restart.db"
    write_bootstrap(
        state_dir,
        BootstrapConfig(db_url=f"sqlite:///{target}"),
    )
    # Clear any env override the test runner might leave behind.
    import os as _os

    prior = _os.environ.pop("MCPXY_DB_URL", None)
    try:
        resolved = resolve_database_url(None, state_dir=state_dir)
        assert resolved == f"sqlite:///{target}"
    finally:
        if prior is not None:
            _os.environ["MCPXY_DB_URL"] = prior
