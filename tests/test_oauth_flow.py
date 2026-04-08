"""OAuth 2.1 end-to-end tests for MCPy's auth layer.

We spin up a fully-featured mock OAuth 2.1 authorization server
inside an in-memory httpx.MockTransport so the tests cover:

- RFC 8414 .well-known discovery
- RFC 7591 dynamic client registration
- Authorization code + PKCE code exchange
- Access token use via the OAuthHttpxAuth httpx auth object
- Refresh token grant, including the 401-triggered reactive refresh
- Rotation vs. non-rotation of refresh tokens
- Persistence via SecretsManager across a full OAuthManager rebuild
- Missing-auth behaviour when no flow has been run yet

Everything happens in-process — no real network, no sockets, no sleeps.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from mcp_proxy.auth.oauth import (
    OAuthError,
    OAuthHttpxAuth,
    OAuthManager,
    OAuthNotAuthorizedError,
    TokenSet,
)
from mcp_proxy.config import OAuth2AuthConfig
from mcp_proxy.secrets import SecretsManager


# ---------------------------------------------------------------------------
# Mock auth server
# ---------------------------------------------------------------------------


@dataclass
class MockAuthServer:
    """Tiny RFC 6749 / 7636 / 7591 / 8414 authorization server.

    Supports the subset MCPy exercises: discovery, dynamic client
    registration, authorization_code + PKCE grant, refresh_token grant.
    Generates new tokens with configurable TTL and optionally rotates
    refresh tokens on refresh.
    """

    issuer: str = "https://auth.test"
    access_ttl_s: int = 3600
    rotate_refresh: bool = True
    registered_clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_refresh_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    access_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    counter: int = 0
    expected_challenge: str | None = None
    refresh_calls: int = 0

    def _next(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}_{self.counter}"

    # --- HTTP endpoints used by the handler router ---------------------

    def handle_discovery(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/oauth/authorize",
                "token_endpoint": f"{self.issuer}/oauth/token",
                "registration_endpoint": f"{self.issuer}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
            },
        )

    def handle_register(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        client_id = self._next("client")
        client_secret = self._next("csecret")
        self.registered_clients[client_id] = {"secret": client_secret, "meta": body}
        return httpx.Response(
            201,
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "client_name": body.get("client_name"),
                "redirect_uris": body.get("redirect_uris", []),
            },
        )

    def simulate_authorize(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        code_challenge: str,
    ) -> str:
        """Not called over HTTP in these tests — the MCPy manager just
        builds the URL. Instead the test harness calls this directly to
        simulate the user approving the consent screen and the auth
        server issuing a code."""
        self.expected_challenge = code_challenge
        code = self._next("code")
        self.pending_codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "state": state,
        }
        return code

    def handle_token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("utf-8"))
        grant = form.get("grant_type", [""])[0]
        if grant == "authorization_code":
            code = form.get("code", [""])[0]
            pending = self.pending_codes.pop(code, None)
            if pending is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", [""])[0]
            digest = hashlib.sha256(verifier.encode("ascii")).digest()
            challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            if challenge != pending["code_challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "detail": "pkce_mismatch"})
            return self._issue_token_pair()
        if grant == "refresh_token":
            self.refresh_calls += 1
            refresh = form.get("refresh_token", [""])[0]
            existing = self.active_refresh_tokens.get(refresh)
            if existing is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            # The old refresh token is consumed if we rotate.
            if self.rotate_refresh:
                self.active_refresh_tokens.pop(refresh, None)
            return self._issue_token_pair(previous_refresh=refresh)
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _issue_token_pair(self, *, previous_refresh: str | None = None) -> httpx.Response:
        access = self._next("at")
        refresh = self._next("rt") if self.rotate_refresh or previous_refresh is None else previous_refresh
        self.access_tokens[access] = {"refresh": refresh}
        self.active_refresh_tokens[refresh] = {"access": access}
        body: dict[str, Any] = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.access_ttl_s,
            "scope": "read write",
        }
        if self.rotate_refresh or previous_refresh is None:
            body["refresh_token"] = refresh
        return httpx.Response(200, json=body)


def _build_transport_router(auth: MockAuthServer) -> httpx.MockTransport:
    def router(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/oauth-authorization-server"):
            return auth.handle_discovery(request)
        if url.endswith("/oauth/register"):
            return auth.handle_register(request)
        if url.endswith("/oauth/token"):
            return auth.handle_token(request)
        return httpx.Response(404, json={"error": "unknown_endpoint", "url": url})

    return httpx.MockTransport(router)


def _build_manager(
    tmp_path: Path, auth: MockAuthServer
) -> tuple[OAuthManager, SecretsManager]:
    secrets = SecretsManager(
        state_dir=tmp_path / "state", key_override=SecretsManager.generate_key()
    )
    http_client = httpx.AsyncClient(transport=_build_transport_router(auth))
    manager = OAuthManager(secrets=secrets, http_client=http_client)
    return manager, secrets


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_resolves_endpoints(tmp_path: Path) -> None:
    auth = MockAuthServer()
    manager, _ = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(type="oauth2", issuer=auth.issuer, client_id="c", scopes=["read"])
    manager.register_upstream("u", cfg)
    endpoints = await manager._resolve_endpoints("u")
    assert endpoints.authorization_endpoint == f"{auth.issuer}/oauth/authorize"
    assert endpoints.token_endpoint == f"{auth.issuer}/oauth/token"
    assert endpoints.registration_endpoint == f"{auth.issuer}/oauth/register"
    await manager.aclose()


@pytest.mark.asyncio
async def test_manual_endpoints_skip_discovery(tmp_path: Path) -> None:
    auth = MockAuthServer()
    manager, _ = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(
        type="oauth2",
        authorization_endpoint="https://a.example/authorize",
        token_endpoint="https://a.example/token",
        client_id="c",
    )
    manager.register_upstream("u", cfg)
    endpoints = await manager._resolve_endpoints("u")
    assert endpoints.authorization_endpoint == "https://a.example/authorize"
    assert endpoints.token_endpoint == "https://a.example/token"
    await manager.aclose()


# ---------------------------------------------------------------------------
# Full authorize / token exchange round trip
# ---------------------------------------------------------------------------


async def _run_authorize_flow(
    manager: OAuthManager, auth: MockAuthServer, upstream: str
) -> TokenSet:
    start = await manager.start_authorization(upstream)
    authorization_url = start["authorization_url"]
    parsed = urlparse(authorization_url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    # Simulate the user approving the consent screen.
    code = auth.simulate_authorize(
        client_id=params["client_id"],
        redirect_uri=params["redirect_uri"],
        state=params["state"],
        code_challenge=params["code_challenge"],
    )
    return await manager.finish_authorization(state=params["state"], code=code)


@pytest.mark.asyncio
async def test_full_authorization_code_pkce_flow(tmp_path: Path) -> None:
    auth = MockAuthServer()
    manager, secrets = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(
        type="oauth2", issuer=auth.issuer, client_id="static_client"
    )
    manager.register_upstream("notion", cfg)

    token = await _run_authorize_flow(manager, auth, "notion")
    assert token.access_token.startswith("at_")
    assert token.refresh_token and token.refresh_token.startswith("rt_")
    assert "read" in token.scope
    # get_access_token returns the same (non-expired) access token.
    assert await manager.get_access_token("notion") == token.access_token
    # Persisted: a new manager with the same secrets file recovers the token.
    await manager.aclose()

    manager2 = OAuthManager(
        secrets=secrets,
        http_client=httpx.AsyncClient(transport=_build_transport_router(auth)),
    )
    manager2.register_upstream("notion", cfg)
    assert await manager2.get_access_token("notion") == token.access_token
    await manager2.aclose()


@pytest.mark.asyncio
async def test_pkce_mismatch_is_rejected(tmp_path: Path) -> None:
    auth = MockAuthServer()
    manager, _ = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(type="oauth2", issuer=auth.issuer, client_id="c")
    manager.register_upstream("u", cfg)
    start = await manager.start_authorization("u")
    parsed = urlparse(start["authorization_url"])
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    # Simulate the auth server issuing a code with a WRONG challenge.
    code = auth.simulate_authorize(
        client_id=params["client_id"],
        redirect_uri=params["redirect_uri"],
        state=params["state"],
        code_challenge="wrong" * 10,
    )
    with pytest.raises(OAuthError, match="token exchange"):
        await manager.finish_authorization(state=params["state"], code=code)
    await manager.aclose()


@pytest.mark.asyncio
async def test_state_mismatch_is_rejected(tmp_path: Path) -> None:
    auth = MockAuthServer()
    manager, _ = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(type="oauth2", issuer=auth.issuer, client_id="c")
    manager.register_upstream("u", cfg)
    await manager.start_authorization("u")
    with pytest.raises(OAuthError, match="no pending authorization"):
        await manager.finish_authorization(state="garbage", code="code")
    await manager.aclose()


# ---------------------------------------------------------------------------
# Dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_client_registration(tmp_path: Path) -> None:
    auth = MockAuthServer()
    manager, secrets = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(
        type="oauth2", issuer=auth.issuer, dynamic_registration=True
    )
    manager.register_upstream("dyn", cfg)
    token = await _run_authorize_flow(manager, auth, "dyn")
    assert token.access_token.startswith("at_")
    # The registered client id is persisted in the secrets store.
    stored = secrets.get("__oauth_client__dyn")
    assert stored is not None
    payload = json.loads(stored)
    assert payload["client_id"] in auth.registered_clients
    await manager.aclose()


@pytest.mark.asyncio
async def test_dynamic_registration_reuses_persisted_client(tmp_path: Path) -> None:
    auth = MockAuthServer()
    manager, secrets = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(
        type="oauth2", issuer=auth.issuer, dynamic_registration=True
    )
    manager.register_upstream("dyn", cfg)
    await _run_authorize_flow(manager, auth, "dyn")
    first_clients = set(auth.registered_clients)
    # Second authorize flow must reuse the existing client_id from the
    # secrets store — no new POST to /oauth/register.
    await _run_authorize_flow(manager, auth, "dyn")
    assert set(auth.registered_clients) == first_clients
    await manager.aclose()


# ---------------------------------------------------------------------------
# Refresh (proactive via expiry, reactive via 401)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proactive_refresh_on_expiry(tmp_path: Path) -> None:
    auth = MockAuthServer(access_ttl_s=1)
    manager, _ = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(type="oauth2", issuer=auth.issuer, client_id="c")
    manager.register_upstream("u", cfg)

    token = await _run_authorize_flow(manager, auth, "u")
    original_access = token.access_token
    # Force the stored token to look expired. is_expired() takes a leeway,
    # so just predate expires_at into the past.
    manager._tokens["u"].expires_at = time.time() - 100
    new_access = await manager.get_access_token("u")
    assert new_access != original_access
    assert auth.refresh_calls == 1
    await manager.aclose()


@pytest.mark.asyncio
async def test_refresh_without_rotation_preserves_refresh_token(tmp_path: Path) -> None:
    auth = MockAuthServer(access_ttl_s=1, rotate_refresh=False)
    manager, _ = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(type="oauth2", issuer=auth.issuer, client_id="c")
    manager.register_upstream("u", cfg)
    token = await _run_authorize_flow(manager, auth, "u")
    original_refresh = token.refresh_token
    manager._tokens["u"].expires_at = time.time() - 100
    await manager.get_access_token("u")
    assert manager._tokens["u"].refresh_token == original_refresh
    await manager.aclose()


@pytest.mark.asyncio
async def test_refresh_failure_forces_re_authorize(tmp_path: Path) -> None:
    auth = MockAuthServer(access_ttl_s=1)
    manager, _ = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(type="oauth2", issuer=auth.issuer, client_id="c")
    manager.register_upstream("u", cfg)
    token = await _run_authorize_flow(manager, auth, "u")
    # Invalidate the refresh token on the server side.
    auth.active_refresh_tokens.clear()
    manager._tokens["u"].expires_at = time.time() - 100
    with pytest.raises(OAuthNotAuthorizedError):
        await manager.get_access_token("u")
    # Tokens for the upstream must be wiped on fatal refresh failure.
    assert "u" not in manager._tokens
    await manager.aclose()


# ---------------------------------------------------------------------------
# OAuthHttpxAuth integration: attaches Bearer, refreshes on 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_httpx_auth_attaches_bearer_and_refreshes_on_401(
    tmp_path: Path,
) -> None:
    auth = MockAuthServer()
    manager, _ = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(type="oauth2", issuer=auth.issuer, client_id="c")
    manager.register_upstream("up", cfg)
    await _run_authorize_flow(manager, auth, "up")

    first_access = manager._tokens["up"].access_token
    seen: list[str] = []
    counter = {"n": 0}

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        counter["n"] += 1
        if counter["n"] == 1:
            return httpx.Response(401, json={"error": "token expired"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler),
        auth=OAuthHttpxAuth(manager=manager, upstream="up"),
    )
    resp = await client.post(
        "https://api.example/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    await client.aclose()
    assert resp.status_code == 200
    # First request used the original bearer, second used a refreshed one.
    assert seen[0] == f"Bearer {first_access}"
    assert seen[1].startswith("Bearer ")
    assert seen[1] != seen[0]
    await manager.aclose()


@pytest.mark.asyncio
async def test_get_access_token_raises_without_authorization(tmp_path: Path) -> None:
    auth = MockAuthServer()
    manager, _ = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(type="oauth2", issuer=auth.issuer, client_id="c")
    manager.register_upstream("u", cfg)
    with pytest.raises(OAuthNotAuthorizedError):
        await manager.get_access_token("u")
    await manager.aclose()


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_tokens_clears_cache_and_storage(tmp_path: Path) -> None:
    auth = MockAuthServer()
    manager, secrets = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(type="oauth2", issuer=auth.issuer, client_id="c")
    manager.register_upstream("u", cfg)
    await _run_authorize_flow(manager, auth, "u")
    assert manager._tokens.get("u") is not None
    assert secrets.exists("__oauth_token__u")
    removed = await manager.revoke_tokens("u")
    assert removed is True
    assert "u" not in manager._tokens
    assert not secrets.exists("__oauth_token__u")
    await manager.aclose()


# ---------------------------------------------------------------------------
# Status introspection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_before_and_after_flow(tmp_path: Path) -> None:
    auth = MockAuthServer()
    manager, _ = _build_manager(tmp_path, auth)
    cfg = OAuth2AuthConfig(type="oauth2", issuer=auth.issuer, client_id="c")
    manager.register_upstream("u", cfg)
    before = manager.status("u")
    assert before["configured"] is True
    assert before["has_token"] is False
    await _run_authorize_flow(manager, auth, "u")
    after = manager.status("u")
    assert after["has_token"] is True
    assert after["refresh_available"] is True
    assert after["expired"] is False
    await manager.aclose()
