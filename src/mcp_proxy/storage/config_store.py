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
    personal_access_tokens_table,
    revoked_jwt_ids_table,
    secrets_table,
    token_mappings_table,
    upstreams_table,
    user_invites_table,
    users_table,
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


@dataclass
class UserRecord:
    """A registered MCPy user."""

    id: int
    email: str
    username: str | None
    name: str | None
    provider: str
    provider_subject: str | None
    role: str
    created_at: float
    invited_by: int | None
    activated_at: float | None
    disabled_at: float | None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "username": self.username,
            "name": self.name,
            "provider": self.provider,
            "role": self.role,
            "created_at": self.created_at,
            "invited_by": self.invited_by,
            "activated_at": self.activated_at,
            "disabled_at": self.disabled_at,
        }

    def to_local_dict(self, password_hash: str | None = None) -> dict[str, Any] | None:
        """Shape expected by authy's LocalProvider ``find_user`` callback."""
        if password_hash is None:
            return None
        return {
            "id": str(self.id),
            "email": self.email,
            "name": self.name or self.email,
            "password_hash": password_hash,
        }


@dataclass
class InviteRecord:
    id: int
    token_hash: str
    email: str
    role: str
    created_at: float
    expires_at: float
    consumed_at: float | None
    invited_by: int | None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "role": self.role,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "consumed_at": self.consumed_at,
            "invited_by": self.invited_by,
        }


@dataclass
class PatRecord:
    id: int
    user_id: int
    name: str
    token_prefix: str
    created_at: float
    last_used_at: float | None
    expires_at: float | None
    revoked_at: float | None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "token_prefix": self.token_prefix,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
        }


