"""Upstream auth subsystem.

This package holds everything related to authenticating MCPy's HTTP
upstream calls:

- ``strategies``: turning static auth config (``bearer``, ``api_key``,
  ``basic``, ``none``) into request headers.
- ``oauth``: OAuth 2.1 authorization-code + PKCE client, with discovery,
  optional RFC 7591 dynamic registration, token refresh, and a
  per-upstream persistent token store backed by
  :class:`mcp_proxy.secrets.SecretsManager`.

Everything in here operates on pydantic config models from
:mod:`mcp_proxy.config` and is consumed by
:class:`mcp_proxy.proxy.http.HttpUpstreamTransport`.
"""

from mcp_proxy.auth.strategies import (
    AuthStrategy,
    BasicAuthStrategy,
    BearerAuthStrategy,
    HeaderAuthStrategy,
    NoAuthStrategy,
    build_strategy,
)

__all__ = [
    "AuthStrategy",
    "BasicAuthStrategy",
    "BearerAuthStrategy",
    "HeaderAuthStrategy",
    "NoAuthStrategy",
    "build_strategy",
]
