"""Tests for the SecretsManager store, ``${secret:NAME}`` expansion, and
the admin API CRUD surface.

These are the Layer 1 tests: they cover the first-class secrets store
independently of any HTTP auth / OAuth code, so a regression here is
isolated from the transport integration in Layer 2.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcpxy_proxy.config import (
    SECRET_RE,
    _apply_expansions,
    find_secret_references,
    load_config,
    validate_config_payload,
)
from mcpxy_proxy.secrets import (
    SECRET_NAME_RE,
    SecretNotFoundError,
    SecretStoreError,
    SecretsManager,
)


# ---------------------------------------------------------------------------
# SecretsManager: core store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_get_round_trip(tmp_path: Path) -> None:
    store = SecretsManager(state_dir=tmp_path, key_override=SecretsManager.generate_key())
    await store.set("github_token", "ghp_abc123", description="PAT for GitHub")
    assert store.get("github_token") == "ghp_abc123"
    assert store.exists("github_token")
    # last_used_at is stamped on read
    rec_after_read = store.list_public()[0]
    assert rec_after_read["last_used_at"] is not None


@pytest.mark.asyncio
async def test_list_public_redacts_value(tmp_path: Path) -> None:
    store = SecretsManager(state_dir=tmp_path, key_override=SecretsManager.generate_key())
    await store.set("notion", "ntn_verysecret")
    entries = store.list_public()
    assert len(entries) == 1
    entry = entries[0]
    assert "value" not in entry
    assert entry["value_length"] == len("ntn_verysecret")
    # The preview only shows the first+last 2 chars; the middle is masked
    # with one bullet per character so length is preserved visually.
    assert entry["value_preview"].startswith("nt")
    assert entry["value_preview"].endswith("et")
    assert entry["value_preview"].count("•") == len("ntn_verysecret") - 4


@pytest.mark.asyncio
async def test_delete_removes_record(tmp_path: Path) -> None:
    store = SecretsManager(state_dir=tmp_path, key_override=SecretsManager.generate_key())
    await store.set("foo", "bar")
    assert await store.delete("foo") is True
    assert not store.exists("foo")
    # Deleting a second time is a no-op that returns False.
    assert await store.delete("foo") is False


@pytest.mark.asyncio
async def test_require_raises_on_missing(tmp_path: Path) -> None:
    store = SecretsManager(state_dir=tmp_path, key_override=SecretsManager.generate_key())
    with pytest.raises(SecretNotFoundError):
        store.require("nope")


@pytest.mark.asyncio
async def test_name_validation(tmp_path: Path) -> None:
    store = SecretsManager(state_dir=tmp_path, key_override=SecretsManager.generate_key())
    for bad in ["", "with space", "has/slash", "dots.in.name", "-leading-dash"]:
        with pytest.raises(SecretStoreError):
            await store.set(bad, "v")
    for good in ["github_token", "GH_PAT", "notion-api-key", "x", "svc_01"]:
        await store.set(good, "v")
        assert store.exists(good)


@pytest.mark.asyncio
async def test_value_length_cap(tmp_path: Path) -> None:
    store = SecretsManager(state_dir=tmp_path, key_override=SecretsManager.generate_key())
    with pytest.raises(SecretStoreError):
        await store.set("big", "x" * (64 * 1024 + 1))


# ---------------------------------------------------------------------------
# Persistence + encryption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistence_round_trip_with_same_key(tmp_path: Path) -> None:
    key = SecretsManager.generate_key()
    store = SecretsManager(state_dir=tmp_path, key_override=key)
    await store.set("a", "alpha", description="first")
    await store.set("b", "beta")

    # Spin up a fresh manager with the same key + state dir. It must
    # re-read the encrypted blob and surface the same records.
    store2 = SecretsManager(state_dir=tmp_path, key_override=key)
    assert sorted(store2.known_names()) == ["a", "b"]
    assert store2.get("a") == "alpha"
    assert store2.get("b") == "beta"


@pytest.mark.asyncio
async def test_persistence_with_wrong_key_raises(tmp_path: Path) -> None:
    key = SecretsManager.generate_key()
    store = SecretsManager(state_dir=tmp_path, key_override=key)
    await store.set("x", "y")
    wrong = SecretsManager.generate_key()
    assert wrong != key
    with pytest.raises(SecretStoreError) as exc_info:
        SecretsManager(state_dir=tmp_path, key_override=wrong)
    assert "cannot be decrypted" in str(exc_info.value)


@pytest.mark.asyncio
async def test_secrets_db_row_is_actually_ciphertext(tmp_path: Path) -> None:
    """The DB stores ciphertext, not plaintext, even though the column
    is structured: a hex dump of the SQLite file must not contain the
    plaintext value, and the ``value_ct`` column round-trips through
    Fernet (the token starts with 0x80 → ``gAAAAA`` in base64).
    """
    from sqlalchemy import select
    from mcpxy_proxy.storage.schema import secrets_table

    store = SecretsManager(state_dir=tmp_path, key_override=SecretsManager.generate_key())
    await store.set("nuclear", "launch-code-4782")

    # The whole on-disk SQLite file must not contain the plaintext.
    db_path = tmp_path / "mcpxy.db"
    raw = db_path.read_bytes()
    assert b"launch-code-4782" not in raw

    # And the ``value_ct`` column for that secret is a Fernet token.
    with store.store.engine.connect() as conn:
        row = conn.execute(
            select(secrets_table.c.value_ct).where(secrets_table.c.name == "nuclear")
        ).first()
    assert row is not None
    blob = bytes(row[0])
    assert blob[:6] == b"gAAAAA"


@pytest.mark.asyncio
async def test_auto_generated_key_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No override, no env var: the manager should auto-generate a key and
    # write it to <state_dir>/secrets.key. A subsequent instantiation
    # should pick up the same key and read the existing store.
    monkeypatch.delenv("MCPXY_SECRETS_KEY", raising=False)
    store = SecretsManager(state_dir=tmp_path)
    key_file = tmp_path / "secrets.key"
    assert key_file.exists()
    assert oct((key_file.stat().st_mode & 0o777)) == "0o600"
    await store.set("persistent", "value")

    store2 = SecretsManager(state_dir=tmp_path)
    assert store2.get("persistent") == "value"


@pytest.mark.asyncio
async def test_env_key_preferred_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``MCPXY_SECRETS_KEY`` is set, the file fallback path must NOT
    be touched. We assert that no ``secrets.key`` file is written and
    that constructing a fresh manager with a *different* env key against
    the same DB file raises (because the existing rows decrypt with the
    original key only).
    """
    env_key = SecretsManager.generate_key()
    monkeypatch.setenv("MCPXY_SECRETS_KEY", env_key)
    store = SecretsManager(state_dir=tmp_path)
    assert not (tmp_path / "secrets.key").exists()
    await store.set("x", "y")
    store.close()

    # A fresh manager with a different env key against the same DB file
    # should fail to decrypt the existing row.
    monkeypatch.setenv("MCPXY_SECRETS_KEY", SecretsManager.generate_key())
    with pytest.raises(SecretStoreError):
        SecretsManager(state_dir=tmp_path)


