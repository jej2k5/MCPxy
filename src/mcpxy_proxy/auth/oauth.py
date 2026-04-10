"""OAuth 2.1 client-side support for HTTP upstreams.

This module implements the pieces MCPxy needs to act as an OAuth 2.1
client talking to MCP servers that gate their HTTP/streamable-HTTP
endpoints behind an authorization server. Scope:

1. **Discovery**. Given an issuer URL, fetch
   ``<issuer>/.well-known/oauth-authorization-server`` (RFC 8414). If the
   config supplies explicit ``authorization_endpoint``/``token_endpoint``
   instead, those win and no discovery request is made.

2. **Dynamic client registration** (RFC 7591, optional). If the config
   has ``dynamic_registration=true`` and no ``client_id``, the manager
   POSTs to the discovered ``registration_endpoint`` once and persists
   the resulting client_id/secret as a regular MCPxy secret so subsequent
   restarts reuse the same registration.

3. **Authorization code + PKCE flow** (RFC 7636). The operator starts
   the flow from the admin UI or CLI; the manager returns an
   authorization URL that opens in a browser; the browser redirects
   back to the proxy's ``/admin/api/oauth/callback`` endpoint with
   ``code``+``state``; the manager exchanges those for an access+refresh
   token and persists the result.

4. **Refresh**. Access tokens are cached in-memory with their
   ``expires_at`` timestamp. ``OAuthManager.get_access_token()`` refreshes
   proactively if the token is within ``REFRESH_LEEWAY_S`` of expiry
   *or* reactively when an httpx 401 triggers the ``OAuthHttpxAuth``
   retry flow. The refreshed token and (rotated) refresh token are
   written back to the secrets store.

5. **Persistent storage**. Tokens and dynamic registrations live inside
   the same :class:`mcpxy_proxy.secrets.SecretsManager` used for user
   secrets, under reserved key prefixes:

   - ``oauth:token:<upstream>`` — JSON blob with access/refresh/expiry
   - ``oauth:client:<upstream>`` — JSON blob with client_id/client_secret
     when dynamic registration was used

   This means all OAuth state is encrypted at rest with the same Fernet
   key and rotates cleanly when the key rotates.

What this module deliberately does NOT implement:

- Device authorization grant (RFC 8628). Out of scope for a first pass.
- Client credentials grant (RFC 6749 §4.4). Call sites that want
  machine-to-machine should use the bearer auth type with a static
  token minted out-of-band.
- PAR (Pushed Authorization Requests, RFC 9126). Nice to have later.

All network I/O goes through a single injected ``httpx.AsyncClient`` so
tests can wire in a ``MockTransport`` and exercise every branch without
touching the network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets as py_secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import httpx

from mcpxy_proxy.config import OAuth2AuthConfig
from mcpxy_proxy.secrets import SecretsManager, SecretNotFoundError

logger = logging.getLogger(__name__)

# Refresh N seconds before actual expiry so a long-running request
# doesn't race a token that's about to expire mid-flight.
REFRESH_LEEWAY_S = 60

# Pending authorization starts live this long before we consider them
# abandoned. 15 minutes is generous but matches common auth-server
# timeouts.
AUTHORIZATION_TTL_S = 900


class OAuthError(RuntimeError):
    """Anything went wrong in an OAuth flow (discovery, token exchange, refresh)."""


class OAuthNotAuthorizedError(OAuthError):
    """No tokens for this upstream yet — operator must run the authorize flow."""


@dataclass
class TokenSet:
    """One access/refresh token pair for a single upstream."""

    access_token: str
    token_type: str = "Bearer"
    refresh_token: str | None = None
    expires_at: float | None = None
    scope: list[str] = field(default_factory=list)

    def is_expired(self, leeway: float = REFRESH_LEEWAY_S) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - leeway)

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "token_type": self.token_type,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "scope": self.scope,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "TokenSet":
        payload = json.loads(raw)
        return cls(
            access_token=str(payload["access_token"]),
            token_type=str(payload.get("token_type", "Bearer")),
            refresh_token=payload.get("refresh_token"),
            expires_at=payload.get("expires_at"),
            scope=list(payload.get("scope") or []),
        )

    @classmethod
    def from_token_response(cls, payload: dict[str, Any]) -> "TokenSet":
        access = payload.get("access_token")
        if not access:
            raise OAuthError(
                f"token endpoint response missing 'access_token': {payload!r}"
            )
        expires_in = payload.get("expires_in")
        expires_at: float | None = None
        if expires_in is not None:
            try:
                expires_at = time.time() + float(expires_in)
            except (TypeError, ValueError) as exc:
                raise OAuthError(
                    f"token endpoint returned invalid expires_in: {expires_in!r}"
                ) from exc
        scope_raw = payload.get("scope")
        scope: list[str]
        if isinstance(scope_raw, str):
            scope = scope_raw.split()
        elif isinstance(scope_raw, list):
            scope = [str(s) for s in scope_raw]
        else:
            scope = []
        return cls(
            access_token=str(access),
            token_type=str(payload.get("token_type", "Bearer")),
            refresh_token=payload.get("refresh_token"),
            expires_at=expires_at,
            scope=scope,
        )


@dataclass
class PendingAuthorization:
    """In-memory bookkeeping for an authorization flow in progress."""

    upstream: str
    state: str
    code_verifier: str
    redirect_uri: str
    config: OAuth2AuthConfig
    endpoints: "DiscoveredEndpoints"
    created_at: float

    def is_expired(self) -> bool:
        return time.time() - self.created_at > AUTHORIZATION_TTL_S


@dataclass
class DiscoveredEndpoints:
    """Resolved authorization/token/registration URLs for one config.

    Populated either from explicit config fields or from an RFC 8414
    ``.well-known/oauth-authorization-server`` response.
    """

    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None

    @classmethod
    def from_config(cls, cfg: OAuth2AuthConfig) -> "DiscoveredEndpoints | None":
        if cfg.authorization_endpoint and cfg.token_endpoint:
            return cls(
                authorization_endpoint=cfg.authorization_endpoint,
                token_endpoint=cfg.token_endpoint,
                registration_endpoint=cfg.registration_endpoint,
            )
        return None

    @classmethod
    def from_discovery(cls, cfg: OAuth2AuthConfig, payload: dict[str, Any]) -> "DiscoveredEndpoints":
        auth_url = cfg.authorization_endpoint or payload.get("authorization_endpoint")
        token_url = cfg.token_endpoint or payload.get("token_endpoint")
        reg_url = cfg.registration_endpoint or payload.get("registration_endpoint")
        if not auth_url or not token_url:
            raise OAuthError(
                "discovery document missing authorization_endpoint or token_endpoint"
            )
        return cls(
            authorization_endpoint=str(auth_url),
            token_endpoint=str(token_url),
            registration_endpoint=str(reg_url) if reg_url else None,
        )


def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636 §4.1/§4.2."""
    # High-entropy url-safe string, 43-128 chars.
    verifier_bytes = py_secrets.token_bytes(48)
    verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _random_state() -> str:
    return py_secrets.token_urlsafe(32)


