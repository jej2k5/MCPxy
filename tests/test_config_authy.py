"""Tests for the Authy config models and effective auth mode resolution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcpxy_proxy.config import (
    AppConfig,
    AuthConfig,
    AuthyConfig,
    AuthyGoogleConfig,
    resolve_effective_auth_mode,
)


def test_authy_disabled_by_default():
    cfg = AuthConfig()
    assert cfg.authy.enabled is False
    assert cfg.authy.primary_provider is None


def test_authy_enabled_requires_primary_provider():
    with pytest.raises(ValidationError, match="primary_provider"):
        AuthyConfig(enabled=True, jwt_secret="test-secret-key")


def test_authy_enabled_requires_jwt_secret():
    with pytest.raises(ValidationError, match="jwt_secret"):
        AuthyConfig(enabled=True, primary_provider="local")


def test_authy_local_auto_creates_config():
    cfg = AuthyConfig(
        enabled=True, primary_provider="local", jwt_secret="test-secret"
    )
    assert cfg.local is not None
    assert cfg.local.token_ttl == 3600


def test_authy_google_requires_google_block():
    with pytest.raises(ValidationError, match="google"):
        AuthyConfig(
            enabled=True,
            primary_provider="google",
            jwt_secret="test-secret",
        )


def test_authy_google_with_config():
    cfg = AuthyConfig(
        enabled=True,
        primary_provider="google",
        jwt_secret="test-secret",
        google=AuthyGoogleConfig(
            client_id="goog-id",
            client_secret="goog-secret",
            redirect_uri="http://localhost/callback",
        ),
    )
    assert cfg.google is not None
    assert cfg.google.client_id == "goog-id"


def test_resolve_effective_auth_mode_authy():
    auth = AuthConfig(
        authy=AuthyConfig(
            enabled=True,
            primary_provider="local",
            jwt_secret="secret",
        )
    )
    assert resolve_effective_auth_mode(auth) == "authy"


def test_resolve_effective_auth_mode_legacy():
    auth = AuthConfig(token="my-bearer-token")
    assert resolve_effective_auth_mode(auth) == "legacy"


def test_resolve_effective_auth_mode_none():
    auth = AuthConfig()
    assert resolve_effective_auth_mode(auth) == "none"


def test_full_app_config_with_authy():
    cfg = AppConfig(
        auth={
            "authy": {
                "enabled": True,
                "primary_provider": "local",
                "jwt_secret": "test-secret",
            }
        }
    )
    assert cfg.auth.authy.enabled is True
    assert cfg.auth.authy.primary_provider == "local"


def test_redact_secrets_covers_authy():
    from mcpxy_proxy.config import redact_secrets

    payload = {
        "auth": {
            "token": "should-redact",
            "authy": {
                "jwt_secret": "should-redact",
                "google": {"client_secret": "should-redact"},
                "sso_saml": {"idp_cert": "should-redact", "sp_private_key": "should-redact"},
            },
        }
    }
    redacted = redact_secrets(payload)
    assert redacted["auth"]["token"] == "***REDACTED***"
    assert redacted["auth"]["authy"]["jwt_secret"] == "***REDACTED***"
    assert redacted["auth"]["authy"]["google"]["client_secret"] == "***REDACTED***"
    assert redacted["auth"]["authy"]["sso_saml"]["idp_cert"] == "***REDACTED***"
    assert redacted["auth"]["authy"]["sso_saml"]["sp_private_key"] == "***REDACTED***"
