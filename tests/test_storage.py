"""Tests for the SQLAlchemy-backed ConfigStore + bootstrap path.

These exercise the storage layer in isolation (no FastAPI, no
RuntimeConfigManager), plus the CLI's first-run bootstrap that imports
a JSON seed file into a fresh DB and renames it to ``.migrated``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from mcpxy_proxy.storage.config_store import (
    ConfigStore,
    SecretStoreError,
    open_store,
)
from mcpxy_proxy.storage.db import (
    DEFAULT_SQLITE_FILENAME,
    build_engine,
    known_table_names,
    resolve_database_url,
    run_migrations,
)
from mcpxy_proxy.storage.schema import CURRENT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def test_resolve_database_url_uses_explicit_arg() -> None:
    assert resolve_database_url("sqlite:////tmp/explicit.db") == "sqlite:////tmp/explicit.db"


def test_resolve_database_url_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPXY_DB_URL", "sqlite:////tmp/from-env.db")
    assert resolve_database_url(None) == "sqlite:////tmp/from-env.db"


def test_resolve_database_url_default_uses_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MCPXY_DB_URL", raising=False)
    monkeypatch.setenv("MCPXY_STATE_DIR", str(tmp_path))
    url = resolve_database_url(None)
    assert url.endswith(f"{tmp_path / DEFAULT_SQLITE_FILENAME}")


# ---------------------------------------------------------------------------
# Migrations create the expected tables and stamp the schema_meta row
# ---------------------------------------------------------------------------


def test_migrations_create_all_tables(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'mcpxy.db'}")
    run_migrations(engine)
    tables = set(known_table_names(engine))
    assert {"schema_meta", "config_kv", "config_history", "upstreams", "secrets"} <= tables


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'mcpxy.db'}")
    run_migrations(engine)
    run_migrations(engine)  # second call must not raise
    run_migrations(engine)


def test_open_store_warms_caches(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    store = open_store(f"sqlite:///{tmp_path / 'mcpxy.db'}", fernet=fernet)
    assert store.is_empty()
    assert store.active_version() == 0
    assert list(store.known_secret_names()) == []
    store.close()


# ---------------------------------------------------------------------------
# Active config + history
# ---------------------------------------------------------------------------


def test_save_active_config_round_trips(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    store = open_store(f"sqlite:///{tmp_path / 'mcpxy.db'}", fernet=fernet)
    payload = {
        "default_upstream": "fs",
        "auth": {"token_env": "MCP_PROXY_TOKEN"},
        "admin": {"mount_name": "__admin__", "enabled": True, "require_token": True, "allowed_clients": []},
        "telemetry": {"enabled": True, "sink": "noop"},
        "upstreams": {
            "fs": {"type": "stdio", "command": "node", "args": ["server.js"]}
        },
    }
    version = store.save_active_config(payload, source="test")
    assert version == 1
    assert store.active_version() == 1

    again = store.get_active_config()
    assert again == payload
    assert not store.is_empty()
    store.close()


def test_save_active_config_history_grows(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    store = open_store(f"sqlite:///{tmp_path / 'mcpxy.db'}", fernet=fernet)
    for i in range(3):
        store.save_active_config({"upstreams": {f"u{i}": {"type": "http", "url": "x"}}}, source=f"test#{i}")
    assert store.active_version() == 3
    history = store.list_config_history()
    assert [h["version"] for h in history] == [3, 2, 1]
    assert [h["source"] for h in history] == ["test#2", "test#1", "test#0"]
    payload2 = store.load_history_payload(2)
    assert payload2 == {"upstreams": {"u1": {"type": "http", "url": "x"}}}
    store.close()


def test_save_active_config_resyncs_upstreams_table(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    store = open_store(f"sqlite:///{tmp_path / 'mcpxy.db'}", fernet=fernet)
    store.save_active_config(
        {
            "upstreams": {
                "a": {"type": "http", "url": "http://a"},
                "b": {"type": "stdio", "command": "node", "args": ["b.js"]},
            }
        },
        source="t1",
    )
    listed = store.list_upstreams()
    assert sorted(u.name for u in listed) == ["a", "b"]

    store.save_active_config({"upstreams": {"c": {"type": "http", "url": "http://c"}}}, source="t2")
    listed = store.list_upstreams()
    assert [u.name for u in listed] == ["c"]
    store.close()


# ---------------------------------------------------------------------------
# Secrets through ConfigStore
# ---------------------------------------------------------------------------


def test_upsert_get_delete_secret(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    store = open_store(f"sqlite:///{tmp_path / 'mcpxy.db'}", fernet=fernet)
    store.upsert_secret("github_token", "ghp_abc", description="dev")
    assert store.get_secret("github_token") == "ghp_abc"
    assert store.secret_exists("github_token")
    assert "github_token" in list(store.known_secret_names())

    public = store.list_public_secrets()
    assert len(public) == 1
    assert public[0]["name"] == "github_token"
    assert "value" not in public[0]

    assert store.delete_secret("github_token") is True
    assert not store.secret_exists("github_token")
    assert store.delete_secret("github_token") is False
    store.close()


def test_internal_secrets_hidden_from_public_listing(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    store = open_store(f"sqlite:///{tmp_path / 'mcpxy.db'}", fernet=fernet)
    store.upsert_secret("user_token", "real")
    store.upsert_secret("__oauth_token__notion", "internal")
    public = store.list_public_secrets()
    assert [p["name"] for p in public] == ["user_token"]
    # But the internal name is still resolvable internally.
    assert store.get_secret("__oauth_token__notion") == "internal"
    store.close()


def test_secret_validation_rejects_bad_names(tmp_path: Path) -> None:
    fernet = Fernet(Fernet.generate_key())
    store = open_store(f"sqlite:///{tmp_path / 'mcpxy.db'}", fernet=fernet)
    for bad in ["", "with space", "has/slash", "dots.in.name", "-leading-dash"]:
        with pytest.raises(SecretStoreError):
            store.upsert_secret(bad, "v")
    store.close()


def test_persistence_across_store_reopen(tmp_path: Path) -> None:
    key = Fernet.generate_key()
    db_url = f"sqlite:///{tmp_path / 'mcpxy.db'}"
    s1 = open_store(db_url, fernet=Fernet(key))
    s1.save_active_config({"upstreams": {"a": {"type": "http", "url": "x"}}}, source="t")
    s1.upsert_secret("k", "v", description="d")
    s1.close()

    s2 = open_store(db_url, fernet=Fernet(key))
    assert s2.get_active_config() == {"upstreams": {"a": {"type": "http", "url": "x"}}}
    assert s2.active_version() == 1
    assert s2.get_secret("k") == "v"
    s2.close()


# ---------------------------------------------------------------------------
# Bootstrap auto-migration via cli.build_state
# ---------------------------------------------------------------------------


def test_bootstrap_imports_seed_file_and_renames_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-run scenario: empty DB + a seed config file. ``build_state``
    must import the file, write history, and rename the file to
    ``.migrated``."""
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MCPXY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MCPXY_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("MCP_PROXY_TOKEN", "tok")

    seed = tmp_path / "config.json"
    seed.write_text(
        json.dumps(
            {
                "auth": {"token_env": "MCP_PROXY_TOKEN"},
                "admin": {
                    "mount_name": "__admin__",
                    "enabled": True,
                    "require_token": True,
                    "allowed_clients": [],
                },
                "telemetry": {"enabled": True, "sink": "noop"},
                "upstreams": {
                    "seed_up": {"type": "http", "url": "http://seed.example/mcp"}
                },
            }
        )
    )

    from mcpxy_proxy.cli import build_state

    state = build_state(str(seed))
    try:
        assert state.bootstrap_source == f"seed:{seed}"
        # Original file is gone, replaced by .migrated.
        assert not seed.exists()
        assert (tmp_path / "config.json.migrated").exists()
        # The DB has the imported config at version 1.
        assert state.config_store.active_version() == 1
        assert "seed_up" in state.config.upstreams
    finally:
        state.config_store.close()