class OAuthManager:
    """Per-process OAuth coordinator shared across all HTTP upstreams.

    Responsibilities:

    - Resolve and cache endpoints (discovery or config).
    - Run dynamic client registration once per upstream if enabled.
    - Run authorize/callback flows.
    - Hand out valid access tokens on demand, refreshing as needed.
    - Persist tokens + dynamic client credentials via SecretsManager.
    """

    # Internal secret names use a ``__`` prefix so they sort out of the
    # user-visible ``/admin/api/secrets`` listing (SecretsManager hides
    # ``__``-prefixed entries from ``list_public``) and because the
    # secret-name regex forbids colons — we can't use ``oauth:token:foo``.
    TOKEN_SECRET_PREFIX = "__oauth_token__"
    CLIENT_SECRET_PREFIX = "__oauth_client__"

    def __init__(
        self,
        secrets: SecretsManager,
        http_client: httpx.AsyncClient | None = None,
        default_redirect_uri: str = "http://127.0.0.1:8000/admin/api/oauth/callback",
    ) -> None:
        self._secrets = secrets
        # A dedicated short-timeout client for auth-server chatter. We
        # deliberately don't reuse upstream transports' clients so the
        # per-upstream timeout / headers don't leak into the OAuth dance.
        self._http = http_client or httpx.AsyncClient(timeout=15.0)
        self._own_http = http_client is None
        self.default_redirect_uri = default_redirect_uri
        self._configs: dict[str, OAuth2AuthConfig] = {}
        self._endpoints: dict[str, DiscoveredEndpoints] = {}
        self._tokens: dict[str, TokenSet] = {}
        self._pending: dict[str, PendingAuthorization] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Config registration
    # ------------------------------------------------------------------

    def register_upstream(self, upstream: str, cfg: OAuth2AuthConfig) -> None:
        """Record that ``upstream`` uses ``cfg`` for OAuth.

        Safe to call multiple times; overwrites previous registrations
        (config hot-reload path). Clears any cached pending authorizations
        but keeps the token cache — the refresh token may still be valid.
        """
        self._configs[upstream] = cfg
        self._endpoints.pop(upstream, None)
        self._pending = {s: p for s, p in self._pending.items() if p.upstream != upstream}
        # Best-effort warm load of any persisted tokens.
        try:
            raw = self._secrets.get(f"{self.TOKEN_SECRET_PREFIX}{upstream}")
        except SecretNotFoundError:
            raw = None
        if raw:
            try:
                self._tokens[upstream] = TokenSet.from_json(raw)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "oauth: could not parse persisted tokens for %s: %s", upstream, exc
                )

    def unregister_upstream(self, upstream: str) -> None:
        self._configs.pop(upstream, None)
        self._endpoints.pop(upstream, None)
        self._tokens.pop(upstream, None)
        self._pending = {s: p for s, p in self._pending.items() if p.upstream != upstream}

    # ------------------------------------------------------------------
    # Endpoint resolution + dynamic registration
    # ------------------------------------------------------------------

    async def _resolve_endpoints(self, upstream: str) -> DiscoveredEndpoints:
        cached = self._endpoints.get(upstream)
        if cached is not None:
            return cached
        cfg = self._require_config(upstream)
        endpoints = DiscoveredEndpoints.from_config(cfg)
        if endpoints is None:
            if not cfg.issuer:
                raise OAuthError(
                    f"upstream {upstream!r}: oauth2 config has no endpoints "
                    "and no issuer"
                )
            discovery_url = cfg.issuer.rstrip("/") + "/.well-known/oauth-authorization-server"
            resp = await self._http.get(discovery_url)
            if resp.status_code >= 400:
                raise OAuthError(
                    f"oauth discovery for {upstream!r} at {discovery_url} "
                    f"failed: {resp.status_code}"
                )
            try:
                payload = resp.json()
            except ValueError as exc:
                raise OAuthError(
                    f"oauth discovery for {upstream!r} returned non-JSON"
                ) from exc
            endpoints = DiscoveredEndpoints.from_discovery(cfg, payload)
        self._endpoints[upstream] = endpoints
        return endpoints

    async def _ensure_client_credentials(
        self, upstream: str
    ) -> tuple[str, str | None]:
        """Return (client_id, client_secret) for ``upstream``.

        If the config supplies them, use them as-is. Otherwise, if
        dynamic_registration is enabled, hit the registration endpoint,
        persist the result in the secrets store, and cache in-memory.
        """
        cfg = self._require_config(upstream)
        if cfg.client_id:
            return cfg.client_id, cfg.client_secret

        if not cfg.dynamic_registration:
            raise OAuthError(
                f"upstream {upstream!r}: oauth2 config has no client_id and "
                "dynamic_registration is disabled"
            )

        # Look up persisted registration first.
        try:
            stored_raw = self._secrets.get(f"{self.CLIENT_SECRET_PREFIX}{upstream}")
        except SecretNotFoundError:
            stored_raw = None
        if stored_raw:
            try:
                stored = json.loads(stored_raw)
                return str(stored["client_id"]), stored.get("client_secret")
            except (ValueError, KeyError):
                # Fall through and re-register.
                pass

        endpoints = await self._resolve_endpoints(upstream)
        if not endpoints.registration_endpoint:
            raise OAuthError(
                f"upstream {upstream!r}: dynamic_registration requires a "
                "registration_endpoint but discovery did not provide one"
            )

        body: dict[str, Any] = {
            "client_name": f"MCPxy Proxy ({upstream})",
            "redirect_uris": [cfg.redirect_uri or self.default_redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",  # PKCE-first public client
        }
        if cfg.scopes:
            body["scope"] = " ".join(cfg.scopes)
        resp = await self._http.post(endpoints.registration_endpoint, json=body)
        if resp.status_code >= 400:
            raise OAuthError(
                f"dynamic client registration for {upstream!r} failed: "
                f"{resp.status_code} {resp.text[:200]}"
            )
        payload = resp.json()
        client_id = payload.get("client_id")
        if not client_id:
            raise OAuthError(
                f"dynamic client registration for {upstream!r} returned no client_id"
            )
        client_secret = payload.get("client_secret")
        await self._secrets.set(
            f"{self.CLIENT_SECRET_PREFIX}{upstream}",
            json.dumps({"client_id": client_id, "client_secret": client_secret}),
            description=f"OAuth dynamic client registration for upstream {upstream}",
        )
        return str(client_id), client_secret

    # ------------------------------------------------------------------
    # Authorization flow
    # ------------------------------------------------------------------

    async def start_authorization(
        self,
        upstream: str,
        *,
        redirect_uri: str | None = None,
    ) -> dict[str, Any]:
        """Begin an OAuth authorization-code + PKCE flow for ``upstream``.

        Returns a dict with ``authorization_url`` that the operator should
        open in a browser. The browser eventually redirects back to
        ``redirect_uri`` with ``code`` + ``state`` which the admin API
        feeds into :meth:`finish_authorization`.
        """
        cfg = self._require_config(upstream)
        endpoints = await self._resolve_endpoints(upstream)
        client_id, _client_secret = await self._ensure_client_credentials(upstream)

        redirect = redirect_uri or cfg.redirect_uri or self.default_redirect_uri
        verifier, challenge = _generate_pkce_pair()
        state = _random_state()

        params: dict[str, str] = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if cfg.scopes:
            params["scope"] = " ".join(cfg.scopes)
        if cfg.audience:
            params["audience"] = cfg.audience

        parsed = urlparse(endpoints.authorization_endpoint)
        query = urlencode(params, doseq=False)
        authorization_url = urlunparse(parsed._replace(query=query))

        self._pending[state] = PendingAuthorization(
            upstream=upstream,
            state=state,
            code_verifier=verifier,
            redirect_uri=redirect,
            config=cfg,
            endpoints=endpoints,
            created_at=time.time(),
        )
        # Opportunistic GC of abandoned flows.
        self._pending = {
            k: p for k, p in self._pending.items() if not p.is_expired()
        }

        return {
            "upstream": upstream,
            "authorization_url": authorization_url,
            "state": state,
            "redirect_uri": redirect,
            "expires_in": AUTHORIZATION_TTL_S,
        }

    async def finish_authorization(self, state: str, code: str) -> TokenSet:
        """Exchange ``code`` for an access token and persist the result."""
        pending = self._pending.pop(state, None)
        if pending is None:
            raise OAuthError(
                "no pending authorization matches this state — "
                "either already completed or expired"
            )
        if pending.is_expired():
            raise OAuthError("authorization flow expired; please restart")

        client_id, client_secret = await self._ensure_client_credentials(pending.upstream)
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "client_id": client_id,
            "code_verifier": pending.code_verifier,
        }
        if client_secret:
            data["client_secret"] = client_secret

        resp = await self._http.post(
            pending.endpoints.token_endpoint,
            data=data,
            headers={"Accept": "application/json"},
        )
        if resp.status_code >= 400:
            raise OAuthError(
                f"token exchange for {pending.upstream!r} failed: "
                f"{resp.status_code} {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OAuthError("token endpoint returned non-JSON") from exc
        token = TokenSet.from_token_response(payload)
        await self._persist_tokens(pending.upstream, token)
        return token

    async def revoke_tokens(self, upstream: str) -> bool:
        """Forget locally-stored tokens for ``upstream``.

        We don't call the auth server's revocation endpoint (RFC 7009)
        because it's optional and often unimplemented in practice; the
        local drop is enough to force a fresh authorize flow.
        """
        had_tokens = upstream in self._tokens
        self._tokens.pop(upstream, None)
        if self._secrets.exists(f"{self.TOKEN_SECRET_PREFIX}{upstream}"):
            await self._secrets.delete(f"{self.TOKEN_SECRET_PREFIX}{upstream}")
            had_tokens = True
        return had_tokens

    # ------------------------------------------------------------------
    # Token issuance for the transport layer
    # ------------------------------------------------------------------

    async def get_access_token(self, upstream: str) -> str:
        """Return a valid access token for ``upstream``, refreshing if needed.

        Raises :class:`OAuthNotAuthorizedError` if the operator hasn't
        completed the authorize flow yet — the caller (usually
        :class:`OAuthHttpxAuth`) must surface that as a 401 to the
        downstream client so they know to link the upstream.
        """
        token = self._tokens.get(upstream)
        if token is None:
            raise OAuthNotAuthorizedError(
                f"upstream {upstream!r} has no OAuth tokens; run the "
                "authorize flow first via POST /admin/api/oauth/{upstream}/start"
            )
        if not token.is_expired():
            return token.access_token
        refreshed = await self._refresh(upstream, token)
        return refreshed.access_token

    async def _refresh(self, upstream: str, current: TokenSet) -> TokenSet:
        """Run the refresh_token grant. Serialised per-upstream via a lock
        so concurrent requests don't fire N parallel refresh exchanges."""
        if current.refresh_token is None:
            raise OAuthNotAuthorizedError(
                f"upstream {upstream!r} access token expired and no refresh "
                "token is available; re-run the authorize flow"
            )
        lock = self._refresh_locks.setdefault(upstream, asyncio.Lock())
        async with lock:
            # Double-check: another coroutine may have refreshed while
            # we were waiting for the lock.
            cached = self._tokens.get(upstream)
            if cached and not cached.is_expired():
                return cached
            endpoints = await self._resolve_endpoints(upstream)
            client_id, client_secret = await self._ensure_client_credentials(upstream)
            data: dict[str, str] = {
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
                "client_id": client_id,
            }
            if client_secret:
                data["client_secret"] = client_secret
            resp = await self._http.post(
                endpoints.token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
            )
            if resp.status_code >= 400:
                # Refresh failure is usually fatal — the refresh token was
                # revoked on the server side. Drop the stored tokens so
                # the operator has to re-authorize.
                await self.revoke_tokens(upstream)
                raise OAuthNotAuthorizedError(
                    f"refresh for {upstream!r} failed ({resp.status_code}); "
                    "re-run the authorize flow"
                )
            payload = resp.json()
            token = TokenSet.from_token_response(payload)
            # Auth servers that don't rotate refresh tokens omit them
            # from the refresh response — preserve the previous one so
            # we can refresh again later.
            if token.refresh_token is None:
                token.refresh_token = current.refresh_token
            await self._persist_tokens(upstream, token)
            return token

    async def _persist_tokens(self, upstream: str, token: TokenSet) -> None:
        self._tokens[upstream] = token
        await self._secrets.set(
            f"{self.TOKEN_SECRET_PREFIX}{upstream}",
            token.to_json(),
            description=f"OAuth tokens for upstream {upstream}",
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def status(self, upstream: str) -> dict[str, Any]:
        cfg = self._configs.get(upstream)
        token = self._tokens.get(upstream)
        return {
            "upstream": upstream,
            "configured": cfg is not None,
            "has_token": token is not None,
            "expires_at": token.expires_at if token else None,
            "expired": token.is_expired() if token else None,
            "scope": token.scope if token else [],
            "refresh_available": bool(token and token.refresh_token),
        }

    def _require_config(self, upstream: str) -> OAuth2AuthConfig:
        cfg = self._configs.get(upstream)
        if cfg is None:
            raise OAuthError(
                f"upstream {upstream!r} has no registered oauth2 config"
            )
        return cfg


class OAuthHttpxAuth(httpx.Auth):
    """``httpx.Auth`` implementation that asks the OAuth manager for a
    fresh access token on every outgoing request.

    If the first attempt comes back 401 we clear the in-memory token
    cache for that upstream and retry once with a fresh token (which
    triggers a refresh, or raises if no refresh token is available).
    """

    requires_response_body = False

    def __init__(self, manager: OAuthManager, upstream: str) -> None:
        self._manager = manager
        self._upstream = upstream

    def sync_auth_flow(self, request: httpx.Request):  # pragma: no cover - async-only
        raise RuntimeError(
            "OAuthHttpxAuth only supports async httpx clients"
        )

    async def async_auth_flow(self, request: httpx.Request):
        token = await self._manager.get_access_token(self._upstream)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            # The upstream rejected our access token. Force-expire the
            # in-memory copy so the next get_access_token() call runs a
            # refresh (rather than dropping it entirely, which would
            # strand us without a refresh_token to use).
            cached = self._manager._tokens.get(self._upstream)
            if cached is not None:
                cached.expires_at = 0.0
            token = await self._manager.get_access_token(self._upstream)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request


__all__ = [
    "OAuthManager",
    "OAuthHttpxAuth",
    "OAuthError",
    "OAuthNotAuthorizedError",
    "TokenSet",
    "DiscoveredEndpoints",
    "REFRESH_LEEWAY_S",
]