# ---------------------------------------------------------------------------
# ${secret:NAME} expansion
# ---------------------------------------------------------------------------


def test_secret_re_matches_expected_forms() -> None:
    assert SECRET_RE.findall("hello ${secret:github_token} world") == ["github_token"]
    assert SECRET_RE.findall("${secret:a}${secret:b-c}") == ["a", "b-c"]
    assert SECRET_RE.findall("${secret:bad space}") == []
    # The name rule in SECRET_NAME_RE matches the regex inside placeholders
    assert SECRET_NAME_RE.match("github_token")
    assert not SECRET_NAME_RE.match("-leading")


def test_apply_expansions_resolves_secrets_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO_VAR", "hello")
    secrets = {"gh": "ghp_xxx", "notion": "ntn_yyy"}
    payload = {
        "upstreams": {
            "a": {
                "type": "stdio",
                "command": "node",
                "args": ["${env:FOO_VAR}"],
                "env": {"TOKEN": "${secret:gh}"},
            },
            "b": {
                "type": "http",
                "url": "https://example.com",
                "headers": {"Authorization": "Bearer ${secret:notion}"},
            },
        }
    }
    out = _apply_expansions(payload, secrets=secrets.get)
    assert out["upstreams"]["a"]["args"] == ["hello"]
    assert out["upstreams"]["a"]["env"]["TOKEN"] == "ghp_xxx"
    assert out["upstreams"]["b"]["headers"]["Authorization"] == "Bearer ntn_yyy"


