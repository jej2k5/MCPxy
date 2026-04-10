"""FastAPI authentication middleware for the Authy integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException, Request

from mcp_proxy.config import AuthConfig, resolve_admin_token
from mcp_proxy.storage.config_store import ConfigStore

from .manager import AuthnManager
from .users import PAT_PREFIX, verify_pat


@dataclass(frozen=True)
class Principal:
    """Authenticated identity attached to a request."""

    user_id: int
    email: str
    role: str
    provider: str
    auth_mode: Literal["jwt", "pat", "legacy"]
    token_jti: str | None = None


async def extract_principal(
    request: Request,
    *,
    auth_config: AuthConfig,
    manager: AuthnManager,
    store: ConfigStore,
) -> Principal | None:
    """Try every credential path and return a Principal, or None."""
    bearer = _get_bearer(request)
    cookie_name = auth_config.authy.cookie_name if auth_config.authy.enabled else None

    if auth_config.authy.enabled:
        # 1. PAT path (most common for /mcp headless clients)
        if bearer and bearer.startswith(PAT_PREFIX):
            user = verify_pat(store, bearer)
            if user is not None:
                return Principal(
                    user_id=user.id,
                    email=user.email,
                    role=user.role,
                    provider=user.provider,
                    auth_mode="pat",
                )
            return None

        # 2. Session cookie
        if cookie_name:
            cookie_val = request.cookies.get(cookie_name)
            if cookie_val:
                payload = manager.verify(cookie_val)
                if payload:
                    return _principal_from_jwt(payload, store)

        # 3. Bearer as JWT
        if bearer:
            payload = manager.verify(bearer)
            if payload:
                return _principal_from_jwt(payload, store)

        return None

    # Legacy fallback
    expected = resolve_admin_token(auth_config)
    if expected and bearer == expected:
        return Principal(
            user_id=-1,
            email="legacy@local",
            role="admin",
            provider="legacy",
            auth_mode="legacy",
        )
    if not expected:
        # No auth configured at all
        return None
    return None


def _principal_from_jwt(payload: dict[str, Any], store: ConfigStore) -> Principal | None:
    """Build a Principal from a verified JWT payload."""
    sub = payload.get("sub") or payload.get("id")
    if sub is None:
        return None
    try:
        user_id = int(sub)
    except (TypeError, ValueError):
        return None
    user = store.get_user(user_id)
    if user is None or user.disabled_at is not None:
        return None
    return Principal(
        user_id=user.id,
        email=user.email,
        role=user.role,
        provider=user.provider,
        auth_mode="jwt",
        token_jti=payload.get("jti"),
    )


def _get_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-mcpy-token")


def require_principal(request: Request) -> Principal:
    """FastAPI dependency: 401 if no principal on request."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return principal


def require_admin_principal(request: Request) -> Principal:
    """FastAPI dependency: 401/403 if not an admin."""
    principal = require_principal(request)
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="forbidden")
    return principal