@dataclass
class TokenMappingRecord:
    """Maps a user to an upstream-specific token (encrypted at rest)."""

    id: int
    upstream: str
    user_id: int
    upstream_token: str  # decrypted plaintext
    description: str
    created_at: float
    updated_at: float

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "upstream": self.upstream,
            "user_id": self.user_id,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_preview": _preview(self.upstream_token),
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
        self._revoked_jwts: set[str] = set()
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

        # Warm the revoked JWT set for fast is_jwt_revoked lookups.
        with self._engine.connect() as conn:
            revoked_rows = conn.execute(
                select(revoked_jwt_ids_table.c.jti).where(
                    revoked_jwt_ids_table.c.expires_at > func.now()
                )
            ).all()
        revoked_jti_set = {str(r[0]) for r in revoked_rows}

        with self._lock:
            self._secrets_cache.clear()
            for name, value_ct, description, created_at, updated_at, last_used_at in rows:
                try:
                    plaintext = self._fernet.decrypt(bytes(value_ct)).decode("utf-8")
                except InvalidToken as exc:
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
            self._revoked_jwts = revoked_jti_set
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

    def stamp_bootstrap_admin_email(self, email: str) -> None:
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    update(onboarding_table).values(bootstrap_admin_email=email)
                )

    def get_bootstrap_admin_email(self) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(onboarding_table.c.bootstrap_admin_email).limit(1)
            ).first()
        if row is None:
            return None
        return row[0]

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def _row_to_user(self, row: Any) -> UserRecord:
        return UserRecord(
            id=int(row[0]),
            email=str(row[1]),
            username=row[2],
            name=row[3],
            provider=str(row[4]),
            provider_subject=row[5],
            role=str(row[6]),
            created_at=_epoch(row[7]) or time.time(),
            invited_by=row[8],
            activated_at=_epoch(row[9]),
            disabled_at=_epoch(row[10]),
        )

    _USER_COLS = (
        users_table.c.id,
        users_table.c.email,
        users_table.c.username,
        users_table.c.name,
        users_table.c.provider,
        users_table.c.provider_subject,
        users_table.c.role,
        users_table.c.created_at,
        users_table.c.invited_by,
        users_table.c.activated_at,
        users_table.c.disabled_at,
    )

    def create_user(
        self,
        *,
        email: str,
        provider: str,
        role: str = "member",
        username: str | None = None,
        name: str | None = None,
        password_hash: str | None = None,
        provider_subject: str | None = None,
        invited_by: int | None = None,
        activated: bool = False,
    ) -> UserRecord:
        with self._lock:
            with self._engine.begin() as conn:
                result = conn.execute(
                    insert(users_table).values(
                        email=email,
                        username=username,
                        name=name,
                        password_hash=password_hash,
                        provider=provider,
                        provider_subject=provider_subject,
                        role=role,
                        invited_by=invited_by,
                        activated_at=func.now() if activated else None,
                    )
                )
                user_id = result.inserted_primary_key[0]
                row = conn.execute(
                    select(*self._USER_COLS).where(users_table.c.id == user_id)
                ).first()
        assert row is not None
        return self._row_to_user(row)

    def get_user(self, user_id: int) -> UserRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(*self._USER_COLS).where(users_table.c.id == user_id)
            ).first()
        if row is None:
            return None
        return self._row_to_user(row)

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(*self._USER_COLS).where(users_table.c.email == email)
            ).first()
        if row is None:
            return None
        return self._row_to_user(row)

    def get_user_password_hash(self, user_id: int) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(users_table.c.password_hash).where(users_table.c.id == user_id)
            ).first()
        if row is None:
            return None
        return row[0]

    def get_user_by_provider_subject(
        self, provider: str, subject: str
    ) -> UserRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(*self._USER_COLS).where(
                    (users_table.c.provider == provider)
                    & (users_table.c.provider_subject == subject)
                )
            ).first()
        if row is None:
            return None
        return self._row_to_user(row)

    def list_users(self, *, include_disabled: bool = False) -> list[UserRecord]:
        stmt = select(*self._USER_COLS).order_by(users_table.c.id)
        if not include_disabled:
            stmt = stmt.where(users_table.c.disabled_at.is_(None))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._row_to_user(r) for r in rows]

    def update_user_role(self, user_id: int, role: str) -> None:
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    update(users_table)
                    .where(users_table.c.id == user_id)
                    .values(role=role)
                )

    def activate_user(self, user_id: int) -> None:
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    update(users_table)
                    .where(users_table.c.id == user_id)
                    .values(activated_at=func.now())
                )

    def disable_user(self, user_id: int) -> None:
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    update(users_table)
                    .where(users_table.c.id == user_id)
                    .values(disabled_at=func.now())
                )

    def delete_user(self, user_id: int) -> bool:
        with self._lock:
            with self._engine.begin() as conn:
                result = conn.execute(
                    delete(users_table).where(users_table.c.id == user_id)
                )
                return result.rowcount > 0

    def count_admins(self) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(func.count())
                .select_from(users_table)
                .where(
                    (users_table.c.role == "admin")
                    & users_table.c.disabled_at.is_(None)
                )
            ).first()
        return int(row[0]) if row else 0

    def set_user_password_hash(self, user_id: int, password_hash: str) -> None:
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    update(users_table)
                    .where(users_table.c.id == user_id)
                    .values(password_hash=password_hash)
                )

    # ------------------------------------------------------------------
    # User invites
    # ------------------------------------------------------------------

    def create_invite(
        self,
        *,
        email: str,
        role: str,
        token_hash: str,
        expires_at: datetime,
        invited_by: int | None = None,
    ) -> InviteRecord:
        with self._lock:
            with self._engine.begin() as conn:
                result = conn.execute(
                    insert(user_invites_table).values(
                        token_hash=token_hash,
                        email=email,
                        role=role,
                        expires_at=expires_at,
                        invited_by=invited_by,
                    )
                )
                invite_id = result.inserted_primary_key[0]
                row = conn.execute(
                    select(
                        user_invites_table.c.id,
                        user_invites_table.c.token_hash,
                        user_invites_table.c.email,
                        user_invites_table.c.role,
                        user_invites_table.c.created_at,
                        user_invites_table.c.expires_at,
                        user_invites_table.c.consumed_at,
                        user_invites_table.c.invited_by,
                    ).where(user_invites_table.c.id == invite_id)
                ).first()
        assert row is not None
        return InviteRecord(
            id=int(row[0]),
            token_hash=str(row[1]),
            email=str(row[2]),
            role=str(row[3]),
            created_at=_epoch(row[4]) or time.time(),
            expires_at=_epoch(row[5]) or time.time(),
            consumed_at=_epoch(row[6]),
            invited_by=row[7],
        )

    def list_invites(self) -> list[InviteRecord]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    user_invites_table.c.id,
                    user_invites_table.c.token_hash,
                    user_invites_table.c.email,
                    user_invites_table.c.role,
                    user_invites_table.c.created_at,
                    user_invites_table.c.expires_at,
                    user_invites_table.c.consumed_at,
                    user_invites_table.c.invited_by,
                ).order_by(user_invites_table.c.id.desc())
            ).all()
        return [
            InviteRecord(
                id=int(r[0]),
                token_hash=str(r[1]),
                email=str(r[2]),
                role=str(r[3]),
                created_at=_epoch(r[4]) or time.time(),
                expires_at=_epoch(r[5]) or time.time(),
                consumed_at=_epoch(r[6]),
                invited_by=r[7],
            )
            for r in rows
        ]

    def consume_invite(self, invite_id: int) -> bool:
        with self._lock:
            with self._engine.begin() as conn:
                result = conn.execute(
                    update(user_invites_table)
                    .where(
                        (user_invites_table.c.id == invite_id)
                        & user_invites_table.c.consumed_at.is_(None)
                    )
                    .values(consumed_at=func.now())
                )
                return result.rowcount > 0

    # ------------------------------------------------------------------
    # Personal access tokens
    # ------------------------------------------------------------------

    def create_pat(
        self,
        *,
        user_id: int,
        name: str,
        token_hash: str,
        token_prefix: str,
        expires_at: datetime | None = None,
    ) -> PatRecord:
        with self._lock:
            with self._engine.begin() as conn:
                result = conn.execute(
                    insert(personal_access_tokens_table).values(
                        user_id=user_id,
                        name=name,
                        token_hash=token_hash,
                        token_prefix=token_prefix,
                        expires_at=expires_at,
                    )
                )
                pat_id = result.inserted_primary_key[0]
                row = conn.execute(
                    select(
                        personal_access_tokens_table.c.id,
                        personal_access_tokens_table.c.user_id,
                        personal_access_tokens_table.c.name,
                        personal_access_tokens_table.c.token_prefix,
                        personal_access_tokens_table.c.created_at,
                        personal_access_tokens_table.c.last_used_at,
                        personal_access_tokens_table.c.expires_at,
                        personal_access_tokens_table.c.revoked_at,
                    ).where(personal_access_tokens_table.c.id == pat_id)
                ).first()
        assert row is not None
        return PatRecord(
            id=int(row[0]),
            user_id=int(row[1]),
            name=str(row[2]),
            token_prefix=str(row[3]),
            created_at=_epoch(row[4]) or time.time(),
            last_used_at=_epoch(row[5]),
            expires_at=_epoch(row[6]),
            revoked_at=_epoch(row[7]),
        )

    def list_pats_for_user(self, user_id: int) -> list[PatRecord]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    personal_access_tokens_table.c.id,
                    personal_access_tokens_table.c.user_id,
                    personal_access_tokens_table.c.name,
                    personal_access_tokens_table.c.token_prefix,
                    personal_access_tokens_table.c.created_at,
                    personal_access_tokens_table.c.last_used_at,
                    personal_access_tokens_table.c.expires_at,
                    personal_access_tokens_table.c.revoked_at,
                )
                .where(
                    (personal_access_tokens_table.c.user_id == user_id)
                    & personal_access_tokens_table.c.revoked_at.is_(None)
                )
                .order_by(personal_access_tokens_table.c.id.desc())
            ).all()
        return [
            PatRecord(
                id=int(r[0]),
                user_id=int(r[1]),
                name=str(r[2]),
                token_prefix=str(r[3]),
                created_at=_epoch(r[4]) or time.time(),
                last_used_at=_epoch(r[5]),
                expires_at=_epoch(r[6]),
                revoked_at=_epoch(r[7]),
            )
            for r in rows
        ]

    def find_active_pats_by_prefix(self, prefix: str) -> list[tuple[PatRecord, str]]:
        """Return live PATs matching the given prefix along with their hash."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    personal_access_tokens_table.c.id,
                    personal_access_tokens_table.c.user_id,
                    personal_access_tokens_table.c.name,
                    personal_access_tokens_table.c.token_prefix,
                    personal_access_tokens_table.c.created_at,
                    personal_access_tokens_table.c.last_used_at,
                    personal_access_tokens_table.c.expires_at,
                    personal_access_tokens_table.c.revoked_at,
                    personal_access_tokens_table.c.token_hash,
                ).where(
                    (personal_access_tokens_table.c.token_prefix == prefix)
                    & personal_access_tokens_table.c.revoked_at.is_(None)
                )
            ).all()
        now = time.time()
        results: list[tuple[PatRecord, str]] = []
        for r in rows:
            exp = _epoch(r[6])
            if exp is not None and exp < now:
                continue
            results.append((
                PatRecord(
                    id=int(r[0]),
                    user_id=int(r[1]),
                    name=str(r[2]),
                    token_prefix=str(r[3]),
                    created_at=_epoch(r[4]) or time.time(),
                    last_used_at=_epoch(r[5]),
                    expires_at=exp,
                    revoked_at=_epoch(r[7]),
                ),
                str(r[8]),
            ))
        return results

    def touch_pat_last_used(self, pat_id: int) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    update(personal_access_tokens_table)
                    .where(personal_access_tokens_table.c.id == pat_id)
                    .values(last_used_at=func.now())
                )
        except Exception:
            pass  # fire-and-forget; never block a proxy request

    def revoke_pat(self, pat_id: int, user_id: int | None = None) -> bool:
        with self._lock:
            with self._engine.begin() as conn:
                stmt = (
                    update(personal_access_tokens_table)
                    .where(
                        (personal_access_tokens_table.c.id == pat_id)
                        & personal_access_tokens_table.c.revoked_at.is_(None)
                    )
                    .values(revoked_at=func.now())
                )
                if user_id is not None:
                    stmt = stmt.where(
                        personal_access_tokens_table.c.user_id == user_id
                    )
                result = conn.execute(stmt)
                return result.rowcount > 0

    def revoke_all_pats_for_user(self, user_id: int) -> int:
        with self._lock:
            with self._engine.begin() as conn:
                result = conn.execute(
                    update(personal_access_tokens_table)
                    .where(
                        (personal_access_tokens_table.c.user_id == user_id)
                        & personal_access_tokens_table.c.revoked_at.is_(None)
                    )
                    .values(revoked_at=func.now())
                )
                return result.rowcount

    # ------------------------------------------------------------------
    # JWT revocation
    # ------------------------------------------------------------------

    def revoke_jwt(self, jti: str, expires_at: datetime) -> None:
        with self._lock:
            with self._engine.begin() as conn:
                conn.execute(
                    insert(revoked_jwt_ids_table).values(
                        jti=jti, expires_at=expires_at
                    )
                )
            self._revoked_jwts.add(jti)

    def is_jwt_revoked(self, jti: str) -> bool:
        return jti in self._revoked_jwts

    # ------------------------------------------------------------------
    # Token mappings (per-user upstream token transformation)
    # ------------------------------------------------------------------

    def upsert_token_mapping(
        self,
        *,
        upstream: str,
        user_id: int,
        upstream_token: str,
        description: str = "",
    ) -> TokenMappingRecord:
        """Create or update a mapping from (upstream, user_id) -> upstream_token."""
        ciphertext = self._fernet.encrypt(upstream_token.encode("utf-8"))
        with self._lock:
            with self._engine.begin() as conn:
                existing = conn.execute(
                    select(token_mappings_table.c.id).where(
                        (token_mappings_table.c.upstream == upstream)
                        & (token_mappings_table.c.user_id == user_id)
                    )
                ).first()
                if existing is not None:
                    conn.execute(
                        update(token_mappings_table)
                        .where(token_mappings_table.c.id == existing[0])
                        .values(
                            upstream_token_ct=ciphertext,
                            description=description,
                            updated_at=func.now(),
                        )
                    )
                    mapping_id = existing[0]
                else:
                    result = conn.execute(
                        insert(token_mappings_table).values(
                            upstream=upstream,
                            user_id=user_id,
                            upstream_token_ct=ciphertext,
                            description=description,
                        )
                    )
                    mapping_id = result.inserted_primary_key[0]
                row = conn.execute(
                    select(
                        token_mappings_table.c.id,
                        token_mappings_table.c.upstream,
                        token_mappings_table.c.user_id,
                        token_mappings_table.c.upstream_token_ct,
                        token_mappings_table.c.description,
                        token_mappings_table.c.created_at,
                        token_mappings_table.c.updated_at,
                    ).where(token_mappings_table.c.id == mapping_id)
                ).first()
        assert row is not None
        return TokenMappingRecord(
            id=int(row[0]),
            upstream=str(row[1]),
            user_id=int(row[2]),
            upstream_token=self._fernet.decrypt(bytes(row[3])).decode("utf-8"),
            description=str(row[4]),
            created_at=_epoch(row[5]) or time.time(),
            updated_at=_epoch(row[6]) or time.time(),
        )

    def get_token_mapping(
        self, *, upstream: str, user_id: int
    ) -> TokenMappingRecord | None:
        """Resolve the upstream token for a given (upstream, user_id) pair."""
        with self._engine.connect() as conn:
            row = conn.execute(
                select(
                    token_mappings_table.c.id,
                    token_mappings_table.c.upstream,
                    token_mappings_table.c.user_id,
                    token_mappings_table.c.upstream_token_ct,
                    token_mappings_table.c.description,
                    token_mappings_table.c.created_at,
                    token_mappings_table.c.updated_at,
                ).where(
                    (token_mappings_table.c.upstream == upstream)
                    & (token_mappings_table.c.user_id == user_id)
                )
            ).first()
        if row is None:
            return None
        try:
            token = self._fernet.decrypt(bytes(row[3])).decode("utf-8")
        except Exception:
            return None
        return TokenMappingRecord(
            id=int(row[0]),
            upstream=str(row[1]),
            user_id=int(row[2]),
            upstream_token=token,
            description=str(row[4]),
            created_at=_epoch(row[5]) or time.time(),
            updated_at=_epoch(row[6]) or time.time(),
        )

    def list_token_mappings(
        self, *, upstream: str | None = None
    ) -> list[TokenMappingRecord]:
        stmt = select(
            token_mappings_table.c.id,
            token_mappings_table.c.upstream,
            token_mappings_table.c.user_id,
            token_mappings_table.c.upstream_token_ct,
            token_mappings_table.c.description,
            token_mappings_table.c.created_at,
            token_mappings_table.c.updated_at,
        ).order_by(token_mappings_table.c.id)
        if upstream is not None:
            stmt = stmt.where(token_mappings_table.c.upstream == upstream)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        results: list[TokenMappingRecord] = []
        for row in rows:
            try:
                token = self._fernet.decrypt(bytes(row[3])).decode("utf-8")
            except Exception:
                continue
            results.append(
                TokenMappingRecord(
                    id=int(row[0]),
                    upstream=str(row[1]),
                    user_id=int(row[2]),
                    upstream_token=token,
                    description=str(row[4]),
                    created_at=_epoch(row[5]) or time.time(),
                    updated_at=_epoch(row[6]) or time.time(),
                )
            )
        return results

    def delete_token_mapping(self, mapping_id: int) -> bool:
        with self._lock:
            with self._engine.begin() as conn:
                result = conn.execute(
                    delete(token_mappings_table).where(
                        token_mappings_table.c.id == mapping_id
                    )
                )
                return result.rowcount > 0

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
    state_dir: Path | str | None = None,
) -> ConfigStore:
    """Open (or create), migrate, and warm a :class:`ConfigStore`.

    Typical call site::

        fernet = load_fernet(state_dir)
        store = open_store(fernet=fernet, state_dir=state_dir)

    Passing ``state_dir`` threads the onboarding wizard's bootstrap
    file through to :func:`resolve_database_url` so the URL precedence
    (env var → bootstrap file → default) stays consistent across every
    entry point (CLI bootstrap, tests, the runtime hot-swap path).
    """
    engine = build_engine(url, echo=echo, state_dir=state_dir)
    run_migrations(engine)
    store = ConfigStore(engine, fernet)
    store.load_all()
    return store


__all__ = [
    "ACTIVE_CONFIG_KEY",
    "ConfigStore",
    "ConfigStoreError",
    "InviteRecord",
    "OnboardingState",
    "PatRecord",
    "SECRET_NAME_RE",
    "SecretNotFoundError",
    "SecretRecord",
    "SecretStoreError",
    "TokenMappingRecord",
    "UpstreamRecord",
    "UserRecord",
    "open_store",
]
