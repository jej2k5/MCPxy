"""Pre-engine bootstrap configuration file.

The proxy cannot serve HTTP until ``ConfigStore`` has opened a DB engine
and warmed its caches, which means the database URL must be resolved
*before* the onboarding wizard can even render. For a long time the only
way to point MCPxy at a non-default database was the ``MCPXY_DB_URL``
environment variable, which is invisible to anyone who isn't already
editing Docker Compose files.

``bootstrap.json`` is the missing link: a tiny JSON file in the state
directory that captures the database URL the onboarding wizard wrote.
``cli.build_state`` reads it *before* it builds the engine, so the
second boot of a freshly onboarded proxy lands directly on the operator's
real database (Postgres, MySQL, or a custom SQLite path) without any env
var plumbing.

The file is deliberately minimal — we only persist the handful of fields
that must be resolved before the DB is open. Every other setting still
lives in the DB itself where it belongs.

Precedence, highest first, is enforced by ``resolve_database_url``:

1. Explicit ``url`` argument (used by tests).
2. ``MCPXY_DB_URL`` environment variable — stays the operator override
   for container deployments that inject the URL at boot time.
3. ``<state_dir>/bootstrap.json`` — written by the onboarding wizard.
4. ``sqlite:///<state_dir>/mcpxy.db`` — the out-of-the-box default.

The file is written ``0o600`` via the same tempfile + ``os.replace``
pattern used by the Fernet key, and is refused if it contains anything
other than a well-formed JSON object.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


BOOTSTRAP_FILENAME = "bootstrap.json"


class BootstrapError(RuntimeError):
    """Raised when ``bootstrap.json`` exists but cannot be parsed.

    Deliberately distinct from ``DatabaseError`` so callers can tell
    "the bootstrap file is broken" apart from "the DB it points at is
    broken"; the first is recoverable by deleting the file, the second
    is not.
    """


class BootstrapConfig:
    """In-memory view of ``bootstrap.json``.

    Plain class (not a pydantic model) to keep this module importable
    from inside the ``storage`` package without introducing an import
    cycle on ``config`` / pydantic. The fields we persist are small
    enough that a hand-written (de)serialiser is cheaper than pulling
    in another dependency at the bootstrap layer.
    """

    __slots__ = ("db_url", "written_at", "written_by")

    def __init__(
        self,
        *,
        db_url: str | None = None,
        written_at: datetime | None = None,
        written_by: str | None = None,
    ) -> None:
        self.db_url = db_url
        self.written_at = written_at
        self.written_by = written_by

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_url": self.db_url,
            "written_at": self.written_at.isoformat() if self.written_at else None,
            "written_by": self.written_by,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BootstrapConfig":
        if not isinstance(payload, dict):
            raise BootstrapError(
                f"bootstrap.json must contain a JSON object, got {type(payload).__name__}"
            )
        raw_url = payload.get("db_url")
        if raw_url is not None and not isinstance(raw_url, str):
            raise BootstrapError("bootstrap.json 'db_url' must be a string or null")
        raw_written_at = payload.get("written_at")
        parsed_at: datetime | None = None
        if isinstance(raw_written_at, str) and raw_written_at:
            try:
                parsed_at = datetime.fromisoformat(raw_written_at)
            except ValueError:
                # Non-fatal: the timestamp is informational only, we
                # don't want a clock format change to stop the proxy
                # from booting.
                logger.warning(
                    "bootstrap.json: could not parse written_at %r; ignoring",
                    raw_written_at,
                )
        raw_written_by = payload.get("written_by")
        if raw_written_by is not None and not isinstance(raw_written_by, str):
            raise BootstrapError("bootstrap.json 'written_by' must be a string or null")
        return cls(
            db_url=raw_url,
            written_at=parsed_at,
            written_by=raw_written_by,
        )


def bootstrap_path(state_dir: Path | str) -> Path:
    """Return the canonical path for the bootstrap file inside ``state_dir``."""
    return Path(state_dir) / BOOTSTRAP_FILENAME


def load_bootstrap(state_dir: Path | str) -> BootstrapConfig | None:
    """Return the parsed bootstrap file or ``None`` if it doesn't exist.

    A missing file is the common case (fresh install, or operator who
    only uses ``MCPXY_DB_URL``) and must not raise. A corrupt file is
    always fatal — we refuse to silently downgrade to the SQLite
    default because that would lose a Postgres URL the operator
    intentionally wrote.
    """
    path = bootstrap_path(state_dir)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BootstrapError(f"failed to read {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError(
            f"{path} is not valid JSON: {exc}. Delete the file to reset or "
            "restore a backup."
        ) from exc
    return BootstrapConfig.from_dict(payload)


def write_bootstrap(
    state_dir: Path | str,
    config: BootstrapConfig,
) -> Path:
    """Atomically persist ``config`` to ``<state_dir>/bootstrap.json``.

    Uses a tempfile + ``os.replace`` so a crash mid-write never leaves a
    truncated file behind. The final file is ``0o600`` because the
    operator may have pasted a password into ``db_url`` even though we
    strongly recommend using ``${secret:NAME}`` references.
    """
    dir_path = Path(state_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / BOOTSTRAP_FILENAME

    if config.written_at is None:
        config.written_at = datetime.now(timezone.utc)

    payload = json.dumps(config.to_dict(), indent=2, sort_keys=True)

    # NamedTemporaryFile so the tempfile lives in the same directory as
    # the target (needed for ``os.replace`` to be atomic across all
    # filesystems). delete=False because we hand the path to
    # ``os.replace`` ourselves.
    fd, tmp_name = tempfile.mkstemp(
        prefix=".bootstrap.", suffix=".tmp", dir=str(dir_path)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_name, 0o600)
        except OSError as exc:  # pragma: no cover - platform-dependent
            logger.warning(
                "bootstrap: could not chmod %s to 0600: %s", tmp_name, exc
            )
        os.replace(tmp_name, target)
    except Exception:
        # Best-effort cleanup — swallow unlink errors so the original
        # exception propagates unchanged.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def clear_bootstrap(state_dir: Path | str) -> bool:
    """Delete ``bootstrap.json`` if present. Returns True if removed."""
    path = bootstrap_path(state_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


__all__ = [
    "BOOTSTRAP_FILENAME",
    "BootstrapConfig",
    "BootstrapError",
    "bootstrap_path",
    "clear_bootstrap",
    "load_bootstrap",
    "write_bootstrap",
]
