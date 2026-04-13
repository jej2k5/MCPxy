"""HTTP upstream transport plugin."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from mcpxy_proxy.auth.strategies import NoAuthStrategy, build_strategy
from mcpxy_proxy.config import (
    HttpUpstreamConfig,
    HttpUpstreamTlsConfig,
    OAuth2AuthConfig,
    TokenTransformConfig,
)
from mcpxy_proxy.proxy.base import UpstreamTransport

if TYPE_CHECKING:
    from mcpxy_proxy.proxy.bridge import RequestContext

logger = logging.getLogger(__name__)


def _root_cause(exc: BaseException) -> str:
    """Walk the exception chain to find the most useful root-cause message.

    httpx wraps OS-level errors (ConnectionRefusedError, gaierror, etc.)
    inside generic ``ConnectError: All connection attempts failed`` layers.
    This unwraps through ``__cause__`` and ``__context__`` to surface the
    actual error the operator needs to see.
    """
    deepest = exc
    seen: set[int] = {id(exc)}
    current: BaseException | None = exc
    while current is not None:
        nxt = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        deepest = nxt
        current = nxt
    root_msg = str(deepest)
    root_type = type(deepest).__name__
    if root_msg and root_type not in root_msg:
        return f"{root_type}: {root_msg}"
    return root_msg or f"{root_type}"


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
      :class:`mcpxy_proxy.auth.oauth.OAuthManager` (if one was supplied via
      ``settings['_oauth_manager']``) and attached per-request via an
      httpx Auth object that refreshes tokens on expiry or 401 — see
      :meth:`_make_oauth_client`.

    Because the transport is constructed by
    :class:`mcpxy_proxy.proxy.manager.UpstreamManager` from the plain
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
        self.token_transform = self._coerce_token_transform(settings.get("token_transform"))
        self._oauth_manager = settings.get("_oauth_manager")
        self._config_store = settings.get("_config_store")
        self._auth_strategy = None  # lazily bound in start()
        self._client: httpx.AsyncClient | None = None
        self._last_error: str | None = None

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

        from mcpxy_proxy.config import HttpAuthConfig

        return TypeAdapter(HttpAuthConfig).validate_python(raw)

    @staticmethod
    def _coerce_token_transform(raw: Any) -> TokenTransformConfig | None:
        if raw is None:
            return None
        if isinstance(raw, TokenTransformConfig):
            return raw
        return TokenTransformConfig.model_validate(raw)

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
            from mcpxy_proxy.auth.oauth import OAuthHttpxAuth

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
    # Token transformation
    # ------------------------------------------------------------------

    def _resolve_transform_headers(
        self, context: "RequestContext | None"
    ) -> dict[str, str] | None:
        """Compute per-request header overrides based on the token transform policy.

        Returns ``None`` when no transformation is needed (strategy is
        ``static`` or unconfigured), an empty dict on deny, or a dict
        of headers to merge on success.
        """
        tt = self.token_transform
        if tt is None or tt.strategy == "static":
            return None

        if tt.strategy == "passthrough":
            if context and context.incoming_bearer:
                return {"Authorization": f"Bearer {context.incoming_bearer}"}
            return None

        if tt.strategy == "header_inject":
            if context and context.email:
                return {tt.inject_header: context.email}
            return None

        if tt.strategy == "map":
            if not context or not context.user_id or not self._config_store:
                if tt.fallback_on_missing_map == "static":
                    return None
                return {}  # empty → deny (caller checks)
            mapping = self._config_store.get_token_mapping(
                upstream=self.name, user_id=context.user_id,
            )
            if mapping is not None:
                return {"Authorization": f"Bearer {mapping.upstream_token}"}
            if tt.fallback_on_missing_map == "static":
                return None
            return {}  # deny

        return None

    # ------------------------------------------------------------------
    # Request path
    # ------------------------------------------------------------------

    async def request(
        self,
        message: dict[str, Any],
        context: "RequestContext | None" = None,
    ) -> dict[str, Any] | None:
        if not self._client:
            raise RuntimeError("http transport not started")
        extra_headers = self._resolve_transform_headers(context)
        if extra_headers is not None and len(extra_headers) == 0:
            # Deny: strategy is "map" and no mapping found
            from mcpxy_proxy.jsonrpc import JsonRpcError

            raise JsonRpcError(
                -32003,
                "token_mapping_not_found",
                request_id=message.get("id"),
            )
        try:
            if extra_headers:
                resp = await self._client.post(
                    self.url, json=message, headers=extra_headers,
                )
            else:
                resp = await self._client.post(self.url, json=message)
        except (httpx.HTTPError, httpx.StreamError) as exc:
            detail = _root_cause(exc)
            self._last_error = detail
            logger.warning(
                "request_failed upstream=%s url=%s error=%s",
                self.name, self.url, detail,
                extra={"upstream": self.name},
            )
            raise
        self._last_error = None
        if not resp.content:
            return None
        return resp.json()

    async def send_notification(
        self,
        message: dict[str, Any],
        context: "RequestContext | None" = None,
    ) -> None:
        if not self._client:
            raise RuntimeError("http transport not started")
        extra_headers = self._resolve_transform_headers(context)
        if extra_headers is not None and len(extra_headers) == 0:
            return  # silently drop notification for unmapped user
        try:
            if extra_headers:
                await self._client.post(self.url, json=message, headers=extra_headers)
            else:
                await self._client.post(self.url, json=message)
        except (httpx.HTTPError, httpx.StreamError) as exc:
            detail = _root_cause(exc)
            self._last_error = detail
            logger.warning(
                "notification_failed upstream=%s url=%s error=%s",
                self.name, self.url, detail,
                extra={"upstream": self.name},
            )
            raise

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
        token_transform_strategy = None
        if self.token_transform is not None:
            token_transform_strategy = self.token_transform.strategy
        return {
            "type": "http",
            "url": self.url,
            "started": self._client is not None,
            "auth": auth_type,
            "tls": tls_state,
            "token_transform": token_transform_strategy,
            "last_error": self._last_error,
        }
