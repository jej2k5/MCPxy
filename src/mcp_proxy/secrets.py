"""First-class secrets store for MCPy.

Why a dedicated store rather than plain env vars?

- Operators want to manage upstream credentials from the dashboard without
  editing host/container env. Restarting to rotate a single key is too
  coarse, and many secrets would otherwise end up in shell history or CI
  logs.
- Config references via ``${secret:name}`` are decoupled from the ambient
  environment, so the same config file works across dev / staging / prod
  with different backing secrets.
- Everything is encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256),
  with the key sourced from ``MCPY_SECRETS_KEY`` if set, or generated and
  persisted to ``<state_dir>/secrets.key`` (0600) on first use.

The store is intentionally minimal: a flat ``name -> value`` map with a
little metadata for audit. Secret names are ``[A-Za-z0-9_-]+`` so they can
appear inside ``${secret:…}`` placeholders without escaping headaches.

This module is the single source of truth for the in-process secret map.
The admin API wraps it with CRUD endpoints, and the config expansion step
resolves placeholders by calling :meth:`SecretsManager.get` at apply time.
Resolved values never hit the filesystem in plaintext — only the ciphertext
blob at ``<state_dir>/secrets.json`` persists across restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception as exc:  # pragma: no cover - cryptography is a required dep
    raise RuntimeError(
        "mcp_proxy.secrets requires the 'cryptography' package; "
        "install it with `pip install cryptography`"
    ) from exc

logger = logging.getLogger(__name__)


DEFAULT_STATE_DIR_CANDIDATES: tuple[Path, ...] = (
    Path("/var/lib/mcpy"),
    Path.home() / ".local" / "state" / "mcpy",
)
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]*$")
_MAX_SECRET_NAME_LENGTH = 128
_MAX_SECRET_VALUE_LENGTH = 64 * 1024  # 64 KiB — OAuth refresh tokens fit easily


class SecretStoreError(RuntimeError):
    """Raised on programmer-facing errors (bad name, decrypt failure, …)."""


class SecretNotFoundError(KeyError):
    """Raised by SecretsManager.require() when a name is missing."""


@dataclass
class SecretRecord:
    """One stored secret with its metadata. ``value`` is the plaintext
    payload kept in-memory only; on disk it is part of a Fernet-encrypted
    blob. The ``to_public_dict`` projection never includes ``value`` and
    is what the admin API returns."""

    name: str
    value: str
    description: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_used_at: float | None = None

    def to_storage_dict(self) -> dict[str, Any]:
        return asdict(self)

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


def _preview(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return f"{value[:2]}{'•' * (len(value) - 4)}{value[-2:]}"


def _validate_name(name: str) -> None:
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


def _validate_value(value: str) -> None:
    if not isinstance(value, str):
        raise SecretStoreError("secret value must be a string")
    if len(value) > _MAX_SECRET_VALUE_LENGTH:
        raise SecretStoreError(
            f"secret value exceeds {_MAX_SECRET_VALUE_LENGTH} bytes"
        )


def _default_state_dir() -> Path:
    """Pick the most appropriate default for runtime state.

    In the container, /var/lib/mcpy exists and is writable; for local dev
    we fall back to ~/.local/state/mcpy. We never chown or permission the
    directory here — the caller is responsible for that (the Docker image
    already creates /var/lib/mcpy with the right owner in the runtime stage).
    """
    for candidate in DEFAULT_STATE_DIR_CANDIDATES:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.touch()
            probe.unlink()
            return candidate
        except OSError:
            continue
    # Last resort: mkdtemp-style directory under /tmp. The caller is free
    # to override explicitly via config anyway.
    fallback = Path("/tmp/mcpy-state")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


class SecretsManager:
    """Encrypted-at-rest secrets store.

    Concurrency: all mutating methods take an asyncio lock so concurrent
    admin API writers are serialised. Reads are lock-free and return the
    in-memory snapshot. That's fine because the only writer path is the
    admin API, which is single-process, and reads are called during config
    expansion where a slightly-stale view is benign.

    File layout under ``state_dir``:
      - ``secrets.json`` — Fernet-encrypted JSON blob (the whole map).
      - ``secrets.key``  — auto-generated Fernet key (0600) when no
                           ``MCPY_SECRETS_KEY`` env var is set. **This file
                           sitting next to the ciphertext is a convenience,
                           not a defence-in-depth story**; a production
                           deployment should set ``MCPY_SECRETS_KEY`` and
                           never persist the file.
    """

    def __init__(
        self,
        state_dir: Path | str | None = None,
        *,
        key_override: str | bytes | None = None,
        autoload: bool = True,
    ) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else _default_state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.secrets_path = self.state_dir / "secrets.json"
        self.key_path = self.state_dir / "secrets.key"
        self._fernet = self._load_fernet(key_override)
        self._records: dict[str, SecretRecord] = {}
        self._lock = asyncio.Lock()
        if autoload:
            self._load_from_disk()

    # ------------------------------------------------------------------
    # Key + cipher setup
    # ------------------------------------------------------------------

    def _load_fernet(self, override: str | bytes | None) -> Fernet:
        raw: bytes | None = None
        if override is not None:
            raw = override if isinstance(override, bytes) else override.encode("utf-8")
            source = "override"
        else:
            env_key = os.getenv("MCPY_SECRETS_KEY")
            if env_key:
                raw = env_key.encode("utf-8")
                source = "env:MCPY_SECRETS_KEY"
            elif self.key_path.exists():
                raw = self.key_path.read_bytes()
                source = f"file:{self.key_path}"
            else:
                raw = Fernet.generate_key()
                try:
                    self.key_path.write_bytes(raw)
                    os.chmod(self.key_path, 0o600)
                except OSError as exc:
                    logger.warning(
                        "secrets: could not persist auto-generated key to %s: %s. "
                        "Secrets will reset on restart; set MCPY_SECRETS_KEY for "
                        "stable encryption.",
                        self.key_path,
                        exc,
                    )
                else:
                    logger.warning(
                        "secrets: auto-generated Fernet key at %s. "
                        "For production deployments, set MCPY_SECRETS_KEY "
                        "and delete this file.",
                        self.key_path,
                    )
                source = f"auto:{self.key_path}"

        try:
            return Fernet(raw)
        except (TypeError, ValueError) as exc:
            raise SecretStoreError(
                f"invalid Fernet key from {source}: {exc}. A Fernet key is "
                "a 32-byte urlsafe-base64 string (44 chars); generate one "
                "with `python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'`"
            ) from exc

    # ------------------------------------------------------------------
    # Disk I/O
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> None:
        if not self.secrets_path.exists():
            return
        try:
            ciphertext = self.secrets_path.read_bytes()
        except OSError as exc:
            raise SecretStoreError(
                f"cannot read secrets file {self.secrets_path}: {exc}"
            ) from exc
        if not ciphertext:
            return
        try:
            plaintext = self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise SecretStoreError(
                f"cannot decrypt {self.secrets_path}: key mismatch. "
                "Either restore the original MCPY_SECRETS_KEY or remove "
                "the file to start fresh."
            ) from exc
        try:
            payload = json.loads(plaintext)
        except json.JSONDecodeError as exc:
            raise SecretStoreError(
                f"decrypted secrets payload is not valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise SecretStoreError(
                "decrypted secrets payload must be a JSON object"
            )
        for name, record in payload.items():
            if not isinstance(record, dict):
                continue
            try:
                self._records[name] = SecretRecord(
                    name=str(name),
                    value=str(record.get("value", "")),
                    description=str(record.get("description", "") or ""),
                    created_at=float(record.get("created_at") or time.time()),
                    updated_at=float(record.get("updated_at") or time.time()),
                    last_used_at=(
                        float(record["last_used_at"])
                        if record.get("last_used_at") is not None
                        else None
                    ),
                )
            except (TypeError, ValueError) as exc:
                logger.warning("secrets: dropping malformed record %r: %s", name, exc)

    def _save_to_disk(self) -> None:
        payload = {
            name: rec.to_storage_dict() for name, rec in self._records.items()
        }
        plaintext = json.dumps(payload, sort_keys=True).encode("utf-8")
        ciphertext = self._fernet.encrypt(plaintext)
        tmp = self.secrets_path.with_suffix(".json.tmp")
        try:
            tmp.write_bytes(ciphertext)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                # Filesystems without chmod support (e.g. some bind mounts)
                # shouldn't block secret storage.
                pass
            os.replace(tmp, self.secrets_path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def set(
        self,
        name: str,
        value: str,
        *,
        description: str = "",
    ) -> SecretRecord:
        """Create or update a secret. Returns the stored record."""
        _validate_name(name)
        _validate_value(value)
        async with self._lock:
            now = time.time()
            existing = self._records.get(name)
            rec = SecretRecord(
                name=name,
                value=value,
                description=description,
                created_at=existing.created_at if existing else now,
                updated_at=now,
                last_used_at=existing.last_used_at if existing else None,
            )
            self._records[name] = rec
            self._save_to_disk()
            return rec

    async def delete(self, name: str) -> bool:
        _validate_name(name)
        async with self._lock:
            if name not in self._records:
                return False
            self._records.pop(name)
            self._save_to_disk()
            return True

    def get(self, name: str) -> str | None:
        """Return the plaintext value for a secret, or None if missing.

        Also stamps ``last_used_at`` in-memory so operators can see which
        secrets are actually referenced by live config. The stamp is not
        flushed to disk on every read to avoid write amplification during
        hot-reloads; it persists on the next ``set``/``delete``.
        """
        rec = self._records.get(name)
        if rec is None:
            return None
        rec.last_used_at = time.time()
        return rec.value

    def require(self, name: str) -> str:
        """Like :meth:`get` but raises :class:`SecretNotFoundError` on miss."""
        value = self.get(name)
        if value is None:
            raise SecretNotFoundError(f"secret {name!r} not found")
        return value

    def exists(self, name: str) -> bool:
        return name in self._records

    def list_public(self) -> list[dict[str, Any]]:
        """Return user-visible secrets only.

        Names that start with ``__`` are considered internal plumbing
        (OAuth tokens, dynamic client registrations, …) and are hidden
        from the admin API listing. They remain fully functional for
        :meth:`get` / :meth:`set` callers inside the process.
        """
        return [
            rec.to_public_dict()
            for rec in sorted(self._records.values(), key=lambda r: r.name)
            if not rec.name.startswith("__")
        ]

    def known_names(self) -> Iterable[str]:
        """Names operators can reference via ``${secret:...}``.

        Internal ``__``-prefixed entries (OAuth tokens etc.) are hidden
        from this view for the same reason as :meth:`list_public`.
        """
        return sorted(n for n in self._records if not n.startswith("__"))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def generate_key() -> str:
        """Convenience: generate a fresh Fernet key as a str."""
        return Fernet.generate_key().decode("utf-8")


__all__ = [
    "SecretsManager",
    "SecretRecord",
    "SecretStoreError",
    "SecretNotFoundError",
    "SECRET_NAME_RE",
]
