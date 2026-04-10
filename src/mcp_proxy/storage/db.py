"""Database engine + URL resolution + migration runner.

This module is the one place that knows about SQLAlchemy engines and URLs.
Everything downstream (``ConfigStore``, tests, CLI) takes an ``Engine``
that's already been opened and migrated, and never touches the driver
or URL directly.

URL resolution order, highest precedence first:

1. Explicit ``url`` argument to :func:`resolve_database_url`.
2. ``MCPY_DB_URL`` environment variable.
3. ``<state_dir>/bootstrap.json`` — written by the onboarding wizard
   when the operator picks a non-default DB via the UI. See
   :mod:`mcp_proxy.storage.bootstrap` for the file format.
4. ``sqlite:///<state_dir>/mcpy.db`` — the out-of-the-box default.
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
postgres, mysql, etc. — operators install the matching driver extras
(``pip install mcpy-proxy[postgres]`` or ``[mysql]``) and point either
``MCPY_DB_URL`` or the onboarding wizard at it.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Iterable

from sqlalchemy import Engine, create_engine, insert, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from mcp_proxy.storage.bootstrap import BootstrapError, load_bootstrap
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


def resolve_database_url(
    url: str | None = None,
    *,
    state_dir: Path | str | None = None,
) -> str:
    """Resolve the effective database URL, applying defaults.

    Precedence (highest to lowest):

    1. Explicit ``url`` argument.
    2. ``MCPY_DB_URL`` environment variable.
    3. ``<state_dir>/bootstrap.json`` ``db_url`` — written by the
       onboarding wizard when the operator picks a non-default backend
       via the UI.
    4. ``sqlite:///<state_dir>/mcpy.db`` default.

    A malformed bootstrap file raises :class:`BootstrapError` rather
    than silently falling through to the SQLite default, so operators
    don't lose a Postgres URL they intentionally wrote.

    >>> resolve_database_url("sqlite:///foo.db")
    'sqlite:///foo.db'
    >>> import os; os.environ.pop("MCPY_DB_URL", None)
    >>> resolve_database_url(None).startswith("sqlite:///")
    True
    """
    if url is not None:
        return url
    env_url = os.getenv("MCPY_DB_URL")
    if env_url:
        return env_url
    resolved_state_dir = (
        Path(state_dir) if state_dir is not None else _default_state_dir()
    )
    try:
        bootstrap = load_bootstrap(resolved_state_dir)
    except BootstrapError:
        # Let the caller surface the error. We don't want to silently
        # ignore a corrupted file and downgrade to the SQLite default.
        raise
    if bootstrap is not None and bootstrap.db_url:
        return bootstrap.db_url
    return f"sqlite:///{resolved_state_dir / DEFAULT_SQLITE_FILENAME}"


def build_engine(
    url: str | None = None,
    *,
    echo: bool = False,
    pool_pre_ping: bool = True,
    state_dir: Path | str | None = None,
) -> Engine:
    """Create a synchronous ``Engine`` for the resolved URL.

    SQLite URLs are built with ``connect_args={"check_same_thread": False}``
    so the shared connection can be used from FastAPI's threadpool.
    Non-SQLite URLs use SQLAlchemy's normal connection pool.

    ``state_dir`` is forwarded to :func:`resolve_database_url` so the
    bootstrap file lookup uses the same directory the caller already
    picked, rather than probing defaults a second time.
    """
    effective = resolve_database_url(url, state_dir=state_dir)
    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": pool_pre_ping}
    if effective.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    try:
        engine = create_engine(effective, **kwargs)
    except Exception as exc:
        raise DatabaseError(
            f"failed to create engine for {sanitize_url(effective)!r}: {exc}"
        ) from exc
    logger.debug("storage: opened engine for %s", sanitize_url(effective))
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
                stored = int(row[0])
                if stored == 1:
                    # v1 -> v2: adds users, user_invites,
                    # personal_access_tokens, revoked_jwt_ids tables and
                    # the bootstrap_admin_email column on onboarding.
                    # create_all() already ran above, which handles the
                    # new tables. We just stamp the new version.
                    from sqlalchemy import update as sa_update

                    from mcp_proxy.storage.schema import onboarding_table

                    # Add the bootstrap_admin_email column if missing
                    # (create_all won't add columns to existing tables).
                    inspector = inspect(conn)
                    existing_cols = {
                        c["name"] for c in inspector.get_columns("onboarding")
                    }
                    if "bootstrap_admin_email" not in existing_cols:
                        conn.execute(
                            text(
                                "ALTER TABLE onboarding "
                                "ADD COLUMN bootstrap_admin_email VARCHAR(254)"
                            )
                        )

                    conn.execute(
                        sa_update(schema_meta_table)
                        .where(schema_meta_table.c.version == stored)
                        .values(version=CURRENT_SCHEMA_VERSION)
                    )
                    logger.info(
                        "storage: migrated schema v%d -> v%d",
                        stored,
                        CURRENT_SCHEMA_VERSION,
                    )
                else:
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


# ---------------------------------------------------------------------------
# URL helpers used by the onboarding wizard
# ---------------------------------------------------------------------------


# Dialect → list of driver module names in preference order. The first
# importable module wins, so ``psycopg`` (v3) is preferred over the older
# ``psycopg2``/``psycopg2cffi`` but we accept any of them.
_DIALECT_DRIVER_MODULES: dict[str, tuple[str, ...]] = {
    "sqlite": ("sqlite3",),
    "postgresql": ("psycopg", "psycopg2", "psycopg2cffi"),
    "mysql": ("pymysql", "mysqlclient", "MySQLdb"),
}


# Canonical dialect key when SQLAlchemy reports a driver-specific variant
# like ``postgresql+psycopg2``. Only the top-level dialect matters for
# UI display and driver availability.
_DIALECT_ALIASES: dict[str, str] = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlite": "sqlite",
}


def _canonical_dialect(name: str) -> str:
    return _DIALECT_ALIASES.get(name.split("+", 1)[0].lower(), name.lower())


def dialect_of(url: str) -> str:
    """Return the canonical dialect (``sqlite``/``postgresql``/``mysql``/…).

    Returns the lowercased dialect name (before the ``+driver``
    fragment) for any URL SQLAlchemy can parse. Unknown dialects are
    returned as-is so the UI can still display them.
    """
    try:
        parsed = make_url(url)
    except ArgumentError:
        return "unknown"
    return _canonical_dialect(parsed.get_backend_name())


def sanitize_url(url: str) -> str:
    """Return ``url`` with the password replaced by ``***``.

    Used anywhere we log or return a URL to the frontend — we never
    want a password to leak through a status endpoint or a log line.
    For URLs that don't parse (rare: SQLAlchemy accepts almost
    everything) we fall back to a generic placeholder rather than
    risk returning the raw value.
    """
    try:
        parsed = make_url(url)
    except ArgumentError:
        return "<unparseable-url>"
    if parsed.password is None:
        return parsed.render_as_string(hide_password=False)
    return parsed.render_as_string(hide_password=True)


def available_dialects() -> list[str]:
    """Return the list of dialects whose driver imports cleanly.

    ``sqlite`` is always present (stdlib). ``postgresql`` and ``mysql``
    depend on the operator having installed the optional extras
    (``pip install mcpy-proxy[postgres]`` or ``[mysql]``). The wizard
    hides/disables UI options for missing drivers and the API layer
    also refuses to probe or swap to a dialect that isn't importable
    here, so the frontend and backend can't disagree about what's
    possible.
    """
    out: list[str] = []
    for dialect, candidates in _DIALECT_DRIVER_MODULES.items():
        for mod_name in candidates:
            try:
                if importlib.util.find_spec(mod_name) is not None:
                    out.append(dialect)
                    break
            except (ImportError, ValueError):
                # ValueError can happen for broken installs whose
                # ``__spec__`` is in an inconsistent state; treat as
                # "not available" and keep probing.
                continue
    return out


def _assemble_url_from_parts(
    *,
    dialect: str,
    host: str | None,
    port: int | None,
    database: str | None,
    user: str | None,
    password: str | None,
    query: dict[str, str] | None = None,
) -> str:
    """Build a URL from structured form fields using SQLAlchemy's own
    constructor, which URL-escapes values correctly and rejects typos.
    """
    canonical = _canonical_dialect(dialect)
    if canonical not in _DIALECT_DRIVER_MODULES:
        raise DatabaseError(f"unsupported database dialect {dialect!r}")
    # Pick the first importable driver for the dialect so the URL
    # SQLAlchemy builds uses something we know is loadable. ``sqlite``
    # omits the driver to let SQLAlchemy default to the stdlib pysqlite.
    driver: str | None = None
    if canonical != "sqlite":
        for candidate in _DIALECT_DRIVER_MODULES[canonical]:
            if importlib.util.find_spec(candidate) is not None:
                # SQLAlchemy uses ``psycopg2``/``psycopg`` etc. as the
                # driver suffix rather than the raw module name in a
                # couple of cases, so we normalise here.
                driver = {
                    "psycopg": "psycopg",
                    "psycopg2": "psycopg2",
                    "psycopg2cffi": "psycopg2cffi",
                    "pymysql": "pymysql",
                    "mysqlclient": "mysqldb",
                    "MySQLdb": "mysqldb",
                }.get(candidate, candidate)
                break
        if driver is None:
            raise DatabaseError(
                f"no driver installed for {canonical!r}; install the "
                f"'mcpy-proxy[{canonical}]' extra"
            )
    drivername = f"{canonical}+{driver}" if driver else canonical
    try:
        url_obj = URL.create(
            drivername=drivername,
            username=user or None,
            password=password or None,
            host=host or None,
            port=port or None,
            database=database or None,
            query=query or {},
        )
    except (ArgumentError, ValueError) as exc:
        raise DatabaseError(f"invalid database connection details: {exc}") from exc
    return url_obj.render_as_string(hide_password=False)


def probe_connection(url: str) -> str:
    """Open a throwaway engine, issue a single ``SELECT 1``, and dispose.

    Returns the canonical dialect name on success so callers can echo
    it back to the UI. On failure, raises :class:`DatabaseError` with
    a human-readable message that strips credentials from the URL.

    Refuses ``sqlite+:memory:`` URLs because the proxy would lose all
    state on every restart — always a footgun, never intentional.
    """
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise DatabaseError(f"cannot parse database URL: {exc}") from exc
    canonical = _canonical_dialect(parsed.get_backend_name())
    if canonical == "sqlite" and (parsed.database in (None, "", ":memory:")):
        raise DatabaseError(
            "sqlite ':memory:' URLs are not supported — the proxy would "
            "lose all state on restart. Use a file path like "
            "sqlite:///<state_dir>/mcpy.db instead."
        )
    if canonical in _DIALECT_DRIVER_MODULES and canonical not in available_dialects():
        raise DatabaseError(
            f"{canonical} driver is not installed. Install "
            f"'mcpy-proxy[{canonical}]' (or the underlying package: "
            f"{', '.join(_DIALECT_DRIVER_MODULES[canonical])}) and retry."
        )
    try:
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args=(
                {"check_same_thread": False} if canonical == "sqlite" else {}
            ),
        )
    except Exception as exc:
        raise DatabaseError(
            f"failed to create engine for {sanitize_url(url)!r}: {exc}"
        ) from exc
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise DatabaseError(
            f"cannot connect to {sanitize_url(url)!r}: {exc}"
        ) from exc
    except Exception as exc:
        # Driver-specific exceptions may not be SQLAlchemyError (e.g.
        # psycopg2.OperationalError during DNS failure).
        raise DatabaseError(
            f"cannot connect to {sanitize_url(url)!r}: {exc}"
        ) from exc
    finally:
        engine.dispose()
    return canonical


__all__ = [
    "DEFAULT_SQLITE_FILENAME",
    "DatabaseError",
    "Engine",
    "_assemble_url_from_parts",
    "_default_state_dir",
    "available_dialects",
    "build_engine",
    "dialect_of",
    "known_table_names",
    "probe_connection",
    "resolve_database_url",
    "run_migrations",
    "sanitize_url",
]