def test_missing_secret_expands_to_empty_string() -> None:
    out = _apply_expansions(
        {"x": "${secret:missing}"},
        secrets=lambda name: None,
    )
    assert out == {"x": ""}


def test_find_secret_references_walks_nested() -> None:
    payload = {
        "upstreams": {
            "a": {
                "env": {"K": "${secret:one}"},
                "args": ["${secret:two}", "plain"],
            },
            "b": {"headers": {"X": "prefix-${secret:three}-suffix"}},
        },
        "unrelated": "${secret:four}${env:FOO}",
    }
    assert find_secret_references(payload) == ["four", "one", "three", "two"]


def test_load_config_with_secret_resolver(tmp_path: Path) -> None:
    raw = {
        "upstreams": {
            "gh": {
                "type": "stdio",
                "command": "node",
                "args": ["server.js"],
                "env": {"GITHUB_TOKEN": "${secret:gh_pat}"},
            }
        }
    }
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_config(cfg_path, secrets={"gh_pat": "ghp_real"}.get)
    stdio_cfg = loaded.upstreams["gh"]
    assert stdio_cfg.env["GITHUB_TOKEN"] == "ghp_real"  # type: ignore[union-attr]


def test_validate_config_payload_with_secret_resolver() -> None:
    payload = {
        "upstreams": {
            "http": {
                "type": "http",
                "url": "https://x.example/mcp",
                "headers": {"Authorization": "Bearer ${secret:live}"},
            }
        }
    }
    ok, error = validate_config_payload(payload, secrets={"live": "tkn"}.get)
    assert ok, error


# ---------------------------------------------------------------------------
# Admin API CRUD + runtime apply with secrets
# ---------------------------------------------------------------------------


