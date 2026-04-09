"""HTTP upstream transport plugin."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from mcp_proxy.auth.strategies import NoAuthStrategy, build_strategy
from mcp_proxy.config import (
    HttpUpstreamConfig,
    HttpUpstreamTlsConfig,
    OAuth2AuthConfig,
)
from mcp_proxy.proxy.base import UpstreamTransport

logger = logging.getLogger(__name__)


class HttpUpstreamTransport(UpstreamTransport):
    """JSON-RPC transport over HTTP POST.

    Auth model:

    * ``settings['headers']`` (dict) — free-form static headers merged
      verbatim into the httpx client at start-time. Use this for non-auth
      headers like ``X-Workspace-Id``, ``User-Agent``, etc.
    * ``settings['auth']`` (HttpAuthConfig) — structured auth. Simple
      types (``bearer``, ``api_key``, ``basic``, ``none``) are resolved
      into static headers here and merged with the above. ``oauth2`` is
      registered as a *dynamic* strategy on the shared
      :class:`mcp_proxy.auth.oauth.OAuthManager` (if one was supplied via
      ``settings['_oauth_manager']``) and attached per-request via an
      httpx Auth object that refreshes tokens on expiry or 401 — see
      :meth:`_make_oauth_client`.

    Because the transport is constructed by
    :class:`mcp_proxy.proxy.manager.UpstreamManager` from the plain
    config dict, the runtime threads the OAuth manager in via a
    settings-dict side channel (``_oauth_manager``) that's scrubbed
    before ``model_validate`` would see it.
    """

    def __init__(self, name: str, settings: dict[str, Any]) -> None:
        self.name = name
        self.url = settings["url"]
        self.timeout_s = float(settings.get("timeout_s", 30.0))
        self.static_headers: dict[str, str] = {
            str(k): str(v) for k, v in (settings.get("headers") or {}).items()
        }
        # Auth config arrives as a pydantic model (via manager._build_transport
        # after AppConfig validation) or as a plain dict from tests. Normalise
        # to a model once so we can pattern-match cleanly.
        raw_auth = settings.get("auth")
        self.auth_config = self._coerce_auth(raw_auth)
        self.tls_config = self._coerce_tls(settings.get("tls"))
        self._oauth_manager = settings.get("_oauth_manager")
        self._auth_strategy = None  # lazily bound in start()
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Config normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_auth(raw: Any) -> Any:
        if raw is None:
            return None
        if hasattr(raw, "type"):
            # Already a pydantic config object.
            return raw
        # Fall back to model validation so tests can pass a plain dict.
        from pydantic import TypeAdapter

        from mcp_proxy.config import HttpAuthConfig

        return TypeAdapter(HttpAuthConfig).validate_python(raw)

    @staticmethod
    def _coerce_tls(raw: Any) -> HttpUpstreamTlsConfig | None:
        if raw is None:
            return None
        if isinstance(raw, HttpUpstreamTlsConfig):
            return raw
        return HttpUpstreamTlsConfig.model_validate(raw)

    # ------------------------------------------------------------------
    # httpx kwarg builders
    # ------------------------------------------------------------------

    def _build_tls_kwargs(self) -> dict[str, Any]:
        """Translate :class:`HttpUpstreamTlsConfig` into ``httpx.AsyncClient`` kwargs.

        ``verify`` maps straight through. ``cert`` takes httpx's
        ``(cert, key)`` or ``(cert, key, password)`` tuple shape, or a
        bare cert path when there's no separate key. Missing files fail
        fast at ``start()`` time so operators get a clean RuntimeError
        instead of a late connection-reset when the first request fires.
        """
        tls = self.tls_config
        if tls is None:
            return {}

        kwargs: dict[str, Any] = {}

        if tls.verify is False:
            logger.warning(
                "upstream %r: TLS verification disabled — "
                "connections to %s will accept any certificate",
                self.name,
                self.url,
            )
            kwargs["verify"] = False
        elif isinstance(tls.verify, str):
            ca_path = Path(tls.verify)
            if not ca_path.is_file():
                raise RuntimeError(
                    f"upstream {self.name!r}: tls.verify CA bundle not found: {tls.verify}"
                )
            kwargs["verify"] = str(ca_path)
        # verify is True (default) → httpx uses the system CA bundle; no
        # kwarg needed.

        if tls.client_cert:
            cert_path = Path(tls.client_cert)
            if not cert_path.is_file():
                raise RuntimeError(
                    f"upstream {self.name!r}: tls.client_cert not found: {tls.client_cert}"
                )
            if tls.client_key:
                key_path = Path(tls.client_key)
                if not key_path.is_file():
                    raise RuntimeError(
                        f"upstream {self.name!r}: tls.client_key not found: {tls.client_key}"
                    )
                if tls.client_key_password:
                    kwargs["cert"] = (
                        str(cert_path),
                        str(key_path),
                        tls.client_key_password,
                    )
                else:
                    kwargs["cert"] = (str(cert_path), str(key_path))
            else:
                # Combined cert+key PEM.
                kwargs["cert"] = str(cert_path)

        return kwargs

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        merged_headers = dict(self.static_headers)
        auth_obj: httpx.Auth | None = None

        if isinstance(self.auth_config, OAuth2AuthConfig):
            if self._oauth_manager is None:
                raise RuntimeError(
                    f"upstream {self.name!r}: oauth2 auth requires an "
                    "OAuthManager wired through the runtime; none was supplied"
                )
            # Deferred import to avoid a startup cycle with secrets store
            # construction in tests that don't exercise oauth at all.
            from mcp_proxy.auth.oauth import OAuthHttpxAuth

            auth_obj = OAuthHttpxAuth(
                manager=self._oauth_manager, upstream=self.name
            )
        elif self.auth_config is not None:
            self._auth_strategy = build_strategy(self.auth_config)
            merged_headers.update(self._auth_strategy.static_headers())
        else:
            self._auth_strategy = NoAuthStrategy()

        tls_kwargs = self._build_tls_kwargs()

        self._client = httpx.AsyncClient(
            timeout=self.timeout_s,
            headers=merged_headers or None,
            auth=auth_obj,
            **tls_kwargs,
        )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
        self._client = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # ------------------------------------------------------------------
    # Request path
    # ------------------------------------------------------------------

    async def request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not self._client:
            raise RuntimeError("http transport not started")
        resp = await self._client.post(self.url, json=message)
        if not resp.content:
            return None
        return resp.json()

    async def send_notification(self, message: dict[str, Any]) -> None:
        if not self._client:
            raise RuntimeError("http transport not started")
        await self._client.post(self.url, json=message)

    def health(self) -> dict[str, Any]:
        auth_type = "none"
        if self.auth_config is not None:
            auth_type = getattr(self.auth_config, "type", type(self.auth_config).__name__)
        tls_state: dict[str, Any] | None = None
        if self.tls_config is not None:
            tls_state = {
                "verify": self.tls_config.verify,
                "mtls": bool(self.tls_config.client_cert),
            }
        return {
            "type": "http",
            "url": self.url,
            "started": self._client is not None,
            "auth": auth_type,
            "tls": tls_state,
        }
