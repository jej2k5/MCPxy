"""The one persistence facade the rest of MCPy talks to.

``ConfigStore`` owns a single SQLAlchemy ``Engine`` and exposes the
narrow set of operations the runtime actually needs:

- **Active config + history**: load/save the AppConfig JSON payload
  atomically, track version numbers, list recent history.
- **Upstreams**: a denormalised view kept in sync with the active config
  blob so the file-drop watcher and admin API can manipulate one
  upstream at a time.
- **Secrets**: encrypted at column level (Fernet) so moving to a DB
  doesn't weaken the at-rest guarantee the file store already gave us.
  The admin API listing hides ``__``-prefixed internal entries
  (OAuth tokens, dynamic client registrations) just like the old
  file-backed store did.

The store maintains two tiny in-memory caches populated at construction
time:

- ``_active_payload`` — the current config JSON so hot-path reads during
  request handling don't hit the DB.
- ``_secrets_cache`` — decrypted secret values keyed by name so
  ``${secret:NAME}`` expansion during config validation is a dict lookup,
  not a round-trip.

Both caches are invalidated-and-refilled inside the same ``threading.RLock``
as the DB write so concurrent admin-API requests never see a half-updated
state.

Synchronous on purpose — see the comment in ``storage/db.py`` for the
rationale. Async callers wrap individual mutations in ``asyncio.to_thread``
if they want to avoid blocking the event loop, but for SQLite + admin
frequency the cost is microseconds.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Engine, delete, func, insert, select, update

from mcp_proxy.storage.db import build_engine, run_migrations
from mcp_proxy.storage.schema import (
    config_history_table,
    config_kv_table,
    onboarding_table,
    secrets_table,
    upstreams_table,
)

logger = logging.getLogger(__name__)


ACTIVE_CONFIG_KEY = "active"

# Mirrors the old file-store regex so existing ${secret:NAME} references
# keep working after the migration.
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]*$")
_MAX_SECRET_NAME_LENGTH = 128
_MAX_SECRET_VALUE_LENGTH = 64 * 1024


class ConfigStoreError(RuntimeError):
    """Base class for storage-layer errors that should surface to users."""


class SecretStoreError(ConfigStoreError):
    """Raised on programmer-facing secret errors (bad name, value too big, …)."""


class SecretNotFoundError(KeyError):
    """Raised by ``require_secret`` when a name is missing."""


def _validate_secret_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise SecretStoreError("secret name must be a non-empty string")
    if len(name) > _MAX_SECRET_NAME_LENGTH:
        raise SecretStoreError(
            f"secret name exceeds {_MAX_SECRET_NAME_LENGTH} characters"
        )
    if not SECRET_NAME_RE.match(name):
        raise SecretStoreError(
            f"secret name {name!r} must match [A-Za-z0-9_][A-Za-z0-9_-]*"
        )


def _validate_secret_value(value: str) -> None:
    if not isinstance(value, str):
        raise SecretStoreError("secret value must be a string")
    if len(value) > _MAX_SECRET_VALUE_LENGTH:
        raise SecretStoreError(
            f"secret value exceeds {_MAX_SECRET_VALUE_LENGTH} bytes"
        )


def _preview(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return f"{value[:2]}{'•' * (len(value) - 4)}{value[-2:]}"


def _epoch(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@dataclass
class SecretRecord:
    """One stored secret. ``value`` is the decrypted plaintext held in
    the in-memory cache; the DB only ever sees Fernet ciphertext.

    ``to_public_dict`` is the safe projection admin API callers see:
    value is replaced with a masked preview and a length, never the
    plaintext itself.
    """

    name: str
    value: str
    description: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_used_at: float | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "value_length": len(self.value),
            "value_preview": _preview(self.value),
        }


@dataclass
class UpstreamRecord:
    name: str
    settings: dict[str, Any]
    source: str | None
    created_at: float
    updated_at: float


@dataclass
class OnboardingState:
    """First-run onboarding row loaded from the ``onboarding`` table.

    ``active`` = row exists and ``completed_at`` is null.
    ``expired`` = ``active`` but ``created_at`` is older than the
    configured TTL; the admin endpoints return 410 Gone in that case.
    """

    created_at: float
    admin_token_set_at: float | None
    first_upstream_at: float | None
    completed_at: float | None
    completed_by: str | None

    def is_complete(self) -> bool:
        return self.completed_at is not None

    def to_public_dict(self, *, ttl_s: float, now: float | None = None) -> dict[str, Any]:
        t = now if now is not None else time.time()
        active = self.completed_at is None
        expired = active and (t - self.created_at) > ttl_s
        return {
            "created_at": self.created_at,
            "admin_token_set_at": self.admin_token_set_at,
            "first_upstream_at": self.first_upstream_at,
            "completed_at": self.completed_at,
            "completed_by": self.completed_by,
            "active": active and not expired,
            "completed": self.completed_at is not None,
            "expired": expired,
            "ttl_s": ttl_s,
            "expires_at": self.created_at + ttl_s if active else None,
        }


class ConfigStore:
    """Single point of truth for everything persistent in MCPy.

    Construct via :func:`open_store` so migrations run automatically and
    in-memory caches get warmed in one call. Direct construction is
    available for tests that already have an Engine + Fernet pair.
    """

    def __init__(self, engine: Engine, fernet: Fernet) -> None:
        self._engine = engine
        self._fernet = fernet
        self._lock = threading.RLock()
        self._active_payload: dict[str, Any] | None = None
        self._active_version: int = 0
        self._secrets_cache: dict[str, SecretRecord] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load_all(self) -> None:
        """Warm the in-memory caches from the DB. Called once at startup."""
        with self._engine.connect() as conn:
            cfg_row = conn.execute(
                select(config_kv_table.c.payload, config_kv_table.c.version).where(
                    config_kv_table.c.key == ACTIVE_CONFIG_KEY
                )
            ).first()
            if cfg_row is not None:
                try:
                    self._active_payload = json.loads(cfg_row[0])
                except json.JSONDecodeError as exc:
                    raise ConfigStoreError(
                        f"active config payload is not valid JSON: {exc}"
                    ) from exc
                self._active_version = int(cfg_row[1] or 0)

            rows = conn.execute(
                select(
                    secrets_table.c.name,
                    secrets_table.c.value_ct,
                    secrets_table.c.description,
                    secrets_table.c.created_at,
                    secrets_table.c.updated_at,
                    secrets_table.c.last_used_at,
                )
            ).all()

        with self._lock:
            self._secrets_cache.clear()
            for name, value_ct, description, created_at, updated_at, last_used_at in rows:
                try:
                    plaintext = self._fernet.decrypt(bytes(value_ct)).decode("utf-8")
                except InvalidToken as exc:
                    # Subclass of ConfigStoreError so callers that catch
                    # the broader type still see this, but the more
                    # specific class lets the admin API distinguish
                    # "your encryption key is wrong" from generic
                    # storage failures.
                    raise SecretStoreError(
                        f"secret {name!r} cannot be decrypted: key mismatch. "
                        "Restore the original MCPY_SECRETS_KEY or wipe the "
                        "secrets table to start fresh."
                    ) from exc
                self._secrets_cache[str(name)] = SecretRecord(
                    name=str(name),
                    value=plaintext,
                    description=str(description or ""),
                    created_at=_epoch(created_at) or time.time(),
                    updated_at=_epoch(updated_at) or time.time(),
                    last_used_at=_epoch(last_used_at),
                )
        self._loaded = True

    def close(self) -> None:
        """Dispose of the engine. Safe to call multiple times."""
        self._engine.dispose()

    # ------------------------------------------------------------------
    # Active config
    # ------------------------------------------------------------------

    def get_active_config(self) -> dict[str, Any] | None:
        """Return a deep copy of the cached active config, or None when empty."""
        if self._active_payload is None:
            return None
        return json.loads(json.dumps(self._active_payload))

    def active_version(self) -> int:
        return self._active_version

    def is_empty(self) -> bool:
        return self._active_payload is None

    def save_active_config(
        self,
        payload: dict[str, Any],
        *,
        source: str | None = None,
        applied_by: str | None = None,
    ) -> int:
        """Atomically replace the active config and append to history.

        Also re-syncs the denormalised ``upstreams`` table from the new
        payload so the single-upstream CRUD endpoints and the file-drop
        watcher see the latest view without any extra writes from the
        caller.
        """
        with self._lock:
            new_version = self._active_version + 1
            serialised = json.dumps(payload, sort_keys=True)
            with self._engine.begin() as conn:
                exists = conn.execute(
                    select(config_kv_table.c.key).where(
                        config_kv_table.c.key == ACTIVE_CONFIG_KEY
                    )
                ).first()
                if exists is None:
                    conn.execute(
                        insert(config_kv_table).values(
                            key=ACTIVE_CONFIG_KEY,
                            payload=serialised,
                            version=new_version,
                            updated_by=applied_by,
                        )
                    )
                else:
                    conn.execute(
                        update(config_kv_table)
                        .where(config_kv_table.c.key == ACTIVE_CONFIG_KEY)
                        .values(
                            payload=serialised,
                            version=new_version,
                            updated_at=func.now(),
                            updated_by=applied_by,
                        )
                    )
                conn.execute(
                    insert(config_history_table).values(
                        version=new_version,
                        payload=serialised,
                        source=source,
                        applied_by=applied_by,
                    )
                )
                conn.execute(delete(upstreams_table))
                upstreams = payload.get("upstreams") or {}
                if isinstance(upstreams, dict):
                    for name, settings in upstreams.items():
                        if not isinstance(settings, dict):
                            continue
                        conn.execute(
                            insert(upstreams_table).values(
                                name=str(name),
                                settings=json.dumps(settings, sort_keys=True),
                                source=source,
                            )
                        )
            self._active_payload = json.loads(serialised)
            self._active_version = new_version
            return new_version

    def list_config_history(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    config_history_table.c.version,
                    config_history_table.c.source,
                    config_history_table.c.applied_at,
                    config_history_table.c.applied_by,
                )
                .order_by(config_history_table.c.version.desc())
                .limit(limit)
            ).all()
        return [
            {
                "version": int(v),
                "source": src,
                "applied_at": _epoch(applied_at),
                "applied_by": applied_by,
            }
            for (v, src, applied_at, applied_by) in rows
        ]

    def load_history_payload(self, version: int) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(config_history_table.c.payload).where(
                    config_history_table.c.version == int(version)
                )
            ).first()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------
    # Upstreams (denormalised view)
    # ------------------------------------------------------------------

    def list_upstreams(self) -> list[UpstreamRecord]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    upstreams_table.c.name,
                    upstreams_table.c.settings,
                    upstreams_table.c.source,
                    upstreams_table.c.created_at,
                    upstreams_table.c.updated_at,
                ).order_by(upstreams_table.c.name)
            ).all()
        records: list[UpstreamRecord] = []
        for name, settings_raw, source, created_at, updated_at in rows:
            try:
                settings = json.loads(settings_raw)
            except json.JSONDecodeError:
                settings = {}
            records.append(
                UpstreamRecord(
                    name=str(name),
                    settings=settings,
                    source=source,
                    created_at=_epoch(created_at) or 0.0,
                    updated_at=_epoch(updated_at) or 0.0,
                )
            )
        return records

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------

    def get_secret(self, name: str) -> str | None:
        rec = self._secrets_cache.get(name)
        if rec is None:
            return None
        rec.last_used_at = time.time()
        return rec.value

    def require_secret(self, name: str) -> str:
        value = self.get_secret(name)
        if value is None:
            raise SecretNotFoundError(f"secret {name!r} not found")
        return value

    def secret_exists(self, name: str) -> bool:
        return name in self._secrets_cache

    def list_public_secrets(self) -> list[dict[str, Any]]:
        """Return user-visible secrets only (hides ``__``-prefixed entries)."""
        return [
            rec.to_public_dict()
            for rec in sorted(self._secrets_cache.values(), key=lambda r: r.name)
            if not rec.name.startswith("__")
        ]

    def known_secret_names(self) -> Iterable[str]:
        return sorted(n for n in self._secrets_cache if not n.startswith("__"))

    def upsert_secret(
        self,
        name: str,
        value: str,
        *,
        description: str = "",
    ) -> SecretRecord:
        _validate_secret_name(name)
        _validate_secret_value(value)
        with self._lock:
            ciphertext = self._fernet.encrypt(value.encode("utf-8"))
            now = time.time()
            existing = self._secrets_cache.get(name)
            with self._engine.begin() as conn:
                exists_row = conn.execute(
                    select(secrets_table.c.name).where(secrets_table.c.name == name)
                ).first()
                if exists_row is None:
                    conn.execute(
                        insert(secrets_table).values(
                            name=name,
                            value_ct=ciphertext,
                            description=description,
                        )
                    )
                else:
                    conn.execute(
                        update(secrets_table)
                        .where(secrets_table.c.name == name)
                        .values(
                            value_ct=ciphertext,
                            description=description,
                            updated_at=func.now(),
                        )
                    )
            record = SecretRecord(
                name=name,
                value=value,
                description=description,
                created_at=existing.created_at if existing else now,
                updated_at=now,
                last_used_at=existing.last_used_at if existing else None,
            )
            self._secrets_cache[name] = record
            return record

    def delete_secret(self, name: str) -> bool:
        _validate_secret_name(name)
        with self._lock:
            if name not in self._secrets_cache:
                return False
            with self._engine.begin() as conn:
                conn.execute(
                    delete(secrets_table).where(secrets_table.c.name == name)
                )
            self._secrets_cache.pop(name, None)
            return True

    # ------------------------------------------------------------------
    # Onboarding state
    # ------------------------------------------------------------------

    def get_onboarding_state(self) -> OnboardingState | None:
        """Return the singleton onboarding row or ``None`` if it doesn't exist.

        A missing row means one of two things: (a) this DB was created
        before the onboarding feature shipped, or (b) the proxy is
        pre-first-run and ``ensure_onboarding_row()`` hasn't been called
        yet. Callers that want "start fresh on every new DB" semantics
        should go through ``ensure_onboarding_row``.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                select(
                    onboarding_table.c.created_at,
                    onboarding_table.c.admin_token_set_at,
                    onboarding_table.c.first_upstream_at,
                    onboarding_table.c.completed_at,
                    onboarding_table.c.completed_by,
                ).order_by(onboarding_table.c.id.asc()).limit(1)
            ).first()
        if row is None:
            return None
        return OnboardingState(
            created_at=_epoch(row[0]) or time.time(),
            admin_token_set_at=_epoch(row[1]),
            first_upstream_at=_epoch(row[2]),
            completed_at=_epoch(row[3]),
            completed_by=row[4],
        )

    def ensure_onboarding_row(self) -> OnboardingState:
        """Create the onboarding row if missing and return current state.

        Called from the CLI bootstrap path after the default config has
        been written, so any brand-new deployment starts with a fresh
        onboarding record ready for the wizard to stamp. Idempotent:
        subsequent calls return the existing row unchanged.
        """
        existing = self.get_onboarding_state()
        if existing is not None:
            return existing
        with self._lock:
            # Re-check inside the lock to avoid a double insert under
            # concurrent startup (fan-out from gunicorn workers etc.).
            with self._engine.begin() as conn:
                row = conn.execute(
                    select(onboarding_table.c.id).limit(1)
                ).first()
                if row is None:
                    conn.execute(insert(onboarding_table).values())
        fresh = self.get_onboarding_state()
        assert fresh is not None
        return fresh

    def stamp_admin_token_set(self) -> OnboardingState:
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    update(onboarding_table).values(
                        admin_token_set_at=func.now()
                    )
                )
        state = self.get_onboarding_state()
        assert state is not None
        return state

    def stamp_first_upstream(self) -> OnboardingState:
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    update(onboarding_table).values(
                        first_upstream_at=func.now()
                    )
                )
        state = self.get_onboarding_state()
        assert state is not None
        return state

    def finish_onboarding(self, *, completed_by: str | None = None) -> OnboardingState:
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    update(onboarding_table)
                    .where(onboarding_table.c.completed_at.is_(None))
                    .values(
                        completed_at=func.now(),
                        completed_by=completed_by,
                    )
                )
        state = self.get_onboarding_state()
        assert state is not None
        return state

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def engine(self) -> Engine:
        return self._engine

    @staticmethod
    def generate_key() -> str:
        """Convenience: generate a fresh Fernet key as a str."""
        return Fernet.generate_key().decode("utf-8")


def open_store(
    url: str | None = None,
    *,
    fernet: Fernet,
    echo: bool = False,
) -> ConfigStore:
    """Open (or create), migrate, and warm a :class:`ConfigStore`.

    Typical call site::

        fernet = load_fernet(state_dir)
        store = open_store(fernet=fernet)
    """
    engine = build_engine(url, echo=echo)
    run_migrations(engine)
    store = ConfigStore(engine, fernet)
    store.load_all()
    return store


__all__ = [
    "ACTIVE_CONFIG_KEY",
    "ConfigStore",
    "ConfigStoreError",
    "OnboardingState",
    "SECRET_NAME_RE",
    "SecretNotFoundError",
    "SecretRecord",
    "SecretStoreError",
    "UpstreamRecord",
    "open_store",
]
