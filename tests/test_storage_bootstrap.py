"""Tests for the pre-engine bootstrap file and the extended URL resolver.

These cover the three things the onboarding wizard depends on:

- ``BootstrapConfig`` round-trip + malformed-file rejection.
- ``resolve_database_url`` precedence: explicit arg > env > bootstrap
  file > default.
- ``available_dialects`` / ``sanitize_url`` / ``probe_connection`` /
  ``_assemble_url_from_parts`` — the helpers the onboarding handlers
  call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mcp_proxy.storage.bootstrap import (
    BOOTSTRAP_FILENAME,
    BootstrapConfig,
    BootstrapError,
    clear_bootstrap,
    load_bootstrap,
    write_bootstrap,
)
from mcp_proxy.storage.db import (
    DEFAULT_SQLITE_FILENAME,
    DatabaseError,
    _assemble_url_from_parts,
    available_dialects,
    dialect_of,
    probe_connection,
    resolve_database_url,
    sanitize_url,
)


# ---------------------------------------------------------------------------
# BootstrapConfig + file I/O
# ---------------------------------------------------------------------------


def test_load_bootstrap_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_bootstrap(tmp_path) is None


def test_write_then_load_bootstrap_roundtrip(tmp_path: Path) -> None:
    cfg = BootstrapConfig(db_url="postgresql://u:p@h/db", written_by="127.0.0.1")
    path = write_bootstrap(tmp_path, cfg)
    assert path == tmp_path / BOOTSTRAP_FILENAME
    loaded = load_bootstrap(tmp_path)
    assert loaded is not None
    assert loaded.db_url == "postgresql://u:p@h/db"
    assert loaded.written_by == "127.0.0.1"
    assert loaded.written_at is not None


def test_write_bootstrap_sets_0600_perms(tmp_path: Path) -> None:
    path = write_bootstrap(tmp_path, BootstrapConfig(db_url="sqlite:///x.db"))
    mode = os.stat(path).st_mode & 0o777
    # On platforms that don't honour chmod (rare: some Windows FUSE
    # mounts) we accept anything at-or-tighter than 0o600.
    assert mode & 0o077 == 0, f"bootstrap.json should not be world/group readable: {oct(mode)}"


def test_load_bootstrap_rejects_malformed_json(tmp_path: Path) -> None:
    (tmp_path / BOOTSTRAP_FILENAME).write_text("not json", encoding="utf-8")
    with pytest.raises(BootstrapError):
        load_bootstrap(tmp_path)


def test_load_bootstrap_rejects_non_object(tmp_path: Path) -> None:
    (tmp_path / BOOTSTRAP_FILENAME).write_text("[]", encoding="utf-8")
    with pytest.raises(BootstrapError):
        load_bootstrap(tmp_path)


def test_load_bootstrap_rejects_non_string_db_url(tmp_path: Path) -> None:
    (tmp_path / BOOTSTRAP_FILENAME).write_text(
        json.dumps({"db_url": 42}), encoding="utf-8"
    )
    with pytest.raises(BootstrapError):
        load_bootstrap(tmp_path)


def test_clear_bootstrap_removes_file(tmp_path: Path) -> None:
    write_bootstrap(tmp_path, BootstrapConfig(db_url="sqlite:///x.db"))
    assert (tmp_path / BOOTSTRAP_FILENAME).exists()
    assert clear_bootstrap(tmp_path) is True
    assert not (tmp_path / BOOTSTRAP_FILENAME).exists()
    assert clear_bootstrap(tmp_path) is False  # idempotent


# ---------------------------------------------------------------------------
# resolve_database_url precedence matrix
# ---------------------------------------------------------------------------


def test_resolve_url_explicit_arg_wins_over_env_and_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCPY_DB_URL", "sqlite:////from-env.db")
    write_bootstrap(tmp_path, BootstrapConfig(db_url="sqlite:////from-bootstrap.db"))
    assert (
        resolve_database_url("sqlite:////explicit.db", state_dir=tmp_path)
        == "sqlite:////explicit.db"
    )


def test_resolve_url_env_wins_over_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCPY_DB_URL", "sqlite:////from-env.db")
    write_bootstrap(tmp_path, BootstrapConfig(db_url="sqlite:////from-bootstrap.db"))
    assert resolve_database_url(None, state_dir=tmp_path) == "sqlite:////from-env.db"


def test_resolve_url_bootstrap_wins_over_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCPY_DB_URL", raising=False)
    write_bootstrap(tmp_path, BootstrapConfig(db_url="sqlite:////from-bootstrap.db"))
    assert (
        resolve_database_url(None, state_dir=tmp_path)
        == "sqlite:////from-bootstrap.db"
    )


def test_resolve_url_falls_through_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCPY_DB_URL", raising=False)
    url = resolve_database_url(None, state_dir=tmp_path)
    assert url.endswith(f"{tmp_path / DEFAULT_SQLITE_FILENAME}")


def test_resolve_url_empty_env_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker Compose expands ``${MCPY_DB_URL:-}`` to empty strings."""
    monkeypatch.setenv("MCPY_DB_URL", "")
    write_bootstrap(tmp_path, BootstrapConfig(db_url="sqlite:////from-bootstrap.db"))
    assert (
        resolve_database_url(None, state_dir=tmp_path)
        == "sqlite:////from-bootstrap.db"
    )


