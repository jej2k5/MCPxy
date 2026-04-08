"""Persistent storage layer for MCPy runtime state.

This package owns everything that survives a process restart:

- Live application config (the JSON payload ``RuntimeConfigManager`` applies)
- Config history for audit and rollback
- Per-upstream definitions (stdio + http with their auth blocks)
- Encrypted user secrets and OAuth tokens

Everything lives in a single SQLAlchemy-managed database selected by the
``MCPY_DB_URL`` env var (or the ``--db-url`` CLI flag). The default is
``sqlite+aiosqlite:///<state_dir>/mcpy.db`` which drops zero new operational
requirements on single-container deployments. Operators who need Postgres
or MySQL swap the URL; SQLAlchemy handles the rest.

Public entry points:

- :class:`ConfigStore` — the one class other modules depend on.
  All reads/writes to the DB go through it, with an in-memory cache for
  hot-path lookups (secrets expansion, config reads during request handling).
- :func:`open_store` — convenience that opens a connection, runs migrations
  if needed, and returns an initialised ``ConfigStore``.
"""

from mcp_proxy.storage.config_store import ConfigStore, OnboardingState, open_store
from mcp_proxy.storage.db import (
    DEFAULT_SQLITE_FILENAME,
    DatabaseError,
    build_engine,
    resolve_database_url,
)
from mcp_proxy.storage.schema import (
    CURRENT_SCHEMA_VERSION,
    METADATA,
    config_history_table,
    config_kv_table,
    onboarding_table,
    secrets_table,
    upstreams_table,
)

__all__ = [
    "ConfigStore",
    "CURRENT_SCHEMA_VERSION",
    "DatabaseError",
    "DEFAULT_SQLITE_FILENAME",
    "METADATA",
    "OnboardingState",
    "build_engine",
    "config_history_table",
    "config_kv_table",
    "onboarding_table",
    "open_store",
    "resolve_database_url",
    "secrets_table",
    "upstreams_table",
]