def test_bootstrap_uses_db_when_already_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MCPXY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MCPXY_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("MCP_PROXY_TOKEN", "tok")

    # Pre-populate the DB directly so build_state sees a non-empty store.
    state_dir.mkdir(parents=True)
    fernet_key = os.environ["MCPXY_SECRETS_KEY"].encode("ascii")
    pre = open_store(f"sqlite:///{state_dir / 'mcpxy.db'}", fernet=Fernet(fernet_key))
    pre.save_active_config(
        {
            "auth": {"token_env": "MCP_PROXY_TOKEN"},
            "admin": {
                "mount_name": "__admin__",
                "enabled": True,
                "require_token": True,
                "allowed_clients": [],
            },
            "telemetry": {"enabled": True, "sink": "noop"},
            "upstreams": {"prepop": {"type": "http", "url": "http://pre"}},
        },
        source="pre",
    )
    pre.close()

    from mcpxy_proxy.cli import build_state

    state = build_state(None)
    try:
        assert state.bootstrap_source == "db"
        assert "prepop" in state.config.upstreams
        assert state.config_store.active_version() == 1
    finally:
        state.config_store.close()


def test_bootstrap_writes_default_when_db_empty_and_no_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MCPXY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MCPXY_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("MCP_PROXY_TOKEN", "tok")

    from mcpxy_proxy.cli import build_state

    state = build_state(None)
    try:
        assert state.bootstrap_source == "default"
        assert state.config_store.active_version() == 1
        assert state.config.upstreams == {}
    finally:
        state.config_store.close()


