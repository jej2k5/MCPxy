"""Static HTTP auth strategies (bearer / api_key / basic / none).

These are the simple auth types: given one config object, produce one
frozen dict of headers to attach to every outgoing request on the
upstream's httpx client. The transport layer wires them in at
``start()`` time and never touches them again.

OAuth2 is deliberately NOT handled here — it needs a per-request token
that can change under refresh. See :mod:`mcp_proxy.auth.oauth` for that
path, which exposes a compatible interface via the same
:class:`AuthStrategy` protocol so the transport can treat every strategy
uniformly.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol

from mcp_proxy.config import (
    ApiKeyAuthConfig,
    BasicAuthConfig,
    BearerAuthConfig,
    HttpAuthConfig,
    NoAuthConfig,
    OAuth2AuthConfig,
)


class AuthStrategy(Protocol):
    """Protocol every static auth strategy implements.

    ``static_headers()`` returns a dict of headers to merge into the
    upstream's httpx client at start-time. For non-dynamic strategies
    the return value is cached; for dynamic ones (OAuth2) the transport
    instead calls a per-request ``attach(request)`` hook — see
    :mod:`mcp_proxy.auth.oauth`.
    """

    def static_headers(self) -> dict[str, str]: ...


@dataclass
class NoAuthStrategy:
    """Sentinel strategy used when ``auth`` is absent or ``type: none``."""

    def static_headers(self) -> dict[str, str]:
        return {}


@dataclass
class BearerAuthStrategy:
    """RFC 6750 Bearer: ``Authorization: Bearer <token>``."""

    token: str

    def static_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class HeaderAuthStrategy:
    """Custom-header API key, e.g. ``X-Api-Key: <value>``."""

    header: str
    value: str

    def static_headers(self) -> dict[str, str]:
        return {self.header: self.value}


@dataclass
class BasicAuthStrategy:
    """HTTP Basic (RFC 7617): ``Authorization: Basic base64(user:pass)``."""

    username: str
    password: str

    def static_headers(self) -> dict[str, str]:
        creds = f"{self.username}:{self.password}".encode("utf-8")
        encoded = base64.b64encode(creds).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}


def build_strategy(cfg: HttpAuthConfig | None) -> AuthStrategy:
    """Turn an auth config into a static strategy.

    Raises ``NotImplementedError`` for OAuth2 config — that case is
    handled at the transport level by wiring an
    :class:`mcp_proxy.auth.oauth.OAuthAuthStrategy` instead, which
    needs a token store + refresh logic that this module deliberately
    doesn't know about.
    """
    if cfg is None or isinstance(cfg, NoAuthConfig):
        return NoAuthStrategy()
    if isinstance(cfg, BearerAuthConfig):
        return BearerAuthStrategy(token=cfg.token)
    if isinstance(cfg, ApiKeyAuthConfig):
        return HeaderAuthStrategy(header=cfg.header, value=cfg.value)
    if isinstance(cfg, BasicAuthConfig):
        return BasicAuthStrategy(username=cfg.username, password=cfg.password)
    if isinstance(cfg, OAuth2AuthConfig):
        raise NotImplementedError(
            "build_strategy does not handle oauth2 — wire via "
            "mcp_proxy.auth.oauth.OAuthTokenProvider at transport setup"
        )
    raise TypeError(f"unknown auth config type: {type(cfg).__name__}")
