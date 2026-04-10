"""Verify schema v2 tables are created correctly on a fresh DB."""

from __future__ import annotations

from cryptography.fernet import Fernet

from mcpxy_proxy.storage.config_store import open_store
from mcpxy_proxy.storage.db import known_table_names
from mcpxy_proxy.storage.schema import CURRENT_SCHEMA_VERSION


def test_schema_version_is_2():
    assert CURRENT_SCHEMA_VERSION == 2


def test_fresh_db_has_all_v2_tables(tmp_path):
    fernet = Fernet(Fernet.generate_key())
    store = open_store(
        f"sqlite:///{tmp_path / 'test.db'}",
        fernet=fernet,
        state_dir=str(tmp_path),
    )
    tables = set(known_table_names(store.engine))
    expected = {
        "schema_meta",
        "config_kv",
        "config_history",
        "upstreams",
        "secrets",
        "onboarding",
        "users",
        "user_invites",
        "personal_access_tokens",
        "revoked_jwt_ids",
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"
    store.close()