def _build_app_with_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Minimal AppState + FastAPI app for CRUD + apply testing."""
    from mcpxy_proxy.config import AppConfig
    from mcpxy_proxy.plugins.registry import PluginRegistry
    from mcpxy_proxy.proxy.bridge import ProxyBridge
    from mcpxy_proxy.proxy.manager import UpstreamManager
    from mcpxy_proxy.server import AppState, create_app
    from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline
    from mcpxy_proxy.telemetry.noop_sink import NoopTelemetrySink

    monkeypatch.setenv("MCP_PROXY_TOKEN", "admin-test-token")
    raw = {
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
    cfg = AppConfig.model_validate(raw)
    registry = PluginRegistry()
    registry.load_entry_points()
    manager = UpstreamManager(cfg.upstreams, registry)
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(sink=NoopTelemetrySink())
    secrets = SecretsManager(
        state_dir=tmp_path / "state", key_override=SecretsManager.generate_key()
    )
    state = AppState(
        cfg, raw, manager, bridge, telemetry, registry=registry, secrets_manager=secrets
    )
    app = create_app(state)
    return app, secrets


def test_secrets_api_crud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, secrets = _build_app_with_secrets(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer admin-test-token"}
    with TestClient(app) as client:
        # Empty listing
        r = client.get("/admin/api/secrets", headers=headers)
        assert r.status_code == 200
        assert r.json() == {"secrets": [], "referenced": [], "missing": [], "orphans": []}

        # Create
        r = client.post(
            "/admin/api/secrets",
            headers=headers,
            json={"name": "gh_pat", "value": "ghp_123", "description": "PAT"},
        )
        assert r.status_code == 200
        body = r.json()["secret"]
        assert body["name"] == "gh_pat"
        assert body["description"] == "PAT"
        assert "value" not in body
        assert body["value_preview"].startswith("gh")
        assert secrets.get("gh_pat") == "ghp_123"

        # Listing now includes it and marks it as orphan (no config refs)
        r = client.get("/admin/api/secrets", headers=headers)
        listing = r.json()
        assert len(listing["secrets"]) == 1
        assert listing["orphans"] == ["gh_pat"]
        assert listing["missing"] == []

        # Update (POST is upsert) bumps updated_at but keeps created_at
        r = client.post(
            "/admin/api/secrets",
            headers=headers,
            json={"name": "gh_pat", "value": "ghp_rotated"},
        )
        assert r.status_code == 200
        assert secrets.get("gh_pat") == "ghp_rotated"

        # Delete
        r = client.delete("/admin/api/secrets/gh_pat", headers=headers)
        assert r.status_code == 200
        assert r.json() == {"deleted": True, "name": "gh_pat"}
        assert not secrets.exists("gh_pat")

        # 404 on delete of missing
        r = client.delete("/admin/api/secrets/gh_pat", headers=headers)
        assert r.status_code == 404


def test_secrets_api_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = _build_app_with_secrets(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.get("/admin/api/secrets")
        assert r.status_code == 401
        r = client.post("/admin/api/secrets", json={"name": "x", "value": "y"})
        assert r.status_code == 401


def test_secrets_api_rejects_bad_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = _build_app_with_secrets(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer admin-test-token"}
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/secrets",
            headers=headers,
            json={"name": "bad space", "value": "v"},
        )
        assert r.status_code == 400


def test_runtime_apply_blocks_missing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _build_app_with_secrets(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer admin-test-token"}
    candidate = {
        "auth": {"token_env": "MCP_PROXY_TOKEN"},
        "admin": {"mount_name": "__admin__", "enabled": True, "require_token": True, "allowed_clients": []},
        "telemetry": {"enabled": True, "sink": "noop"},
        "upstreams": {
            "linear": {
                "type": "http",
                "url": "https://api.linear.app/mcp",
                "headers": {"Authorization": "Bearer ${secret:linear_api_key}"},
            }
        },
    }
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/config",
            headers=headers,
            json={"config": candidate},
        )
        # Runtime applier blocks the swap and reports the missing secret
        # by name; HTTP status is 200 because the admin API wraps the
        # apply result as JSON.
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["applied"] is False
        assert "linear_api_key" in result["error"]


def test_runtime_apply_succeeds_once_secret_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, secrets = _build_app_with_secrets(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer admin-test-token"}
    asyncio.get_event_loop().run_until_complete(
        secrets.set("linear_api_key", "lin_live_xxx")
    ) if False else None  # placeholder to avoid mypy complaining
    # Use a synchronous path: the TestClient lifespan runs in its own loop,
    # so we just POST a secret via the admin API first, then apply.
    candidate = {
        "auth": {"token_env": "MCP_PROXY_TOKEN"},
        "admin": {"mount_name": "__admin__", "enabled": True, "require_token": True, "allowed_clients": []},
        "telemetry": {"enabled": True, "sink": "noop"},
        "upstreams": {
            "linear": {
                "type": "http",
                "url": "https://api.linear.app/mcp",
                "headers": {"Authorization": "Bearer ${secret:linear_api_key}"},
            }
        },
    }
    with TestClient(app) as client:
        r = client.post(
            "/admin/api/secrets",
            headers=headers,
            json={"name": "linear_api_key", "value": "lin_live_xxx"},
        )
        assert r.status_code == 200

        r = client.post("/admin/api/config", headers=headers, json={"config": candidate})
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["applied"] is True, result

        # The Config page listing should now show the linear secret as
        # referenced (not orphan) and no missing entries.
        r = client.get("/admin/api/secrets", headers=headers)
        listing = r.json()
        assert "linear_api_key" in listing["referenced"]
        assert listing["missing"] == []
        assert listing["orphans"] == []
