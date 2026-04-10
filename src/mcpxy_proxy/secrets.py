"""Backwards-compatible facade over the SQLAlchemy-backed ConfigStore.

The original ``mcpxy_proxy.secrets`` module shipped a self-contained
file-based store: a Fernet-encrypted JSON blob plus a per-instance key
loader. That code path moved into :class:`mcpxy_proxy.storage.ConfigStore`
when secrets joined the rest of MCPxy's persistent state in a single
SQLAlchemy database.

This module is now a *thin facade* that preserves the public API the
older code used:

- ``SecretsManager(state_dir=..., key_override=..., autoload=...)``
- ``await store.set(name, value, description=...)``
- ``store.get(name)`` / ``store.require(name)``
- ``await store.delete(name)``
- ``store.list_public()`` / ``store.known_names()`` / ``store.exists(name)``
- ``SecretsManager.generate_key()``
- ``SecretRecord``, ``SecretStoreError``, ``SecretNotFoundError``,
  ``SECRET_NAME_RE``

Internally, every call routes through a :class:`ConfigStore` opened
against the database resolved by ``MCPXY_DB_URL`` (default:
``sqlite:///<state_dir>/mcpxy.db``). The async ``set``/``delete`` shape
is preserved so callers (notably ``OAuthManager``) don't have to be
rewritten — under the hood the methods just call into the synchronous
``ConfigStore`` which is fast enough that we don't bother offloading to
a thread.

The Fernet key resolution rules are unchanged:

1. Constructor ``key_override``
2. ``MCPXY_SECRETS_KEY`` env var
3. ``<state_dir>/secrets.key`` (auto-generated 0600 on first use)

The DB stores ciphertext only — placing the key alongside it would
defeat the purpose, so the key file (or env var) remains the security
boundary.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable

try:
    from cryptography.fernet import Fernet
except Exception as exc:  # pragma: no cover - cryptography is a required dep
    raise RuntimeError(
        "mcpxy_proxy.secrets requires the 'cryptography' package; "
        "install it with `pip install cryptography`"
    ) from exc

from mcpxy_proxy.storage.config_store import (
    SECRET_NAME_RE,
    ConfigStore,
    SecretNotFoundError,
    SecretRecord,
    SecretStoreError,
    open_store,
)
from mcpxy_proxy.storage.db import (
    DEFAULT_SQLITE_FILENAME,
    DatabaseError,
    _default_state_dir,
)

logger = logging.getLogger(__name__)


DEFAULT_STATE_DIR_CANDIDATES: tuple[Path, ...] = (
    Path("/var/lib/mcpxy"),
    Path.home() / ".local" / "state" / "mcpxy",
)


def load_fernet(
    state_dir: Path | str | None = None,
    *,
    key_override: str | bytes | None = None,
) -> Fernet:
    """Resolve a Fernet cipher using the documented key precedence.

    Order: explicit override → ``MCPXY_SECRETS_KEY`` env var → file at
    ``<state_dir>/secrets.key`` (auto-generated 0600 on first use).
    Raises :class:`SecretStoreError` if the resolved bytes don't form
    a valid Fernet key.
    """
    if state_dir is None:
        state_path = _default_state_dir()
    else:
        state_path = Path(state_dir)
        state_path.mkdir(parents=True, exist_ok=True)

    raw: bytes | None = None
    source: str
    if key_override is not None:
        raw = key_override if isinstance(key_override, bytes) else key_override.encode("utf-8")
        source = "override"
    else:
        env_key = os.getenv("MCPXY_SECRETS_KEY")
        if env_key:
            raw = env_key.encode("utf-8")
            source = "env:MCPXY_SECRETS_KEY"
        else:
            key_path = state_path / "secrets.key"
            if key_path.exists():
                raw = key_path.read_bytes()
                source = f"file:{key_path}"
            else:
                raw = Fernet.generate_key()
                try:
                    key_path.write_bytes(raw)
                    os.chmod(key_path, 0o600)
                except OSError as exc:
                    logger.warning(
                        "secrets: could not persist auto-generated key to %s: %s. "
                        "Secrets will reset on restart; set MCPXY_SECRETS_KEY for "
                        "stable encryption.",
                        key_path,
                        exc,
                    )
                else:
                    logger.warning(
                        "secrets: auto-generated Fernet key at %s. "
                        "For production deployments, set MCPXY_SECRETS_KEY "
                        "and delete this file.",
                        key_path,
                    )
                source = f"auto:{key_path}"

    try:
        return Fernet(raw)
    except (TypeError, ValueError) as exc:
        raise SecretStoreError(
            f"invalid Fernet key from {source}: {exc}. A Fernet key is "
            "a 32-byte urlsafe-base64 string (44 chars); generate one "
            "with `python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'`"
        ) from exc


def _default_db_url_for_state_dir(state_dir: Path) -> str:
    return f"sqlite:///{state_dir / DEFAULT_SQLITE_FILENAME}"


class SecretsManager:
    """Backwards-compatible facade over :class:`ConfigStore`.

    Existing call sites (the CLI bootstrap, the OAuth manager, every
    test that constructed ``SecretsManager(state_dir=…, key_override=…)``)
    keep working unchanged. Internally each instance owns either:

    - a fresh ``ConfigStore`` opened against ``state_dir``'s default
      sqlite file (the historical "make me a store, please" path), or
    - a caller-supplied ``ConfigStore`` (used by ``cli.build_state``
      so the CLI shares one store across SecretsManager + AppState +
      RuntimeConfigManager + OAuthManager — a single DB connection,
      a single Fernet key, a single source of truth).

    Methods marked ``async`` keep their async signature so the OAuth
    manager and admin API can ``await`` them, but the actual work is
    synchronous (sub-millisecond for SQLite). If a future Postgres
    deployment hits latency that matters, switching to async drivers
    would only require touching the underlying ConfigStore.
    """

    def __init__(
        self,
        state_dir: Path | str | None = None,
        *,
        key_override: str | bytes | None = None,
        autoload: bool = True,
        config_store: ConfigStore | None = None,
        db_url: str | None = None,
    ) -> None:
        if config_store is not None:
            self._store = config_store
            self._owns_store = False
            self.state_dir = (
                Path(state_dir) if state_dir is not None else _default_state_dir()
            )
        else:
            self.state_dir = (
                Path(state_dir) if state_dir is not None else _default_state_dir()
            )
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fernet = load_fernet(self.state_dir, key_override=key_override)
            url = db_url or os.getenv("MCPXY_DB_URL") or _default_db_url_for_state_dir(self.state_dir)
            try:
                self._store = open_store(url, fernet=fernet)
            except DatabaseError as exc:
                raise SecretStoreError(str(exc)) from exc
            self._owns_store = True
        # ``autoload=False`` is honoured for the rare test that wants an
        # empty in-memory cache without touching the DB. ``open_store``
        # already calls load_all once; we provide a backdoor to flip the
        # cache to empty for those tests.
        if not autoload:
            self._store._secrets_cache.clear()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Properties matching the old API
    # ------------------------------------------------------------------

    @property
    def store(self) -> ConfigStore:
        """Underlying ConfigStore for callers that need full access."""
        return self._store

    @property
    def secrets_path(self) -> Path:
        """Backwards compatibility: the historical secrets.json location.

        The DB-backed store doesn't actually use this file anymore, but
        a few tests inspect it to assert "the file exists". Returning
        the path keeps those tests pointing at *something*; they should
        be migrated to assert against the DB row instead.
        """
        return self.state_dir / "secrets.json"

    @property
    def key_path(self) -> Path:
        return self.state_dir / "secrets.key"

    # ------------------------------------------------------------------
    # Mutating API (async-shaped to preserve the historical signature)
    # ------------------------------------------------------------------

    async def set(
        self,
        name: str,
        value: str,
        *,
        description: str = "",
    ) -> SecretRecord:
        return self._store.upsert_secret(name, value, description=description)

    async def delete(self, name: str) -> bool:
        return self._store.delete_secret(name)

    # ------------------------------------------------------------------
    # Read API (sync — fed by the in-memory cache so it's hot-path safe)
    # ------------------------------------------------------------------

    def get(self, name: str) -> str | None:
        return self._store.get_secret(name)

    def require(self, name: str) -> str:
        return self._store.require_secret(name)

    def exists(self, name: str) -> bool:
        return self._store.secret_exists(name)

    def list_public(self) -> list[dict[str, Any]]:
        return self._store.list_public_secrets()

    def known_names(self) -> Iterable[str]:
        return self._store.known_secret_names()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._owns_store:
            self._store.close()

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("utf-8")


__all__ = [
    "DEFAULT_STATE_DIR_CANDIDATES",
    "SECRET_NAME_RE",
    "SecretNotFoundError",
    "SecretRecord",
    "SecretStoreError",
    "SecretsManager",
    "load_fernet",
]
