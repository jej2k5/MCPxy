"""Database engine + URL resolution + migration runner.

This module is the one place that knows about SQLAlchemy engines and URLs.
Everything downstream (``ConfigStore``, tests, CLI) takes an ``Engine``
that's already been opened and migrated, and never touches the driver
or URL directly.

URL resolution order, highest precedence first:

1. Explicit ``url`` argument to :func:`resolve_database_url`.
2. ``MCPY_DB_URL`` environment variable.
3. ``sqlite:///<state_dir>/mcpy.db`` — the out-of-the-box default.
   ``<state_dir>`` follows the same rules as the secrets store:
   ``MCPY_STATE_DIR`` env var → ``/var/lib/mcpy`` → ``~/.local/state/mcpy``
   → ``/tmp/mcpy-state`` fallback.

We deliberately use **synchronous** SQLAlchemy here. The proxy's hot
path (``${secret:NAME}`` expansion, config reads during request handling)
goes through an in-memory cache populated once at startup, so DB latency
only matters for admin-API writes — and those happen at human frequency,
not request frequency. Sync code drops a class of event-loop-binding
bugs that would otherwise plague the CLI bootstrap → FastAPI lifespan
handover, keeps tests simple, and works equally well with sqlite,
postgres, mysql, etc. — operators just install the matching driver
(``psycopg2-binary``, ``PyMySQL``, …) and point ``MCPY_DB_URL`` at it.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

from sqlalchemy import Engine, create_engine, insert, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from mcp_proxy.storage.schema import (
    CURRENT_SCHEMA_VERSION,
    METADATA,
    schema_meta_table,
)

logger = logging.getLogger(__name__)


DEFAULT_SQLITE_FILENAME = "mcpy.db"


class DatabaseError(RuntimeError):
    """Raised when the database is unreachable, unreadable, or migration fails."""


# Duplicated from mcp_proxy.secrets to avoid an import cycle at module load
# time; the two constants mean the same thing.
_DEFAULT_STATE_DIR_CANDIDATES: tuple[Path, ...] = (
    Path("/var/lib/mcpy"),
    Path.home() / ".local" / "state" / "mcpy",
)


def _default_state_dir() -> Path:
    """Pick the most appropriate default for runtime state.

    Kept as a private helper (rather than reusing the secrets-module
    version) so this module has zero imports from other MCPy modules at
    load time. The two paths are identical in practice.
    """
    override = os.getenv("MCPY_STATE_DIR")
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    for candidate in _DEFAULT_STATE_DIR_CANDIDATES:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.touch()
            probe.unlink()
            return candidate
        except OSError:
            continue
    fallback = Path("/tmp/mcpy-state")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def resolve_database_url(url: str | None = None) -> str:
    """Resolve the effective database URL, applying defaults.

    >>> resolve_database_url("sqlite:///foo.db")
    'sqlite:///foo.db'
    >>> import os; os.environ.pop("MCPY_DB_URL", None)
    >>> resolve_database_url(None).startswith("sqlite:///")
    True
    """
    if url is None:
        url = os.getenv("MCPY_DB_URL")
    if url is None:
        state_dir = _default_state_dir()
        url = f"sqlite:///{state_dir / DEFAULT_SQLITE_FILENAME}"
    return url


def build_engine(
    url: str | None = None,
    *,
    echo: bool = False,
    pool_pre_ping: bool = True,
) -> Engine:
    """Create a synchronous ``Engine`` for the resolved URL.

    SQLite URLs are built with ``connect_args={"check_same_thread": False}``
    so the shared connection can be used from FastAPI's threadpool.
    Non-SQLite URLs use SQLAlchemy's normal connection pool.
    """
    effective = resolve_database_url(url)
    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": pool_pre_ping}
    if effective.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    try:
        engine = create_engine(effective, **kwargs)
    except Exception as exc:
        raise DatabaseError(
            f"failed to create engine for {effective!r}: {exc}"
        ) from exc
    logger.debug("storage: opened engine for %s", effective)
    return engine


def run_migrations(engine: Engine) -> None:
    """Ensure the schema exists and is at the current version.

    For now the migration strategy is "create if missing": we call
    ``metadata.create_all`` and then check the ``schema_meta`` row.
    Once the schema evolves past v1 this function will grow real step
    functions keyed by the stored version.
    """
    try:
        with engine.begin() as conn:
            METADATA.create_all(conn)
            existing = conn.execute(select(schema_meta_table.c.version))
            row = existing.first()
            if row is None:
                conn.execute(
                    insert(schema_meta_table).values(version=CURRENT_SCHEMA_VERSION)
                )
                logger.info(
                    "storage: initialised schema at version %d", CURRENT_SCHEMA_VERSION
                )
            elif int(row[0]) < CURRENT_SCHEMA_VERSION:
                # Future migration steps plug in here.
                raise DatabaseError(
                    f"database schema version {row[0]} is older than "
                    f"{CURRENT_SCHEMA_VERSION}; no upgrade path is implemented"
                )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"schema migration failed: {exc}") from exc


def known_table_names(engine: Engine) -> list[str]:
    """Return every table the DB knows about. Used by diagnostics tests."""
    with engine.connect() as conn:
        return list(inspect(conn).get_table_names())


__all__ = [
    "DEFAULT_SQLITE_FILENAME",
    "DatabaseError",
    "Engine",
    "_default_state_dir",
    "build_engine",
    "known_table_names",
    "resolve_database_url",
    "run_migrations",
]