def test_resolve_url_propagates_bootstrap_parse_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCPY_DB_URL", raising=False)
    (tmp_path / BOOTSTRAP_FILENAME).write_text("{not-json", encoding="utf-8")
    with pytest.raises(BootstrapError):
        resolve_database_url(None, state_dir=tmp_path)


# ---------------------------------------------------------------------------
# sanitize_url / dialect_of
# ---------------------------------------------------------------------------


def test_sanitize_url_masks_password() -> None:
    out = sanitize_url("postgresql://alice:hunter2@host:5432/mcpy")
    assert "hunter2" not in out
    assert "alice" in out
    assert "host" in out
    assert "mcpy" in out


def test_sanitize_url_leaves_passwordless_urls_alone() -> None:
    out = sanitize_url("sqlite:///var/lib/mcpy/mcpy.db")
    assert out == "sqlite:///var/lib/mcpy/mcpy.db"


def test_sanitize_url_handles_garbage() -> None:
    assert sanitize_url("not a url at all") == "<unparseable-url>"


def test_dialect_of_recognises_variants() -> None:
    assert dialect_of("sqlite:///x.db") == "sqlite"
    assert dialect_of("postgresql://h/db") == "postgresql"
    assert dialect_of("postgresql+psycopg2://h/db") == "postgresql"
    assert dialect_of("mysql+pymysql://h/db") == "mysql"
    assert dialect_of("mariadb://h/db") == "mysql"


# ---------------------------------------------------------------------------
# available_dialects / probe_connection
# ---------------------------------------------------------------------------


def test_available_dialects_always_includes_sqlite() -> None:
    assert "sqlite" in available_dialects()


def test_probe_connection_sqlite_file_ok(tmp_path: Path) -> None:
    # A file URL that doesn't yet exist is fine — SQLAlchemy will
    # create it on the first connection.
    dialect = probe_connection(f"sqlite:///{tmp_path / 'probe.db'}")
    assert dialect == "sqlite"


def test_probe_connection_rejects_in_memory() -> None:
    with pytest.raises(DatabaseError, match=":memory:"):
        probe_connection("sqlite:///:memory:")
    with pytest.raises(DatabaseError, match=":memory:"):
        probe_connection("sqlite://")


def test_probe_connection_surfaces_parse_errors() -> None:
    with pytest.raises(DatabaseError):
        probe_connection("not-a-url")


def test_probe_connection_surfaces_missing_driver() -> None:
    if "postgresql" in available_dialects():
        pytest.skip("psycopg2 is installed in this environment")
    with pytest.raises(DatabaseError, match="driver is not installed"):
        probe_connection("postgresql://localhost/foo")


# ---------------------------------------------------------------------------
# _assemble_url_from_parts
# ---------------------------------------------------------------------------


def test_assemble_url_rejects_unknown_dialect() -> None:
    with pytest.raises(DatabaseError):
        _assemble_url_from_parts(
            dialect="oracle",
            host="h",
            port=None,
            database="db",
            user="u",
            password="p",
        )


def test_assemble_url_escapes_special_chars_in_password() -> None:
    if "postgresql" not in available_dialects():
        pytest.skip("postgresql driver not installed")
    url = _assemble_url_from_parts(
        dialect="postgresql",
        host="h.example",
        port=5432,
        database="mcpy",
        user="svc",
        password="p@ss:w/rd",
    )
    # Password must be percent-escaped so the URL parses back cleanly.
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    assert parsed.password == "p@ss:w/rd"
    assert parsed.host == "h.example"
    assert parsed.database == "mcpy"
    assert parsed.port == 5432


def test_assemble_url_attaches_query_args() -> None:
    if "postgresql" not in available_dialects():
        pytest.skip("postgresql driver not installed")
    url = _assemble_url_from_parts(
        dialect="postgresql",
        host="h",
        port=5432,
        database="mcpy",
        user="u",
        password="p",
        query={"sslmode": "require"},
    )
    assert "sslmode=require" in url
