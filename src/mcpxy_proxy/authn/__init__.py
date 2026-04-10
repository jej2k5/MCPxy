"""MCPxy authentication integration (wraps the ``authy`` package)."""

from __future__ import annotations

from .manager import AuthnManager, FederatedStartResult
from .middleware import Principal, extract_principal, require_admin_principal, require_principal
from .users import (
    accept_invite,
    create_bootstrap_admin,
    ensure_federated_user_on_callback,
    invite_user,
    mint_pat,
    verify_pat,
)

__all__ = [
    "AuthnManager",
    "FederatedStartResult",
    "Principal",
    "accept_invite",
    "create_bootstrap_admin",
    "ensure_federated_user_on_callback",
    "extract_principal",
    "invite_user",
    "mint_pat",
    "require_admin_principal",
    "require_principal",
    "verify_pat",
]
