"""AuthnManager — wraps ``authy.AuthManager`` for MCPxy.

This is the only module in the MCPxy codebase that imports from the
third-party ``authy`` package. Everything else talks to ``AuthnManager``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from authy import (
    AuthManager,
    AuthResult,
    GoogleProvider,
    GoogleProviderConfig,
    LocalProvider,
    LocalProviderConfig,
    M365Provider,
    M365ProviderConfig,
    SSOProvider,
    OidcSSOConfig,
    SamlSSOConfig,
    hash_password,
)

from mcpxy_proxy.config import AuthyConfig
from mcpxy_proxy.storage.config_store import ConfigStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FederatedStartResult:
    """Data extracted from authy's ``get_auth_url`` JWT."""

    auth_url: str
    code_verifier: str | None


class AuthnManager:
    """Lifecycle owner for the underlying ``authy.AuthManager``."""

    def __init__(
        self,
        config: AuthyConfig,
        *,
        store: ConfigStore,
    ) -> None:
        self._store = store
        self._underlying: AuthManager | None = None
        self._config = config
        if config.enabled:
            self.rebuild(config)

    def rebuild(self, config: AuthyConfig) -> None:
        """(Re)construct the ``authy.AuthManager`` from *config*."""
        self._config = config
        if not config.enabled or not config.jwt_secret:
            self._underlying = None
            return

        mgr = AuthManager(jwt_secret=config.jwt_secret)
        provider_name = config.primary_provider

        if provider_name == "local":
            lcfg = config.local or LocalProviderConfig(
                jwt_secret=config.jwt_secret,
                token_ttl=config.token_ttl_s,
            )
            prov = LocalProvider(
                config=LocalProviderConfig(
                    jwt_secret=config.jwt_secret,
                    token_ttl=lcfg.token_ttl,
                ),
                find_user=self._find_user,
            )
            mgr.register(prov)

        elif provider_name == "google" and config.google:
            prov = GoogleProvider(
                config=GoogleProviderConfig(
                    client_id=config.google.client_id,
                    client_secret=config.google.client_secret,
                    redirect_uri=config.google.redirect_uri,
                    jwt_secret=config.jwt_secret,
                    token_ttl=config.token_ttl_s,
                ),
            )
            mgr.register(prov)

        elif provider_name == "m365" and config.m365:
            prov = M365Provider(
                config=M365ProviderConfig(
                    client_id=config.m365.client_id,
                    client_secret=config.m365.client_secret,
                    tenant_id=config.m365.tenant_id,
                    redirect_uri=config.m365.redirect_uri,
                    jwt_secret=config.jwt_secret,
                    token_ttl=config.token_ttl_s,
                ),
            )
            mgr.register(prov)

        elif provider_name == "sso_oidc" and config.sso_oidc:
            prov = SSOProvider(
                config=OidcSSOConfig(
                    type="oidc",
                    issuer_url=config.sso_oidc.issuer_url,
                    client_id=config.sso_oidc.client_id,
                    client_secret=config.sso_oidc.client_secret,
                    redirect_uri=config.sso_oidc.redirect_uri,
                    jwt_secret=config.jwt_secret,
                    token_ttl=config.token_ttl_s,
                ),
            )
            mgr.register(prov)

        elif provider_name == "sso_saml" and config.sso_saml:
            prov = SSOProvider(
                config=SamlSSOConfig(
                    type="saml",
                    sp_entity_id=config.sso_saml.sp_entity_id,
                    idp_sso_url=config.sso_saml.idp_sso_url,
                    idp_cert=config.sso_saml.idp_cert,
                    sp_private_key=config.sso_saml.sp_private_key,
                    jwt_secret=config.jwt_secret,
                    token_ttl=config.token_ttl_s,
                ),
            )
            mgr.register(prov)

        self._underlying = mgr
        logger.info("authn: rebuilt AuthManager with provider=%s", provider_name)

    async def _find_user(self, username: str) -> dict[str, Any] | None:
        """``LocalProvider`` callback — look up a user by email."""
        user = self._store.get_user_by_email(username)
        if user is None or user.disabled_at is not None:
            return None
        pw_hash = self._store.get_user_password_hash(user.id)
        if pw_hash is None:
            return None
        return {
            "id": str(user.id),
            "email": user.email,
            "name": user.name or user.email,
            "password_hash": pw_hash,
        }

    async def authenticate_local(self, email: str, password: str) -> AuthResult:
        if self._underlying is None:
            return AuthResult(success=False, error="auth not configured")
        return await self._underlying.authenticate(
            "local", {"username": email, "password": password}
        )

    async def start_federated(self, provider_name: str, state: str) -> FederatedStartResult:
        if self._underlying is None:
            raise RuntimeError("auth not configured")
        result = await self._underlying.authenticate(
            provider_name, {"action": "get_auth_url", "state": state}
        )
        if result.error:
            raise RuntimeError(result.error)
        # authy returns a signed JWT whose payload contains the real
        # auth_url and a PKCE code_verifier.  Decode it to extract both.
        token = result.token or ""
        try:
            payload = self._underlying.verify_token(token)
        except Exception:
            # verify_token failed — try decoding the JWT payload without
            # signature verification to recover the auth_url.  This
            # handles the case where the signing key is momentarily out
            # of sync (e.g. during onboarding hot-reload).
            import base64 as _b64
            import json as _json

            try:
                parts = token.split(".")
                if len(parts) == 3:
                    padded = parts[1] + "=" * (-len(parts[1]) % 4)
                    unsigned_payload = _json.loads(
                        _b64.urlsafe_b64decode(padded)
                    )
                    url = unsigned_payload.get("auth_url", "")
                    if url.startswith(("http://", "https://")):
                        logger.warning(
                            "start_federated: verify_token failed but "
                            "decoded auth_url from unsigned JWT payload"
                        )
                        return FederatedStartResult(
                            auth_url=url,
                            code_verifier=unsigned_payload.get(
                                "code_verifier"
                            ),
                        )
            except Exception:
                pass
            # Final fallback: if the token looks like a plain URL, use it.
            if token.startswith(("http://", "https://")):
                return FederatedStartResult(auth_url=token, code_verifier=None)
            raise RuntimeError(
                "start_federated: cannot extract a valid auth_url from "
                "the authy response (verify_token failed and token is "
                "not a URL)"
            )
        auth_url = payload.get("auth_url", "")
        if not auth_url:
            raise RuntimeError("authy get_auth_url did not return an auth_url in the JWT payload")
        return FederatedStartResult(auth_url=auth_url, code_verifier=payload.get("code_verifier"))

    async def complete_federated(
        self, provider_name: str, code: str, state: str, *, code_verifier: str | None = None
    ) -> AuthResult:
        if self._underlying is None:
            return AuthResult(success=False, error="auth not configured")
        params: dict[str, Any] = {"action": "callback", "code": code, "state": state}
        if code_verifier is not None:
            params["code_verifier"] = code_verifier
        return await self._underlying.authenticate(provider_name, params)

    def verify(self, token: str) -> dict[str, Any] | None:
        """Verify a JWT and check revocation. Returns payload or None."""
        if self._underlying is None:
            return None
        try:
            payload = self._underlying.verify_token(token)
        except Exception:
            return None
        jti = payload.get("jti")
        if jti and self._store.is_jwt_revoked(str(jti)):
            return None
        return payload

    def list_enabled_providers(self) -> list[str]:
        if self._underlying is None:
            return []
        return self._underlying.list_providers()

    @staticmethod
    def hash_password(password: str) -> str:
        return hash_password(password)

    @property
    def config(self) -> AuthyConfig:
        return self._config