def test_bootstrap_seeds_onboarding_when_seed_config_has_no_resolvable_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh deployment with a seed config but no resolvable admin token.

    Regression test for the Docker-deploy footgun: ``deploy/docker/config.json``
    ships with ``auth.token_env = MCP_PROXY_TOKEN`` and ``require_token =
    True`` but operators routinely forget to set the env var on first
    ``docker compose up``. Before this fix the bootstrap only seeded the
    onboarding row when ``source_label == "default"``, so the
    seed-config path came up with no row, the frontend routed to
    LoginGate, and the fail-closed middleware at ``server.py`` refused
    every admin API call with 503 ``admin_token_not_configured`` —
    leaving the operator stuck on a token prompt they couldn't satisfy.

    The fix is to seed the onboarding row whenever the resolved config
    has no bearer token, regardless of how the config was bootstrapped.
    """
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MCPXY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MCPXY_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    # Deliberately NOT setting MCP_PROXY_TOKEN — this is the whole point
    # of the regression test.
    monkeypatch.delenv("MCP_PROXY_TOKEN", raising=False)

    seed = tmp_path / "config.json"
    seed.write_text(
        json.dumps(
            {
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
        )
    )

    from mcpxy_proxy.cli import build_state

    state = build_state(str(seed))
    try:
        # The seed path was taken, not the default bootstrap.
        assert state.bootstrap_source == f"seed:{seed}"
        # But because the resolved config has no admin token, the
        # onboarding row must still be seeded so the wizard is
        # reachable on the next request.
        obstate = state.config_store.get_onboarding_state()
        assert obstate is not None, (
            "onboarding row should be auto-seeded when the bootstrapped "
            "config has no resolvable admin token"
        )
        assert obstate.admin_token_set_at is None
        assert obstate.completed_at is None
    finally:
        state.config_store.close()


def test_bootstrap_does_not_seed_onboarding_when_token_is_resolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counterpart to the no-token test: when the seed config DOES have
    a resolvable bearer (env var is set), the onboarding row must not
    be created. Operators who already have a token shouldn't get the
    wizard hijacking their dashboard on boot.
    """
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MCPXY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MCPXY_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("MCP_PROXY_TOKEN", "real-token-value")

    seed = tmp_path / "config.json"
    seed.write_text(
        json.dumps(
            {
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
        )
    )

    from mcpxy_proxy.cli import build_state

    state = build_state(str(seed))
    try:
        assert state.bootstrap_source == f"seed:{seed}"
        assert state.config_store.get_onboarding_state() is None
    finally:
        state.config_store.close()


def test_bootstrap_seeds_onboarding_when_token_env_is_empty_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live failure mode this fix targets: ``MCP_PROXY_TOKEN=""``.

    ``docker-compose.yml`` expands ``${MCP_PROXY_TOKEN:-}`` to the empty
    string when the operator hasn't populated ``.env``, so the container
    env has ``MCP_PROXY_TOKEN=""`` (set but empty). A naive ``os.getenv``
    returns ``""`` for that case, not ``None``, and the original
    ``is None`` check in ``build_state`` let the onboarding row
    creation slip through. Verifies the fix: ``resolve_admin_token``
    treats empty env vars as unset, so bootstrap still seeds the row.
    """
    state_dir = tmp_path / "state"
    monkeypatch.setenv("MCPXY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MCPXY_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    # The whole point: env var is present but empty.
    monkeypatch.setenv("MCP_PROXY_TOKEN", "")

    seed = tmp_path / "config.json"
    seed.write_text(
        json.dumps(
            {
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
        )
    )

    from mcpxy_proxy.cli import build_state

    state = build_state(str(seed))
    try:
        assert state.bootstrap_source == f"seed:{seed}"
        obstate = state.config_store.get_onboarding_state()
        assert obstate is not None, (
            "onboarding row must be auto-seeded when MCP_PROXY_TOKEN is set "
            "to the empty string (the default Docker Compose behaviour "
            "when .env doesn't define it)"
        )
        assert obstate.admin_token_set_at is None
        assert obstate.completed_at is None
    finally:
        state.config_store.close()
