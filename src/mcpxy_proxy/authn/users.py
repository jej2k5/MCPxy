"""User management business logic on top of ConfigStore CRUD."""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone

import bcrypt

from mcpxy_proxy.storage.config_store import ConfigStore, InviteRecord, PatRecord, UserRecord

from .manager import AuthnManager

PAT_PREFIX = "mcpxy_pat_"


def create_bootstrap_admin(
    store: ConfigStore,
    *,
    email: str,
    name: str,
    password: str,
    manager: AuthnManager,
) -> UserRecord:
    """Create the very first admin user during onboarding."""
    password_hash = manager.hash_password(password)
    return store.create_user(
        email=email,
        username=email,
        name=name,
        password_hash=password_hash,
        provider="local",
        role="admin",
        activated=True,
    )


def invite_user(
    store: ConfigStore,
    *,
    email: str,
    role: str = "member",
    invited_by_id: int | None = None,
    ttl_hours: int = 72,
) -> tuple[InviteRecord, str]:
    """Create an invite and return (record, plaintext_token)."""
    plaintext = secrets.token_urlsafe(32)
    token_hash = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()
    expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    record = store.create_invite(
        email=email,
        role=role,
        token_hash=token_hash,
        expires_at=expires,
        invited_by=invited_by_id,
    )
    return record, plaintext


def accept_invite(
    store: ConfigStore,
    *,
    token_plaintext: str,
    password: str,
    name: str | None = None,
    manager: AuthnManager,
) -> UserRecord | None:
    """Consume an invite by matching its bcrypt hash, then create a user."""
    now = time.time()
    for inv in store.list_invites():
        if inv.consumed_at is not None:
            continue
        if inv.expires_at < now:
            continue
        if bcrypt.checkpw(token_plaintext.encode(), inv.token_hash.encode()):
            store.consume_invite(inv.id)
            password_hash = manager.hash_password(password)
            user = store.create_user(
                email=inv.email,
                name=name or inv.email,
                password_hash=password_hash,
                provider="local",
                role=inv.role,
                invited_by=inv.invited_by,
                activated=True,
            )
            return user
    return None


def ensure_federated_user_on_callback(
    store: ConfigStore,
    *,
    provider: str,
    subject: str,
    email: str,
    name: str,
) -> tuple[UserRecord, bool]:
    """Find-or-create a user from a federated callback.

    If the user's email matches the bootstrap_admin_email stored during
    onboarding, auto-promote to admin.
    """
    existing = store.get_user_by_provider_subject(provider, subject)
    if existing is not None:
        return existing, False

    existing_by_email = store.get_user_by_email(email)
    if existing_by_email is not None:
        return existing_by_email, False

    bootstrap_email = store.get_bootstrap_admin_email()
    role = "admin" if bootstrap_email and email.lower() == bootstrap_email.lower() else "member"

    user = store.create_user(
        email=email,
        name=name,
        provider=provider,
        provider_subject=subject,
        role=role,
        activated=True,
    )
    return user, True


def mint_pat(
    store: ConfigStore,
    *,
    user_id: int,
    name: str,
    ttl_days: int | None = None,
) -> tuple[PatRecord, str]:
    """Create a personal access token. Returns (record, plaintext)."""
    raw = secrets.token_urlsafe(30)
    plaintext = f"{PAT_PREFIX}{raw}"
    token_prefix = plaintext[:8]
    token_hash = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()
    expires_at = None
    if ttl_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    record = store.create_pat(
        user_id=user_id,
        name=name,
        token_hash=token_hash,
        token_prefix=token_prefix,
        expires_at=expires_at,
    )
    return record, plaintext


def verify_pat(
    store: ConfigStore,
    plaintext: str,
) -> UserRecord | None:
    """Verify a PAT and return its owner, or None."""
    if not plaintext.startswith(PAT_PREFIX):
        return None
    prefix = plaintext[:8]
    candidates = store.find_active_pats_by_prefix(prefix)
    for pat, stored_hash in candidates:
        if bcrypt.checkpw(plaintext.encode(), stored_hash.encode()):
            store.touch_pat_last_used(pat.id)
            user = store.get_user(pat.user_id)
            if user is not None and user.disabled_at is None:
                return user
    return None
