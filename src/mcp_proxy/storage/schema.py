"""SQLAlchemy Core table definitions for the MCPy persistent store.

Schema philosophy:

- **One active config row.** ``config_kv`` has a singleton ``active`` entry
  that holds the full ``AppConfig`` JSON payload. Replacement is atomic:
  one UPDATE/INSERT and readers see the new version. Keeping it as a
  single JSON blob mirrors how the runtime already treats config
  (validate-and-swap rather than field-level edits) and avoids a
  column-per-field migration treadmill every time the ``AppConfig``
  schema grows.
- **Separate history table.** Every successful apply writes a row to
  ``config_history`` with a monotonically increasing version and the
  full payload at that moment. Gives us rollback targets and an audit
  trail at the cost of a bit of disk — fine for a proxy whose config
  changes are measured in "tens per day, tops".
- **Upstreams are split out.** Even though the full AppConfig contains
  upstreams, we also maintain a denormalised ``upstreams`` table so the
  file-drop watcher, the dashboard, and the admin API can manipulate
  individual entries without locking the whole config blob. The two
  views are kept consistent inside ``ConfigStore`` by writing both
  atomically.
- **Secrets stay ciphertext.** Moving to a DB doesn't change the threat
  model: an attacker with DB read access + filesystem read access can
  compromise either the JSON file or the SQLite file equally, so the
  Fernet key is still the security boundary. Values live as ``LargeBinary``
  so drivers that distinguish text/binary (notably Postgres + psycopg)
  don't mangle the ciphertext.

Versioning: ``CURRENT_SCHEMA_VERSION`` is bumped whenever this file
materially changes the table layout. ``storage/db.py::run_migrations``
reads the stored version from ``schema_meta`` and replays any missing
steps. For now there is only ``v1``, so migration is "create tables if
they don't exist".
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    func,
)

CURRENT_SCHEMA_VERSION = 1

# A single MetaData object so create_all() / reflect() work in one call.
METADATA = MetaData()


# Tracks the schema version actually present in the DB. One row, one column.
# Bootstrapped to ``CURRENT_SCHEMA_VERSION`` the first time the store opens.
schema_meta_table = Table(
    "schema_meta",
    METADATA,
    Column("version", Integer, primary_key=True),
    Column("applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


# The one-row active config + a monotonically increasing version counter so
# the admin API can tell operators "you just applied version 42". Integer
# (not BigInteger) because SQLite only honours AUTOINCREMENT on plain
# INTEGER PRIMARY KEY columns; the audit table below relies on it.
config_kv_table = Table(
    "config_kv",
    METADATA,
    Column("key", String(64), primary_key=True),
    Column("payload", Text, nullable=False),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_by", String(128), nullable=True),
)


# Append-only audit log of every successful apply. ``version`` mirrors the
# counter in ``config_kv`` so "what was version 17" is a single SELECT.
config_history_table = Table(
    "config_history",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("version", Integer, nullable=False, index=True),
    Column("payload", Text, nullable=False),
    Column("source", String(128), nullable=True),
    Column("applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("applied_by", String(128), nullable=True),
)


# Denormalised view of the upstreams dict. Kept in sync with the JSON blob
# in config_kv so the file-drop watcher and /admin/api/upstreams can make
# fine-grained changes without a full-config swap.
upstreams_table = Table(
    "upstreams",
    METADATA,
    Column("name", String(128), primary_key=True),
    Column("settings", Text, nullable=False),
    Column("source", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


# Encrypted user secrets + internal OAuth state (under reserved ``__``-prefix
# names hidden from the admin API listing). ``value_ct`` is Fernet ciphertext;
# the key lives in MCPY_SECRETS_KEY or <state_dir>/secrets.key, NOT the DB.
secrets_table = Table(
    "secrets",
    METADATA,
    Column("name", String(128), primary_key=True),
    Column("value_ct", LargeBinary, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
)


# First-run onboarding state. One row, lifecycle:
#
#   - created on the very first start of a fresh DB (alongside the default
#     bootstrap config in cli.build_state)
#   - ``admin_token_set_at`` is stamped the moment the wizard POSTs a
#     token so a second round-trip can't overwrite it
#   - ``completed_at`` is stamped when the operator finishes the wizard;
#     from then on the onboarding admin endpoints return 410 Gone
#
# The self-destruct-once-done pattern (rather than a deletable row) gives
# us immutable "was this proxy ever onboarded" history for audit, and
# makes the "onboarding active" predicate a single SELECT.
onboarding_table = Table(
    "onboarding",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("admin_token_set_at", DateTime(timezone=True), nullable=True),
    Column("first_upstream_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("completed_by", String(128), nullable=True),
)


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "METADATA",
    "config_history_table",
    "config_kv_table",
    "onboarding_table",
    "schema_meta_table",
    "secrets_table",
    "upstreams_table",
]
