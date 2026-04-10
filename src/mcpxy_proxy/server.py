"""FastAPI server implementation."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import signal
import time
from codecs import getincrementaldecoder
import asyncio
from collections import deque
from collections.abc import Iterable
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from mcpxy_proxy.auth.oauth import (
    OAuthError,
    OAuthManager,
    OAuthNotAuthorizedError,
)
from mcpxy_proxy.authn import (
    AuthnManager,
    Principal,
    accept_invite,
    create_bootstrap_admin,
    ensure_federated_user_on_callback,
    extract_principal,
    invite_user,
    mint_pat,
    verify_pat,
)
from mcpxy_proxy.config import (
    AppConfig,
    AuthyConfig,
    HttpUpstreamConfig,
    OAuth2AuthConfig,
    find_secret_references,
    redact_secrets,
    resolve_admin_token,
    resolve_effective_auth_mode,
)
from mcpxy_proxy.discovery.catalog import Catalog, load_catalog
from mcpxy_proxy.discovery.importers import IMPORTERS, discover_all, get_importer
from mcpxy_proxy.discovery.registration import (
    DEFAULT_DROP_DIR,
    FileDropWatcher,
    RegistrationError,
    RegistrationService,
)
from mcpxy_proxy.install.clients import InstallOptions, get_adapter, list_clients
from mcpxy_proxy.jsonrpc import JsonRpcError, is_notification
from mcpxy_proxy.observability.discovery import RouteDiscoverer
from mcpxy_proxy.observability.traffic import TrafficRecorder
from mcpxy_proxy.policy.engine import PolicyEngine
from mcpxy_proxy.proxy.admin import AdminService
from mcpxy_proxy.proxy.bridge import ProxyBridge, RequestContext
from mcpxy_proxy.plugins.registry import PluginRegistry
from mcpxy_proxy.proxy.manager import UpstreamManager
from mcpxy_proxy.routing import resolve_upstream
from mcpxy_proxy.runtime import RuntimeConfigManager
from mcpxy_proxy.secrets import SecretsManager, SecretStoreError
from mcpxy_proxy.storage.bootstrap import (
    BootstrapConfig,
    write_bootstrap,
)
from mcpxy_proxy.storage.config_store import ConfigStore, OnboardingState, open_store
from mcpxy_proxy.storage.db import (
    DatabaseError,
    _assemble_url_from_parts,
    available_dialects,
    dialect_of,
    probe_connection,
    sanitize_url,
)
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline


# Onboarding bypass TTL: if the wizard isn't completed within this window
# after the first-run row is created, the onboarding endpoints stop
# accepting writes and return 410. Operators who need longer can set
# MCPXY_ONBOARDING_TTL_S (seconds) before starting the proxy.
DEFAULT_ONBOARDING_TTL_S = 30 * 60

# Client IPs that are allowed to hit the onboarding endpoints. Limited
# to loopback by default because the endpoints are unauthenticated;
# operators running the proxy behind an ingress that rewrites the
# client IP can override via MCPXY_ONBOARDING_ALLOWED_CLIENTS
# (comma-separated list, e.g. ``127.0.0.1,10.0.0.5``).
_DEFAULT_ONBOARDING_ALLOWED_CLIENTS: tuple[str, ...] = (
    "127.0.0.1",
    "::1",
    "localhost",
    "testclient",  # FastAPI TestClient
)


def _onboarding_ttl() -> float:
    raw_env = os.getenv("MCPXY_ONBOARDING_TTL_S")
    if raw_env is None:
        return float(DEFAULT_ONBOARDING_TTL_S)
    try:
        return max(60.0, float(raw_env))
    except ValueError:
        return float(DEFAULT_ONBOARDING_TTL_S)


def _parse_allowed_clients(
    entries: Iterable[str],
) -> tuple[set[str], list[ipaddress._BaseNetwork]]:
    """Split a raw allowlist into literal strings and CIDR networks.

    Entries that parse as an IP or CIDR (``ipaddress.ip_network`` with
    ``strict=False``, so bare literals like ``127.0.0.1`` become /32
    networks) are collected as networks. Everything else — including
    the historical ``"localhost"`` and ``"testclient"`` sentinels —
    stays as a literal string for exact-match fallback. This preserves
    backwards compatibility with existing configs while adding CIDR
    support for Docker NAT ranges, reverse-proxy subnets, and similar
    operator needs.
    """
    literals: set[str] = set()
    networks: list[ipaddress._BaseNetwork] = []
    for entry in entries:
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            literals.add(entry)
    return literals, networks


def _client_ip_allowed(
    client_ip: str,
    literals: set[str],
    networks: list[ipaddress._BaseNetwork],
) -> bool:
    """Check whether ``client_ip`` matches the parsed allowlist.

    Literal entries match exactly (so ``"testclient"`` and ``"localhost"``
    still work). Network entries match by membership — a ``172.16.0.0/12``
    entry admits any IP in that range. IPs that fail to parse (the
    ``"testclient"`` / ``"unknown"`` sentinels) skip the network check
    and fall through to literal comparison only.
    """
    if client_ip in literals:
        return True
    if not networks:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def _onboarding_allowed_clients() -> tuple[set[str], list[ipaddress._BaseNetwork]]:
    raw = os.getenv("MCPXY_ONBOARDING_ALLOWED_CLIENTS")
    if not raw:
        entries: list[str] = list(_DEFAULT_ONBOARDING_ALLOWED_CLIENTS)
    else:
        entries = [item.strip() for item in raw.split(",") if item.strip()]
    return _parse_allowed_clients(entries)


class InMemoryLogHandler(logging.Handler):
    def __init__(self, store: deque[dict[str, Any]]) -> None:
        super().__init__()
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        self.store.append(
            {
                "timestamp": time.time(),
                "logger": record.name,
                "level": record.levelname,
                "message": record.getMessage(),
                "upstream": getattr(record, "upstream", None),
            }
        )


class AppState:
    """Runtime state container."""

    def __init__(
        self,
        config: AppConfig,
        raw_config: dict[str, Any],
        manager: UpstreamManager,
        bridge: ProxyBridge,
        telemetry: TelemetryPipeline,
        registry: PluginRegistry,
        config_path: str | None = None,
        secrets_manager: SecretsManager | None = None,
        oauth_manager: OAuthManager | None = None,
        config_store: ConfigStore | None = None,
    ) -> None:
        self.config = config
        self.raw_config = raw_config
        self.manager = manager
        self.bridge = bridge
        self.telemetry = telemetry
        self.registry = registry
        self.config_path = config_path
        # SecretsManager is created by the CLI's build_state so that config
        # loading can run through ${secret:NAME} expansion before we even
        # reach this class. Tests that construct AppState directly can
        # leave it None and the runtime config path will skip secret
        # expansion (secrets_manager.get is simply not installed).
        self.secrets_manager = secrets_manager or SecretsManager(autoload=False)
        # ConfigStore: prefer the one supplied by the caller (CLI bootstrap
        # path); fall back to the one already wrapped inside the secrets
        # manager so the CLI's "single store, single fernet, single source
        # of truth" guarantee holds even when AppState is constructed in a
        # legacy two-arg shape from tests.
        self.config_store: ConfigStore = config_store or self.secrets_manager.store
        # OAuth manager is a process-wide coordinator for every HTTP
        # upstream that uses oauth2. It persists tokens and dynamic
        # client credentials via the same SecretsManager, so rotation of
        # MCPXY_SECRETS_KEY is the single point of control for all
        # upstream auth state.
        self.oauth_manager = oauth_manager or OAuthManager(secrets=self.secrets_manager)
        self.authn = AuthnManager(config.auth.authy, store=self.config_store)
        # If the caller didn't wire the OAuth manager into UpstreamManager
        # (most tests construct UpstreamManager without one), fill it in
        # now so HTTP transports with oauth2 auth can find the shared
        # manager via the settings side-channel.
        if getattr(manager, "_oauth_manager", None) is None:
            manager._oauth_manager = self.oauth_manager  # type: ignore[attr-defined]
        self.started_at = time.time()
        self.log_buffer: deque[dict[str, Any]] = deque(maxlen=400)
        self.traffic = TrafficRecorder()
        self.route_discovery = RouteDiscoverer(manager)
        self.policy_engine = PolicyEngine(self.config)
        self.runtime_config = RuntimeConfigManager(
            raw_config=self.raw_config,
            config=self.config,
            manager=self.manager,
            telemetry=self.telemetry,
            registry=self.registry,
            config_path=self.config_path,
            policy_engine=self.policy_engine,
            secrets_resolver=self.secrets_manager.get,
            on_config_applied=self._on_config_applied,
            store=self.config_store,
        )
        # Seed the OAuth manager with any upstreams that already have
        # oauth2 configured, so persisted tokens from a previous run are
        # warmed into the in-memory cache before the transports start.
        self._register_oauth_configs(config)
        self.registration = RegistrationService(self.runtime_config)
        try:
            self.catalog: Catalog | None = load_catalog()
        except Exception as exc:  # pragma: no cover - catalog is bundled
            logging.getLogger(__name__).error("Failed to load MCP catalog: %s", exc)
            self.catalog = None
        self.file_drop: FileDropWatcher | None = None
        drop_dir = raw_config.get("registration", {}).get("drop_dir") if isinstance(raw_config.get("registration"), dict) else None
        drop_enabled = True
        if isinstance(raw_config.get("registration"), dict):
            drop_enabled = bool(raw_config["registration"].get("file_drop_enabled", True))
        if drop_enabled:
            self.file_drop = FileDropWatcher(
                self.registration,
                directory=Path(drop_dir) if drop_dir else DEFAULT_DROP_DIR,
            )

    # ------------------------------------------------------------------
    # OAuth config bookkeeping
    # ------------------------------------------------------------------

    def _on_config_applied(self, cfg: AppConfig) -> None:
        """Called after every successful config apply (hot-reload)."""
        self._register_oauth_configs(cfg)
        self.authn.rebuild(cfg.auth.authy)

    def _register_oauth_configs(self, cfg: AppConfig) -> None:
        """Push every HTTP upstream's oauth2 config into the OAuthManager.

        Upstreams whose ``auth`` is not ``oauth2`` are unregistered from
        the manager so stale token caches don't linger after a config
        edit that switches them to bearer auth (or removes them
        entirely).
        """
        seen: set[str] = set()
        for name, upstream_cfg in cfg.upstreams.items():
            if not isinstance(upstream_cfg, HttpUpstreamConfig):
                continue
            auth = upstream_cfg.auth
            if not isinstance(auth, OAuth2AuthConfig):
                self.oauth_manager.unregister_upstream(name)
                continue
            self.oauth_manager.register_upstream(name, auth)
            seen.add(name)
        # Drop OAuth state for upstreams that disappeared entirely.
        for known in list(self.oauth_manager._configs):  # type: ignore[attr-defined]
            if known not in seen:
                self.oauth_manager.unregister_upstream(known)


def _decode_message(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("JSON-RPC payload must be an object")
    return raw


def _get_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return auth.removeprefix("Bearer ").strip()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def create_app(state: AppState, health_path: str = "/health", request_timeout_s: float = 30.0) -> FastAPI:
    """Create configured FastAPI app."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            signal.signal(signal.SIGINT, handle_shutdown_signal)
            signal.signal(signal.SIGTERM, handle_shutdown_signal)
        except ValueError:
            pass
        await state.manager.start()
        await state.runtime_config.telemetry.start()
        state.runtime_config.telemetry.emit_nowait({"event": "proxy_startup"})
        await state.runtime_config.start()
        await state.route_discovery.start()
        if state.file_drop is not None:
            await state.file_drop.start()
        try:
            yield
        finally:
            # Best-effort teardown: a failure in any single subsystem
            # shutdown must not prevent the others from running, and
            # must not propagate up through the lifespan (which would
            # cause uvicorn to exit with a non-zero status on SIGTERM).
            state.bridge.start_shutdown()
            state.runtime_config.telemetry.emit_nowait({"event": "proxy_shutdown_start"})
            shutdown_steps: list[tuple[str, Any]] = []
            if state.file_drop is not None:
                shutdown_steps.append(("file_drop", state.file_drop.stop()))
            shutdown_steps.append(("route_discovery", state.route_discovery.stop()))
            shutdown_steps.append(("runtime_config", state.runtime_config.stop()))
            shutdown_steps.append(("manager", state.manager.stop()))
            for name, coro in shutdown_steps:
                try:
                    await coro
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "shutdown step '%s' raised: %s", name, exc
                    )
            state.runtime_config.telemetry.emit_nowait({"event": "proxy_shutdown_complete"})
            with suppress(Exception):
                await state.runtime_config.telemetry.stop()

    app = FastAPI(title="MCPxy Proxy", lifespan=lifespan)
    admin_service = AdminService(state.manager, state.telemetry, state.raw_config, state.runtime_config, state.log_buffer)
    state.bridge.set_telemetry_emitter(state.runtime_config.telemetry.emit_nowait)
    state.bridge.set_traffic_recorder(state.traffic.record)
    state.bridge.set_policy_engine(state.policy_engine)

    root_logger = logging.getLogger()
    root_logger.addHandler(InMemoryLogHandler(state.log_buffer))

    def _resolve_admin_bearer() -> str | None:
        """Return the currently-configured admin bearer token.

        Tries the direct ``auth.token`` field first (populated by the
        onboarding wizard or by a ``${secret:NAME}`` reference) and
        falls back to the env var pointed at by ``auth.token_env`` for
        backwards compatibility with file-based deployments.
        """
        return resolve_admin_token(state.runtime_config.config.auth)

    # In-memory state-param store for federated OAuth login flows.
    _oauth_state_store: dict[str, tuple[str, float]] = {}

    async def _extract_request_principal(request: Request) -> Principal | None:
        """Resolve a Principal from the request using the Authy integration."""
        principal = await extract_principal(
            request,
            auth_config=state.runtime_config.config.auth,
            manager=state.authn,
            store=state.config_store,
        )
        if principal is not None:
            request.state.principal = principal
        return principal

    async def require_auth_if_needed(request: Request) -> Principal | None:
        mode = resolve_effective_auth_mode(state.runtime_config.config.auth)
        if mode == "none":
            return None
        principal = await _extract_request_principal(request)
        if principal is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        return principal

    async def require_admin_auth(request: Request) -> Principal:
        admin = state.runtime_config.config.admin
        if admin.allowed_clients:
            literals, networks = _parse_allowed_clients(admin.allowed_clients)
            if not _client_ip_allowed(_client_ip(request), literals, networks):
                raise HTTPException(status_code=403, detail="forbidden")
        principal = await _extract_request_principal(request)
        mode = resolve_effective_auth_mode(state.runtime_config.config.auth)
        if mode == "authy":
            if principal is None or principal.role != "admin":
                raise HTTPException(status_code=401, detail="unauthorized")
            return principal
        # Legacy path
        if admin.require_token:
            expected = _resolve_admin_bearer()
            if not expected:
                raise HTTPException(status_code=500, detail="admin_token_not_configured")
            if _get_bearer(request) != expected:
                raise HTTPException(status_code=401, detail="unauthorized")
        return principal or Principal(
            user_id=-1, email="legacy@local", role="admin",
            provider="legacy", auth_mode="legacy",
        )

    async def parse_messages(request: Request) -> AsyncIterator[dict[str, Any]]:
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
        if ctype == "application/x-ndjson":
            decoder = getincrementaldecoder("utf-8")()
            pending = ""
            async for chunk in request.stream():
                pending += decoder.decode(chunk)
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    if line.strip():
                        yield _decode_message(json.loads(line))
            pending += decoder.decode(b"", final=True)
            if pending.strip():
                yield _decode_message(json.loads(pending))
            return

        decoder = getincrementaldecoder("utf-8")()
        parser = json.JSONDecoder()
        mode = "unknown"
        cursor = 0
        buffer = ""

        async for chunk in request.stream():
            buffer += decoder.decode(chunk)
            while True:
                while cursor < len(buffer) and buffer[cursor].isspace():
                    cursor += 1

                if mode == "unknown":
                    if cursor >= len(buffer):
                        break
                    if buffer[cursor] == "[":
                        mode = "array"
                        cursor += 1
                        continue
                    mode = "single"

                if mode == "array":
                    if cursor >= len(buffer):
                        break
                    if buffer[cursor] == "]":
                        mode = "done"
                        cursor += 1
                        continue
                    if buffer[cursor] == ",":
                        cursor += 1
                        continue

                if mode == "done":
                    while cursor < len(buffer) and buffer[cursor].isspace():
                        cursor += 1
                    if cursor < len(buffer):
                        raise ValueError("Trailing data after JSON payload")
                    break

                try:
                    parsed, end = parser.raw_decode(buffer, cursor)
                except json.JSONDecodeError:
                    break

                if mode == "single":
                    if isinstance(parsed, list):
                        for item in parsed:
                            yield _decode_message(item)
                    else:
                        yield _decode_message(parsed)
                    cursor = end
                    mode = "done"
                    continue

                yield _decode_message(parsed)
                cursor = end

            if cursor > 0 and cursor >= len(buffer):
                buffer = ""
                cursor = 0

        buffer += decoder.decode(b"", final=True)
        while cursor < len(buffer) and buffer[cursor].isspace():
            cursor += 1
        if mode in {"unknown", "single", "array"} and cursor < len(buffer):
            raise ValueError("Incomplete JSON payload")

    async def call_admin_method(method: str, params: dict[str, Any]) -> Any:
        response = await admin_service.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, lambda: build_health())
        if "error" in response:
            raise HTTPException(status_code=400, detail=response["error"]["message"])
        return response["result"]

    async def handle_proxy(request: Request, path_name: str | None, x_mcp_upstream: str | None) -> Response:
        await require_auth_if_needed(request)
        client_ip = _client_ip(request)
        # Build a RequestContext for token transformation
        principal: Principal | None = getattr(request.state, "principal", None)
        req_context = RequestContext(
            user_id=principal.user_id if principal else None,
            email=principal.email if principal else None,
            role=principal.role if principal else None,
            incoming_bearer=_get_bearer(request),
        )
        async def iter_response_lines() -> AsyncIterator[bytes]:
            async for msg in parse_messages(request):
                admin = state.runtime_config.config.admin
                params = msg.get("params")
                in_band_upstream = params.get("mcp_upstream") if isinstance(params, dict) else None
                is_admin_target = admin.enabled and (
                    path_name == admin.mount_name
                    or x_mcp_upstream == admin.mount_name
                    or in_band_upstream == admin.mount_name
                )

                upstream, cleaned = resolve_upstream(msg, state.runtime_config.config, path_name, x_mcp_upstream)
                if is_admin_target:
                    await require_admin_auth(request)
                    resp = await admin_service.handle(cleaned, lambda: build_health())
                    if not is_notification(msg):
                        yield (json.dumps(resp) + "\n").encode("utf-8")
                    continue
                if upstream is None:
                    if not is_notification(msg):
                        err = JsonRpcError(-32602, "upstream_not_resolved", request_id=msg.get("id")).to_response()
                        yield (json.dumps(err) + "\n").encode("utf-8")
                    continue
                try:
                    try:
                        request_bytes = len(json.dumps(cleaned).encode("utf-8"))
                    except (TypeError, ValueError):
                        request_bytes = 0
                    out = await state.bridge.forward(
                        upstream,
                        cleaned,
                        request_bytes=request_bytes,
                        client_ip=client_ip,
                        context=req_context,
                    )
                    if out is not None:
                        yield (json.dumps(out) + "\n").encode("utf-8")
                except JsonRpcError as exc:
                    if not is_notification(msg):
                        yield (json.dumps(exc.to_response()) + "\n").encode("utf-8")

        iterator = iter_response_lines()
        try:
            first = await anext(iterator)
        except StopAsyncIteration:
            return Response(status_code=202)

        async def with_first() -> AsyncIterator[bytes]:
            yield first
            async for chunk in iterator:
                yield chunk

        return StreamingResponse(with_first(), media_type="application/x-ndjson")

    def build_health() -> dict[str, Any]:
        return {
            "status": "ok",
            "upstreams": state.manager.health(),
            "telemetry": state.runtime_config.telemetry.health(),
            "uptime_s": round(time.time() - state.started_at, 3),
            "version": "0.1.0",
        }

    def handle_shutdown_signal(signum: int, _frame: Any | None = None) -> None:
        state.bridge.start_shutdown()
        state.runtime_config.telemetry.emit_nowait(
            {
                "event": "proxy_shutdown_signal",
                "signal": signal.Signals(signum).name,
            }
        )

    app.state.handle_shutdown_signal = handle_shutdown_signal

    web_root = Path(__file__).parent / "web"
    dist_root = web_root / "dist"
    dist_assets = dist_root / "assets"
    if dist_assets.is_dir():
        app.mount(
            "/admin/static/dist/assets",
            StaticFiles(directory=str(dist_assets)),
            name="admin-dist-assets",
        )
    if (web_root / "static").is_dir():
        app.mount("/admin/static", StaticFiles(directory=str(web_root / "static")), name="admin-static")

    async def handle_proxy_with_timeout(request: Request, path_name: str | None, x_mcp_upstream: str | None) -> Response:
        try:
            return await asyncio.wait_for(handle_proxy(request, path_name, x_mcp_upstream), timeout=request_timeout_s)
        except TimeoutError:
            return JSONResponse({"error": "request_timeout"}, status_code=504)

    @app.post("/mcp")
    async def post_mcp(request: Request, x_mcp_upstream: str | None = Header(default=None)) -> Response:
        return await handle_proxy_with_timeout(request, None, x_mcp_upstream)

    @app.post("/mcp/{name}")
    async def post_mcp_named(name: str, request: Request, x_mcp_upstream: str | None = Header(default=None)) -> Response:
        return await handle_proxy_with_timeout(request, name, x_mcp_upstream)

    @app.get(health_path)
    async def health() -> JSONResponse:
        return JSONResponse(build_health())

    @app.get("/status")
    async def status() -> JSONResponse:
        data = build_health()
        return JSONResponse({"upstreams": data["upstreams"], "uptime_s": data["uptime_s"], "version": data["version"]})

    def _dashboard_html() -> str:
        dist_index = dist_root / "index.html"
        if dist_index.is_file():
            return dist_index.read_text(encoding="utf-8")
        legacy = web_root / "templates" / "admin.html"
        if legacy.is_file():
            return legacy.read_text(encoding="utf-8")
        return (
            "<!doctype html><meta charset='utf-8'><title>MCPxy Admin</title>"
            "<h1>MCPxy Admin</h1><p>Dashboard assets are not built. "
            "Run <code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>.</p>"
        )

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_index(_request: Request) -> HTMLResponse:
        # The SPA HTML is public; the in-page LoginGate collects the admin
        # token and all /admin/api/* endpoints remain auth-gated.
        return HTMLResponse(_dashboard_html())

    # ------------------------------------------------------------------
    # First-run onboarding
    # ------------------------------------------------------------------

    # Serialises the hot-swap path in ``/admin/api/onboarding/set_database``
    # so concurrent wizard clicks can't half-swap the store reference.
    _database_swap_lock = asyncio.Lock()

    def _current_onboarding() -> OnboardingState | None:
        return state.config_store.get_onboarding_state()

    def _state_dir() -> Path:
        """Return the directory the proxy uses for runtime state.

        We read it off the SecretsManager rather than recomputing the
        default candidates so a test that pins ``state_dir`` explicitly
        is respected by the onboarding database endpoints too.
        """
        return Path(state.secrets_manager.state_dir)

    def _onboarding_database_block() -> dict[str, Any]:
        """Return the structured ``database`` block exposed by status.

        Shape:
          {
            "current_url_masked": "sqlite:////var/lib/mcpxy/mcpxy.db",
            "current_dialect": "sqlite",
            "is_default": true,
            "bootstrap_file_present": false,
            "available_dialects": ["sqlite", "postgresql"],
          }
        """
        raw_url = str(state.config_store.engine.url)
        masked = sanitize_url(raw_url)
        dialect = dialect_of(raw_url)
        sd = _state_dir()
        default_sqlite = f"sqlite:///{sd / 'mcpxy.db'}"
        is_default = raw_url == default_sqlite
        bootstrap_present = (sd / "bootstrap.json").exists()
        return {
            "current_url_masked": masked,
            "current_dialect": dialect,
            "is_default": is_default,
            "bootstrap_file_present": bootstrap_present,
            "available_dialects": available_dialects(),
        }

    def _onboarding_public(obstate: OnboardingState | None) -> dict[str, Any]:
        if obstate is None:
            return {
                "active": False,
                "completed": False,
                "expired": False,
                "required": False,
                "database": _onboarding_database_block(),
            }
        base = obstate.to_public_dict(ttl_s=_onboarding_ttl())
        # ``required`` tells the frontend whether to route straight to
        # the wizard instead of the normal LoginGate. It's active-and-
        # not-expired AND the admin token isn't set yet (the wizard
        # can finish before touching upstreams, so expired state
        # still counts as "required" unless finished).
        base["required"] = bool(
            base["active"]
            and not base["completed"]
            and obstate.admin_token_set_at is None
        )
        base["database"] = _onboarding_database_block()
        return base

    def _require_onboarding_access(request: Request) -> OnboardingState:
        """Shared gating for all mutating onboarding endpoints.

        Refuses when the row is missing, the flow was already
        completed (410 Gone), the TTL elapsed (410 Gone), or the
        request came from an IP that isn't on the allowed list.
        The allowed list defaults to loopback + FastAPI's
        ``testclient`` so local dev + CI both work without ceremony.
        """
        obstate = _current_onboarding()
        if obstate is None:
            raise HTTPException(
                status_code=404,
                detail="onboarding not initialised",
            )
        if obstate.completed_at is not None:
            raise HTTPException(
                status_code=410,
                detail="onboarding already completed",
            )
        ttl = _onboarding_ttl()
        if time.time() - obstate.created_at > ttl:
            raise HTTPException(
                status_code=410,
                detail=(
                    f"onboarding window expired ({int(ttl)}s TTL); "
                    "restart the proxy to reopen it"
                ),
            )
        literals, networks = _onboarding_allowed_clients()
        client_ip = _client_ip(request)
        if (literals or networks) and not _client_ip_allowed(client_ip, literals, networks):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"onboarding endpoints are loopback-only by default; "
                    f"client {client_ip!r} is not in the allowed list"
                ),
            )
        return obstate

    @app.middleware("http")
    async def _onboarding_gate(request: Request, call_next: Any) -> Any:
        """Two-level fail-closed gate on the admin API surface.

        While onboarding is still pending we intentionally run with
        ``auth.require_token=False`` so the wizard is reachable. The
        side effect is that every other admin endpoint would be
        unauthenticated too — which is exactly the footgun this
        middleware closes. The gate has two layers:

        1. **Onboarding required** — if the ``onboarding`` row exists
           and ``required=true`` (wizard not yet completed, not yet
           expired, admin token not yet set), return 503
           ``onboarding_required`` so the dashboard client redirects
           straight to the wizard.
        2. **No bearer configured** — if no ``auth.token`` or
           ``auth.token_env`` resolves to a real value, return 503
           ``admin_token_not_configured`` regardless of onboarding
           state. This is the fail-closed check that plugs the
           TTL-expiry hole: once the onboarding window lapses,
           ``required`` flips to false, but that must NOT fall
           through to an unauthenticated admin API. It also guards
           against any future footgun (operator manually unset
           ``auth.token``, config rollback to an older version with
           no token, etc.) leaving the proxy open.

        Routes that are deliberately left open:
          - ``/admin/api/onboarding/*``   (the wizard itself)
          - ``/admin/api/oauth/callback`` (browser redirect target)
          - anything outside ``/admin/api/*`` (SPA, health, /mcp, etc.)
        """
        path = request.url.path
        if not path.startswith("/admin/api/"):
            return await call_next(request)
        if path.startswith("/admin/api/onboarding/"):
            return await call_next(request)
        if path == "/admin/api/oauth/callback":
            return await call_next(request)
        # Authy login endpoints are always reachable (they handle their
        # own auth internally).
        if path.startswith("/admin/api/authy/"):
            return await call_next(request)
        if path == "/admin/api/users/accept_invite":
            return await call_next(request)
        obstate = _current_onboarding()
        public = _onboarding_public(obstate)
        if obstate is not None and public["required"]:
            return JSONResponse(
                {
                    "detail": "onboarding_required",
                    "onboarding": public,
                },
                status_code=503,
            )
        # Fail closed: refuse ALL non-onboarding admin API calls when
        # no identity is resolvable from the live config, whether via
        # authy (admin user exists) or legacy bearer.
        auth_cfg = state.runtime_config.config.auth
        if auth_cfg.authy.enabled:
            if state.config_store.count_admins() == 0:
                return JSONResponse(
                    {
                        "detail": "authy_not_configured",
                        "onboarding": public,
                    },
                    status_code=503,
                )
        elif resolve_admin_token(auth_cfg) is None:
            return JSONResponse(
                {
                    "detail": "admin_token_not_configured",
                    "onboarding": public,
                },
                status_code=503,
            )
        return await call_next(request)

    @app.get("/admin/api/onboarding/status")
    async def admin_api_onboarding_status(_request: Request) -> JSONResponse:
        # Intentionally unauthenticated: the whole point is that the
        # frontend can probe this before it has any credentials.
        return JSONResponse(_onboarding_public(_current_onboarding()))

    @app.post("/admin/api/onboarding/set_admin_token")
    async def admin_api_onboarding_set_admin_token(request: Request) -> JSONResponse:
        _require_onboarding_access(request)
        body = await request.json()
        token = body.get("token")
        if not isinstance(token, str) or len(token) < 16:
            raise HTTPException(
                status_code=400,
                detail=(
                    "body requires a 'token' string of at least 16 chars "
                    "(use a Fernet-style key or any high-entropy secret)"
                ),
            )

        # Patch the live config with the new admin token + require_token=True
        # and run it through the normal apply pipeline so everything
        # downstream (OAuth manager, file-drop watcher, etc.) sees the
        # new state in one atomic swap.
        merged = deepcopy(state.raw_config)
        merged.setdefault("auth", {})
        merged["auth"]["token"] = token
        merged.setdefault("admin", {})
        merged["admin"]["require_token"] = True

        result = await state.runtime_config.apply(
            merged, source="onboarding.set_admin_token"
        )
        if not result.get("applied"):
            raise HTTPException(
                status_code=400,
                detail=result.get("error", "failed to apply token"),
            )
        updated = state.config_store.stamp_admin_token_set()
        state.runtime_config.telemetry.emit_nowait(
            {"event": "onboarding_set_admin_token"}
        )
        return JSONResponse(
            {
                "applied": True,
                "onboarding": _onboarding_public(updated),
            }
        )

    @app.post("/admin/api/onboarding/add_upstream")
    async def admin_api_onboarding_add_upstream(request: Request) -> JSONResponse:
        _require_onboarding_access(request)
        body = await request.json()
        name = body.get("name")
        definition = body.get("config") or body.get("definition")
        if not name or not isinstance(definition, dict):
            raise HTTPException(
                status_code=400, detail="body requires 'name' and 'config' object"
            )
        try:
            result = await state.registration.add(
                name=str(name),
                definition=definition,
                replace=bool(body.get("replace", False)),
                source="onboarding.add_upstream",
            )
        except RegistrationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not result.get("applied"):
            raise HTTPException(
                status_code=400, detail=result.get("error", "registration failed")
            )
        state.config_store.stamp_first_upstream()
        return JSONResponse(
            {
                **result,
                "onboarding": _onboarding_public(_current_onboarding()),
            }
        )

    @app.post("/admin/api/onboarding/finish")
    async def admin_api_onboarding_finish(request: Request) -> JSONResponse:
        obstate = _require_onboarding_access(request)
        if obstate.admin_token_set_at is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "cannot finish onboarding before an admin token is set; "
                    "POST /admin/api/onboarding/set_admin_token first"
                ),
            )
        finished = state.config_store.finish_onboarding(
            completed_by=_client_ip(request)
        )
        state.runtime_config.telemetry.emit_nowait(
            {"event": "onboarding_finished"}
        )
        return JSONResponse(_onboarding_public(finished))

    # ------------------------------------------------------------------
    # Authy auth endpoints
    # ------------------------------------------------------------------

    @app.get("/admin/api/authy/providers")
    async def admin_api_authy_providers(_request: Request) -> JSONResponse:
        providers = state.authn.list_enabled_providers()
        return JSONResponse({
            "providers": providers,
            "authy_enabled": state.runtime_config.config.auth.authy.enabled,
        })

    @app.post("/admin/api/authy/login")
    async def admin_api_authy_login(request: Request) -> JSONResponse:
        body = await request.json()
        email = body.get("email") or body.get("username")
        password = body.get("password")
        if not email or not password:
            raise HTTPException(status_code=400, detail="email and password required")
        result = await state.authn.authenticate_local(email, password)
        if not result.success or not result.token:
            raise HTTPException(status_code=401, detail=result.error or "authentication failed")
        response = JSONResponse({
            "token": result.token,
            "user": result.user.__dict__ if result.user else None,
        })
        authy_cfg = state.runtime_config.config.auth.authy
        response.set_cookie(
            key=authy_cfg.cookie_name,
            value=result.token,
            httponly=True,
            secure=authy_cfg.cookie_secure,
            samesite=authy_cfg.cookie_same_site,
            max_age=authy_cfg.token_ttl_s,
            path="/",
        )
        return response

    @app.post("/admin/api/authy/login/start")
    async def admin_api_authy_login_start(request: Request) -> JSONResponse:
        import secrets as _secrets
        body = await request.json()
        provider = body.get("provider")
        if not provider:
            raise HTTPException(status_code=400, detail="provider required")
        oauth_state = _secrets.token_urlsafe(32)
        _oauth_state_store[oauth_state] = (provider, time.time())
        try:
            auth_url = await state.authn.start_federated(provider, oauth_state)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return JSONResponse({"authorization_url": auth_url})

    @app.get("/admin/api/authy/callback")
    async def admin_api_authy_callback(request: Request) -> Response:
        from starlette.responses import RedirectResponse
        code = request.query_params.get("code")
        oauth_state = request.query_params.get("state")
        if not code or not oauth_state:
            raise HTTPException(status_code=400, detail="code and state required")
        entry = _oauth_state_store.pop(oauth_state, None)
        if entry is None:
            raise HTTPException(status_code=400, detail="invalid or expired state")
        provider, created = entry
        if time.time() - created > 600:
            raise HTTPException(status_code=400, detail="state expired")
        result = await state.authn.complete_federated(provider, code, oauth_state)
        if not result.success or not result.user:
            raise HTTPException(status_code=401, detail=result.error or "authentication failed")
        user, _created = ensure_federated_user_on_callback(
            state.config_store,
            provider=provider,
            subject=result.user.id,
            email=result.user.email,
            name=result.user.name,
        )
        authy_cfg = state.runtime_config.config.auth.authy
        token = result.token or ""
        resp = RedirectResponse(url="/admin", status_code=302)
        resp.set_cookie(
            key=authy_cfg.cookie_name,
            value=token,
            httponly=True,
            secure=authy_cfg.cookie_secure,
            samesite=authy_cfg.cookie_same_site,
            max_age=authy_cfg.token_ttl_s,
            path="/",
        )
        return resp

    @app.post("/admin/api/authy/logout")
    async def admin_api_authy_logout(request: Request) -> JSONResponse:
        principal = await _extract_request_principal(request)
        if principal and principal.token_jti:
            from datetime import datetime, timedelta, timezone
            exp = datetime.now(timezone.utc) + timedelta(
                seconds=state.runtime_config.config.auth.authy.token_ttl_s
            )
            state.config_store.revoke_jwt(principal.token_jti, exp)
        authy_cfg = state.runtime_config.config.auth.authy
        response = JSONResponse({"ok": True})
        response.delete_cookie(key=authy_cfg.cookie_name, path="/")
        return response

    @app.get("/admin/api/authy/me")
    async def admin_api_authy_me(request: Request) -> JSONResponse:
        principal = await _extract_request_principal(request)
        if principal is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        return JSONResponse({
            "user_id": principal.user_id,
            "email": principal.email,
            "role": principal.role,
            "provider": principal.provider,
            "auth_mode": principal.auth_mode,
        })

    # ------------------------------------------------------------------
    # User management endpoints (admin only)
    # ------------------------------------------------------------------

    @app.get("/admin/api/users")
    async def admin_api_users(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        users = state.config_store.list_users()
        return JSONResponse([u.to_public_dict() for u in users])

    @app.post("/admin/api/users/invite")
    async def admin_api_users_invite(request: Request) -> JSONResponse:
        principal = await require_admin_auth(request)
        body = await request.json()
        email = body.get("email")
        role = body.get("role", "member")
        if not email:
            raise HTTPException(status_code=400, detail="email required")
        if role not in ("admin", "member"):
            raise HTTPException(status_code=400, detail="role must be 'admin' or 'member'")
        record, plaintext = invite_user(
            state.config_store,
            email=email,
            role=role,
            invited_by_id=principal.user_id if principal.user_id > 0 else None,
        )
        result = record.to_public_dict()
        result["plaintext_token"] = plaintext
        return JSONResponse(result)

    @app.post("/admin/api/users/accept_invite")
    async def admin_api_users_accept_invite(request: Request) -> JSONResponse:
        body = await request.json()
        token = body.get("token")
        password = body.get("password")
        name = body.get("name")
        if not token or not password:
            raise HTTPException(status_code=400, detail="token and password required")
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="password must be at least 8 characters")
        user = accept_invite(
            state.config_store,
            token_plaintext=token,
            password=password,
            name=name,
            manager=state.authn,
        )
        if user is None:
            raise HTTPException(status_code=400, detail="invalid or expired invite")
        return JSONResponse(user.to_public_dict(), status_code=201)

    @app.delete("/admin/api/users/{user_id}")
    async def admin_api_users_delete(user_id: int, request: Request) -> JSONResponse:
        await require_admin_auth(request)
        target = state.config_store.get_user(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")
        if target.role == "admin" and state.config_store.count_admins() <= 1:
            raise HTTPException(status_code=400, detail="cannot delete last admin")
        state.config_store.revoke_all_pats_for_user(user_id)
        state.config_store.delete_user(user_id)
        return JSONResponse({"ok": True})

    @app.post("/admin/api/users/{user_id}/role")
    async def admin_api_users_role(user_id: int, request: Request) -> JSONResponse:
        await require_admin_auth(request)
        body = await request.json()
        role = body.get("role")
        if role not in ("admin", "member"):
            raise HTTPException(status_code=400, detail="role must be 'admin' or 'member'")
        target = state.config_store.get_user(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="user not found")
        if target.role == "admin" and role == "member" and state.config_store.count_admins() <= 1:
            raise HTTPException(status_code=400, detail="cannot demote last admin")
        state.config_store.update_user_role(user_id, role)
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------
    # Personal access token endpoints
    # ------------------------------------------------------------------

    @app.get("/admin/api/pats")
    async def admin_api_pats(request: Request) -> JSONResponse:
        principal = await require_auth_if_needed(request)
        if principal is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        pats = state.config_store.list_pats_for_user(principal.user_id)
        return JSONResponse([p.to_public_dict() for p in pats])

    @app.post("/admin/api/pats")
    async def admin_api_pats_create(request: Request) -> JSONResponse:
        principal = await require_auth_if_needed(request)
        if principal is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        body = await request.json()
        name = body.get("name", "Untitled")
        ttl_days = body.get("ttl_days")
        record, plaintext = mint_pat(
            state.config_store,
            user_id=principal.user_id,
            name=name,
            ttl_days=int(ttl_days) if ttl_days is not None else None,
        )
        result = record.to_public_dict()
        result["plaintext"] = plaintext
        return JSONResponse(result, status_code=201)

    @app.delete("/admin/api/pats/{pat_id}")
    async def admin_api_pats_delete(pat_id: int, request: Request) -> JSONResponse:
        principal = await require_auth_if_needed(request)
        if principal is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        ok = state.config_store.revoke_pat(pat_id, user_id=principal.user_id)
        if not ok and principal.role == "admin":
            ok = state.config_store.revoke_pat(pat_id)
        if not ok:
            raise HTTPException(status_code=404, detail="token not found or already revoked")
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------
    # Token mapping endpoints (admin only)
    # ------------------------------------------------------------------

    @app.get("/admin/api/token-mappings")
    async def admin_api_token_mappings(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        upstream = request.query_params.get("upstream")
        mappings = state.config_store.list_token_mappings(
            upstream=upstream or None,
        )
        return JSONResponse([m.to_public_dict() for m in mappings])

    @app.post("/admin/api/token-mappings")
    async def admin_api_token_mappings_create(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        body = await request.json()
        upstream = body.get("upstream")
        user_id = body.get("user_id")
        upstream_token = body.get("upstream_token")
        description = body.get("description", "")
        if not upstream or user_id is None or not upstream_token:
            raise HTTPException(
                status_code=400,
                detail="upstream, user_id, and upstream_token are required",
            )
        record = state.config_store.upsert_token_mapping(
            upstream=upstream,
            user_id=int(user_id),
            upstream_token=upstream_token,
            description=description,
        )
        return JSONResponse(record.to_public_dict(), status_code=201)

    @app.delete("/admin/api/token-mappings/{mapping_id}")
    async def admin_api_token_mappings_delete(mapping_id: int, request: Request) -> JSONResponse:
        await require_admin_auth(request)
        ok = state.config_store.delete_token_mapping(mapping_id)
        if not ok:
            raise HTTPException(status_code=404, detail="mapping not found")
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------
    # Onboarding: set_authy_config (replaces set_admin_token for new wizard)
    # ------------------------------------------------------------------

    @app.post("/admin/api/onboarding/set_authy_config")
    async def admin_api_onboarding_set_authy_config(request: Request) -> JSONResponse:
        _require_onboarding_access(request)
        body = await request.json()
        primary_provider = body.get("primary_provider")
        jwt_secret = body.get("jwt_secret")
        if not primary_provider or not jwt_secret:
            raise HTTPException(
                status_code=400,
                detail="primary_provider and jwt_secret are required",
            )
        # Persist jwt_secret and any client_secret into the secrets store.
        state.config_store.upsert_secret("AUTHY_JWT_SECRET", jwt_secret)
        for prov_key in ("google", "m365", "sso_oidc"):
            prov_block = body.get(prov_key)
            if isinstance(prov_block, dict) and prov_block.get("client_secret"):
                secret_name = f"AUTHY_{prov_key.upper()}_CLIENT_SECRET"
                state.config_store.upsert_secret(secret_name, prov_block["client_secret"])
                prov_block["client_secret"] = f"${{secret:{secret_name}}}"
        saml_block = body.get("sso_saml")
        if isinstance(saml_block, dict):
            if saml_block.get("idp_cert"):
                state.config_store.upsert_secret("AUTHY_SAML_IDP_CERT", saml_block["idp_cert"])
                saml_block["idp_cert"] = "${secret:AUTHY_SAML_IDP_CERT}"
            if saml_block.get("sp_private_key"):
                state.config_store.upsert_secret("AUTHY_SAML_SP_KEY", saml_block["sp_private_key"])
                saml_block["sp_private_key"] = "${secret:AUTHY_SAML_SP_KEY}"
        # Build the authy config block.
        authy_block: dict[str, Any] = {
            "enabled": True,
            "primary_provider": primary_provider,
            "jwt_secret": "${secret:AUTHY_JWT_SECRET}",
            "token_ttl_s": body.get("token_ttl_s", 86400),
        }
        for key in ("local", "google", "m365", "sso_oidc", "sso_saml"):
            val = body.get(key)
            if val is not None:
                authy_block[key] = val
        # Merge into the raw config and apply.
        merged = deepcopy(state.raw_config)
        auth_section = merged.setdefault("auth", {})
        auth_section["authy"] = authy_block
        auth_section["token"] = None
        # Turn off require_token since authy manages auth now.
        admin_section = merged.setdefault("admin", {})
        admin_section["require_token"] = False
        try:
            version = await state.runtime_config.apply(
                merged, source="onboarding.set_authy_config"
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        state.raw_config = merged
        # Create bootstrap admin user if local provider.
        bootstrap = body.get("bootstrap_admin")
        if bootstrap and primary_provider == "local":
            create_bootstrap_admin(
                state.config_store,
                email=bootstrap["email"],
                name=bootstrap.get("name", bootstrap["email"]),
                password=bootstrap["password"],
                manager=state.authn,
            )
        # For federated providers, record the bootstrap email.
        bootstrap_email = None
        if bootstrap and bootstrap.get("email"):
            bootstrap_email = bootstrap["email"]
        elif body.get("bootstrap_admin_email"):
            bootstrap_email = body["bootstrap_admin_email"]
        if bootstrap_email:
            state.config_store.stamp_bootstrap_admin_email(bootstrap_email)
        # Stamp the onboarding token-set flag.
        state.config_store.stamp_admin_token_set()
        return JSONResponse({"ok": True, "version": version})

    def _build_url_from_body(body: dict[str, Any]) -> str:
        """Build a database URL from an onboarding request body.

        The wizard can send either a raw ``url`` field (escape hatch
        for exotic URIs the form-based builder can't express) or a
        structured block with ``dialect``/``host``/``port``/``user``/
        ``password``/``database``/``sslmode``. Structured requests are
        run through SQLAlchemy's URL constructor so values are escaped
        correctly and typos fail fast with a 400.
        """
        raw_url = body.get("url")
        if isinstance(raw_url, str) and raw_url.strip():
            if any(ch in raw_url for ch in ("\r", "\n")):
                raise HTTPException(
                    status_code=400, detail="database URL may not contain newlines"
                )
            return raw_url.strip()
        dialect = body.get("dialect")
        if not isinstance(dialect, str) or not dialect:
            raise HTTPException(
                status_code=400,
                detail=(
                    "body requires either 'url' or 'dialect' + connection fields"
                ),
            )
        port_raw = body.get("port")
        port: int | None = None
        if port_raw is not None and port_raw != "":
            try:
                port = int(port_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400, detail=f"port must be an integer: {exc}"
                )
            if port < 1 or port > 65535:
                raise HTTPException(
                    status_code=400, detail="port must be between 1 and 65535"
                )
        query_args: dict[str, str] = {}
        sslmode = body.get("sslmode")
        if isinstance(sslmode, str) and sslmode:
            query_args["sslmode"] = sslmode
        try:
            return _assemble_url_from_parts(
                dialect=dialect,
                host=body.get("host") or None,
                port=port,
                database=body.get("database") or None,
                user=body.get("user") or body.get("username") or None,
                password=body.get("password") or None,
                query=query_args or None,
            )
        except DatabaseError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/admin/api/onboarding/test_database")
    async def admin_api_onboarding_test_database(request: Request) -> JSONResponse:
        """Probe a candidate database URL without touching live state.

        Opens a throwaway engine, runs ``SELECT 1``, and disposes. On
        success the UI enables the "Save and continue" button for this
        URL; on failure the inline error tells the operator exactly
        what went wrong (driver missing, DNS unreachable, auth).

        Never logs or returns the raw URL — only the masked form.
        """
        _require_onboarding_access(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        try:
            url = _build_url_from_body(body)
        except HTTPException:
            raise
        try:
            dialect = probe_connection(url)
        except DatabaseError as exc:
            # 200 with ok=false, not 500: this is expected user error
            # and the UI renders the message inline.
            return JSONResponse(
                {
                    "ok": False,
                    "error": str(exc),
                    "url_masked": sanitize_url(url),
                }
            )
        return JSONResponse(
            {
                "ok": True,
                "dialect": dialect,
                "url_masked": sanitize_url(url),
            }
        )

    def _hot_swap_store(new_store: ConfigStore) -> None:
        """Rebind every cached reference to the new ConfigStore.

        The existing ``state.config_store``, ``state.secrets_manager._store``
        and ``state.runtime_config.store`` each hold a direct reference
        to the old store (for performance — the hot path doesn't pay
        for a getter indirection). The set_database handler updates
        all three pointers inside a single lock scope so no admin
        call can see a half-swapped state.

        The secrets cache on the new store must already be populated
        (via ``upsert_secret`` on each old secret) before this runs —
        otherwise the first ``secret_resolver`` call after the swap
        would return None and break config expansion.
        """
        old = state.config_store
        state.config_store = new_store
        # Private attribute by design — see ``SecretsManager.__init__``.
        state.secrets_manager._store = new_store  # type: ignore[attr-defined]
        state.runtime_config.store = new_store
        # Dispose the old engine outside the caller's hot path; swallow
        # errors so a broken dispose doesn't leave the swap half-done.
        try:
            old.close()
        except Exception as exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).warning(
                "storage: old engine dispose raised after hot-swap: %s", exc
            )

    @app.post("/admin/api/onboarding/set_database")
    async def admin_api_onboarding_set_database(request: Request) -> JSONResponse:
        """Point the proxy at a new database via the onboarding wizard.

        Writes ``<state_dir>/bootstrap.json`` with the new URL and
        attempts a hot-swap so the wizard can continue in the same
        request without a restart. If the hot-swap fails (reference
        holder we didn't catch, driver crash inside the pool, …) the
        bootstrap file is *still* left in place, and the response sets
        ``mode: "restart_required"`` — on the next start, the existing
        ``cli.build_state`` path picks up the new URL automatically.

        Requires the operator to acknowledge the Fernet-key warning
        (``secrets_key_ack``) whenever they're switching dialect, so
        nobody accidentally migrates to a remote Postgres without
        realising they need to carry ``secrets.key`` along.
        """
        _require_onboarding_access(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        try:
            new_url = _build_url_from_body(body)
        except HTTPException:
            raise
        new_dialect = dialect_of(new_url)
        current_url = str(state.config_store.engine.url)
        current_dialect = dialect_of(current_url)
        dialect_changing = new_dialect != current_dialect
        secrets_key_ack = bool(body.get("secrets_key_ack"))
        if dialect_changing and not secrets_key_ack:
            raise HTTPException(
                status_code=400,
                detail=(
                    "switching dialect requires 'secrets_key_ack': true — "
                    "the Fernet key at <state_dir>/secrets.key (or the "
                    "MCPXY_SECRETS_KEY env var) must be reachable from "
                    "wherever the proxy runs, otherwise encrypted secrets "
                    "cannot be decrypted after the swap."
                ),
            )
        sd = _state_dir()
        async with _database_swap_lock:
            # 1. Probe the target URL and run migrations via open_store
            #    so we know it's usable before touching anything live.
            try:
                probe_connection(new_url)
            except DatabaseError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            fernet = state.secrets_manager._store._fernet  # type: ignore[attr-defined]
            try:
                new_store = open_store(
                    new_url, fernet=fernet, state_dir=sd
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"failed to open target database: {exc}",
                )
            # 2. Copy pre-onboarding state into the new store. This is
            #    a very small amount of data during onboarding: one
            #    active-config row, any secrets the user happened to
            #    create, and an onboarding row with the wizard's
            #    current stamp (which is always pre-token here — the
            #    gate rejects this endpoint once the token is set).
            try:
                active = state.config_store.get_active_config()
                if active is not None:
                    new_store.save_active_config(
                        active, source="onboarding.set_database"
                    )
                # Copy secrets through the plaintext cache so the new
                # store encrypts with its own (matching) Fernet handle.
                old_cache = dict(
                    state.config_store._secrets_cache  # type: ignore[attr-defined]
                )
                for name, rec in old_cache.items():
                    new_store.upsert_secret(
                        name, rec.value, description=rec.description
                    )
                new_store.ensure_onboarding_row()
            except Exception as exc:
                # The new DB is usable but we couldn't seed it. Clean
                # up the engine and refuse the swap; bootstrap file
                # hasn't been written yet, so nothing on disk changed.
                try:
                    new_store.close()
                except Exception:
                    pass
                raise HTTPException(
                    status_code=500,
                    detail=f"failed to seed target database: {exc}",
                )
            # 3. Persist bootstrap.json *before* the hot-swap so a
            #    crash mid-swap still leaves the next start on the new
            #    URL. The operator's next action after a crash is a
            #    restart anyway.
            try:
                write_bootstrap(
                    sd,
                    BootstrapConfig(
                        db_url=new_url,
                        written_by=_client_ip(request),
                    ),
                )
            except OSError as exc:
                try:
                    new_store.close()
                except Exception:
                    pass
                raise HTTPException(
                    status_code=500,
                    detail=f"failed to write bootstrap.json: {exc}",
                )
            # 4. Hot-swap the store references. If this step raises,
            #    we fall back to restart-required mode: the bootstrap
            #    file is already written, so the next start picks up
            #    the new URL without any operator action.
            mode = "hot_swap"
            try:
                _hot_swap_store(new_store)
            except Exception as exc:
                logging.getLogger(__name__).error(
                    "storage: hot-swap failed, falling back to restart: %s", exc
                )
                mode = "restart_required"
                try:
                    new_store.close()
                except Exception:
                    pass
        state.runtime_config.telemetry.emit_nowait(
            {
                "event": "onboarding_set_database",
                "dialect": new_dialect,
                "mode": mode,
            }
        )
        return JSONResponse(
            {
                "ok": True,
                "mode": mode,
                "onboarding": _onboarding_public(_current_onboarding()),
            }
        )

    @app.get("/admin/api/config")
    async def admin_api_config(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        return JSONResponse(await call_admin_method("admin.get_config", {}))

    @app.post("/admin/api/config")
    async def admin_api_apply_config(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        body = await request.json()
        return JSONResponse(await call_admin_method("admin.apply_config", body))

    @app.post("/admin/api/config/validate")
    async def admin_api_validate_config(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        body = await request.json()
        return JSONResponse(await call_admin_method("admin.validate_config", body))

    @app.get("/admin/api/upstreams")
    async def admin_api_upstreams(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        return JSONResponse(await call_admin_method("admin.list_upstreams", {}))

    @app.post("/admin/api/restart")
    async def admin_api_restart(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        body = await request.json()
        return JSONResponse(await call_admin_method("admin.restart_upstream", body))

    @app.get("/admin/api/telemetry")
    async def admin_api_telemetry(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        return JSONResponse(state.runtime_config.telemetry.health())

    @app.post("/admin/api/telemetry")
    async def admin_api_send_telemetry(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        body = await request.json()
        return JSONResponse(await call_admin_method("admin.send_telemetry", body))

    @app.get("/admin/api/logs")
    async def admin_api_logs(request: Request, upstream: str | None = None, level: str | None = None) -> JSONResponse:
        await require_admin_auth(request)
        return JSONResponse(await call_admin_method("admin.get_logs", {"upstream": upstream, "level": level}))

    @app.get("/admin/api/traffic")
    async def admin_api_traffic(
        request: Request,
        limit: int = 200,
        upstream: str | None = None,
        method: str | None = None,
        status: str | None = None,
    ) -> JSONResponse:
        await require_admin_auth(request)
        limit = max(1, min(limit, 2000))
        return JSONResponse(
            {
                "items": state.traffic.recent(
                    limit=limit, upstream=upstream, method=method, status=status
                )
            }
        )

    @app.get("/admin/api/traffic/stream")
    async def admin_api_traffic_stream(request: Request) -> StreamingResponse:
        await require_admin_auth(request)
        # Register the subscription synchronously, before the response body
        # starts streaming, so we cannot miss records that arrive between
        # the initial snapshot and the first await.
        subscription = state.traffic.subscribe()

        async def event_source() -> AsyncIterator[bytes]:
            try:
                recent = state.traffic.recent(limit=100)
                yield f"event: snapshot\ndata: {json.dumps({'items': recent})}\n\n".encode("utf-8")
                while True:
                    try:
                        rec = await asyncio.wait_for(subscription.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": heartbeat\n\n"
                        continue
                    if await request.is_disconnected():
                        return
                    payload = json.dumps(rec.to_dict())
                    yield f"data: {payload}\n\n".encode("utf-8")
            finally:
                subscription.close()

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/admin/api/metrics")
    async def admin_api_metrics(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        metrics = state.traffic.metrics()
        metrics["uptime_s"] = round(time.time() - state.started_at, 3)
        return JSONResponse(metrics)

    @app.get("/admin/api/routes")
    async def admin_api_routes(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        return JSONResponse(state.route_discovery.snapshot())

    @app.post("/admin/api/routes/refresh")
    async def admin_api_routes_refresh(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        await state.route_discovery.refresh_now()
        return JSONResponse(state.route_discovery.snapshot())

    @app.get("/admin/api/policies")
    async def admin_api_policies_get(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        return JSONResponse(
            state.runtime_config.config.policies.model_dump(by_alias=True, mode="json")
        )

    @app.post("/admin/api/policies")
    async def admin_api_policies_set(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        body = await request.json()
        policies = body.get("policies", body)
        # Merge with the rest of the live raw config so the apply pipeline
        # validates a complete document and the existing diff/rollback
        # behavior covers the change.
        merged = deepcopy(state.raw_config)
        merged["policies"] = policies
        result = await state.runtime_config.apply(
            merged,
            dry_run=bool(body.get("dry_run", False)),
            source="admin.api.policies",
        )
        return JSONResponse(result)

    @app.get("/admin/api/install/clients")
    async def admin_api_install_clients(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        return JSONResponse({"clients": list_clients()})

    @app.get("/admin/api/install/{client}")
    async def admin_api_install_snippet(
        request: Request,
        client: str,
        url: str | None = None,
        token_env: str | None = None,
        upstream: str | None = None,
        name: str = "mcpxy",
    ) -> JSONResponse:
        await require_admin_auth(request)
        try:
            adapter = get_adapter(client)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        opts = InstallOptions(
            name=name,
            url=url or "http://127.0.0.1:8000",
            token_env=token_env or state.runtime_config.config.auth.token_env,
            upstream=upstream,
        )
        return JSONResponse(
            {
                "client": client,
                "supports_auto_install": adapter.supports_auto_install(),
                "entry": adapter.format_entry(opts),
                "merged": adapter.merge(None, opts),
                "config_paths": [str(p) for p in adapter.default_config_paths()],
            }
        )

    @app.get("/admin/api/upstreams/registered")
    async def admin_api_registered(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        return JSONResponse(state.registration.snapshot())

    @app.post("/admin/api/upstreams")
    async def admin_api_register(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        body = await request.json()
        name = body.get("name")
        definition = body.get("config") or body.get("definition")
        replace = bool(body.get("replace", False))
        if not name or not isinstance(definition, dict):
            raise HTTPException(status_code=400, detail="body requires 'name' and 'config' object")
        try:
            result = await state.registration.add(
                name=str(name),
                definition=definition,
                replace=replace,
                source="admin.api.register",
            )
        except RegistrationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not result.get("applied") and not result.get("dry_run"):
            raise HTTPException(status_code=400, detail=result.get("error", "registration failed"))
        return JSONResponse(result)

    @app.delete("/admin/api/upstreams/{name}")
    async def admin_api_unregister(request: Request, name: str) -> JSONResponse:
        await require_admin_auth(request)
        try:
            result = await state.registration.remove(name=name, source="admin.api.unregister")
        except RegistrationError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not result.get("applied"):
            raise HTTPException(status_code=400, detail=result.get("error", "unregister failed"))
        return JSONResponse(result)

    @app.get("/admin/api/discovery/clients")
    async def admin_api_discovery_clients(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        return JSONResponse(discover_all())

    @app.get("/admin/api/discovery/clients/{client}")
    async def admin_api_discovery_client(request: Request, client: str) -> JSONResponse:
        await require_admin_auth(request)
        try:
            importer = get_importer(client)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        path = importer.find_config()
        upstreams = importer.read() if path is not None else []
        return JSONResponse(
            {
                "client_id": importer.client_id,
                "display_name": importer.display_name,
                "config_path": str(path) if path else None,
                "detected": path is not None,
                "upstreams": [u.to_dict() for u in upstreams],
            }
        )

    @app.post("/admin/api/discovery/import")
    async def admin_api_discovery_import(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        body = await request.json()
        client = body.get("client")
        names = body.get("upstreams")
        replace = bool(body.get("replace", False))
        if not client or not isinstance(names, list):
            raise HTTPException(
                status_code=400,
                detail="body requires 'client' and 'upstreams' array",
            )
        try:
            importer = get_importer(str(client))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        discovered = {u.name: u for u in importer.read()}
        selected: list[tuple[str, dict[str, Any]]] = []
        missing: list[str] = []
        for name in names:
            upstream = discovered.get(str(name))
            if upstream is None:
                missing.append(str(name))
                continue
            selected.append((upstream.name, upstream.config))
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"upstreams not found in {client}: {', '.join(missing)}",
            )
        try:
            result = await state.registration.bulk_add(
                selected,
                replace=replace,
                source=f"admin.api.import:{client}",
            )
        except RegistrationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not result.get("applied"):
            raise HTTPException(status_code=400, detail=result.get("error", "import failed"))
        return JSONResponse({**result, "imported": [n for n, _ in selected]})

    @app.get("/admin/api/catalog")
    async def admin_api_catalog(
        request: Request,
        q: str = "",
        category: str | None = None,
    ) -> JSONResponse:
        await require_admin_auth(request)
        if state.catalog is None:
            raise HTTPException(status_code=503, detail="catalog_unavailable")
        entries = state.catalog.search(q, category=category)
        return JSONResponse(
            {
                "version": state.catalog.version,
                "updated_at": state.catalog.updated_at,
                "categories": state.catalog.categories(),
                "entries": [e.to_dict() for e in entries],
            }
        )

    @app.get("/admin/api/secrets")
    async def admin_api_secrets_list(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        # Raw secret values never leave the process; list_public returns
        # name + metadata + a masked preview only. ``referenced`` enumerates
        # every ${secret:NAME} found in the current config so the UI can
        # surface orphans and dangling references.
        referenced = find_secret_references(state.raw_config)
        known = set(state.secrets_manager.known_names())
        return JSONResponse(
            {
                "secrets": state.secrets_manager.list_public(),
                "referenced": referenced,
                "missing": sorted(n for n in referenced if n not in known),
                "orphans": sorted(n for n in known if n not in set(referenced)),
            }
        )

    @app.post("/admin/api/secrets")
    async def admin_api_secrets_upsert(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        body = await request.json()
        name = body.get("name")
        value = body.get("value")
        description = body.get("description", "") or ""
        if not isinstance(name, str) or not isinstance(value, str):
            raise HTTPException(
                status_code=400,
                detail="body requires 'name' and 'value' strings",
            )
        try:
            rec = await state.secrets_manager.set(
                name, value, description=description
            )
        except SecretStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        state.runtime_config.telemetry.emit_nowait(
            {"event": "secret_upserted", "name": name}
        )
        return JSONResponse({"secret": rec.to_public_dict()})

    @app.delete("/admin/api/secrets/{name}")
    async def admin_api_secrets_delete(request: Request, name: str) -> JSONResponse:
        await require_admin_auth(request)
        try:
            removed = await state.secrets_manager.delete(name)
        except SecretStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not removed:
            raise HTTPException(status_code=404, detail=f"secret {name!r} not found")
        state.runtime_config.telemetry.emit_nowait(
            {"event": "secret_deleted", "name": name}
        )
        return JSONResponse({"deleted": True, "name": name})

    # ------------------------------------------------------------------
    # OAuth upstream flow
    # ------------------------------------------------------------------

    @app.get("/admin/api/oauth")
    async def admin_api_oauth_list(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        names: list[str] = []
        for name, up in state.config.upstreams.items():
            if isinstance(up, HttpUpstreamConfig) and isinstance(up.auth, OAuth2AuthConfig):
                names.append(name)
        return JSONResponse(
            {
                "upstreams": [state.oauth_manager.status(n) for n in sorted(names)]
            }
        )

    @app.get("/admin/api/oauth/{upstream}/status")
    async def admin_api_oauth_status(request: Request, upstream: str) -> JSONResponse:
        await require_admin_auth(request)
        return JSONResponse(state.oauth_manager.status(upstream))

    @app.post("/admin/api/oauth/{upstream}/start")
    async def admin_api_oauth_start(request: Request, upstream: str) -> JSONResponse:
        await require_admin_auth(request)
        body: dict[str, Any] = {}
        if request.headers.get("content-length") not in (None, "0"):
            try:
                body = await request.json()
            except ValueError:
                body = {}
        redirect_uri = body.get("redirect_uri")
        try:
            result = await state.oauth_manager.start_authorization(
                upstream, redirect_uri=redirect_uri
            )
        except OAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        state.runtime_config.telemetry.emit_nowait(
            {"event": "oauth_start", "upstream": upstream}
        )
        return JSONResponse(result)

    @app.get("/admin/api/oauth/callback")
    async def admin_api_oauth_callback(
        request: Request,
        code: str | None = None,
        state_arg: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        # No require_admin_auth here: the browser hitting this endpoint
        # after the auth-server redirect has no way to send a bearer
        # token. The ``state`` parameter (CSRF-bound) is the auth for
        # this path — it must match an in-memory PendingAuthorization
        # that the admin API created earlier via /start.
        raw_state = request.query_params.get("state") or state_arg
        if error:
            return HTMLResponse(
                f"<h1>Authorization failed</h1><p>{error}</p>",
                status_code=400,
            )
        if not code or not raw_state:
            raise HTTPException(
                status_code=400, detail="missing 'code' or 'state' query param"
            )
        try:
            token = await state.oauth_manager.finish_authorization(raw_state, code)
        except OAuthError as exc:
            return HTMLResponse(
                f"<h1>Authorization failed</h1><p>{exc}</p>",
                status_code=400,
            )
        state.runtime_config.telemetry.emit_nowait(
            {"event": "oauth_complete", "scopes": token.scope}
        )
        return HTMLResponse(
            "<!doctype html><title>MCPxy OAuth</title>"
            "<h1>Authorization complete</h1>"
            "<p>You can close this tab and return to MCPxy.</p>",
            status_code=200,
        )

    @app.delete("/admin/api/oauth/{upstream}/token")
    async def admin_api_oauth_revoke(request: Request, upstream: str) -> JSONResponse:
        await require_admin_auth(request)
        removed = await state.oauth_manager.revoke_tokens(upstream)
        state.runtime_config.telemetry.emit_nowait(
            {"event": "oauth_revoke", "upstream": upstream, "had_token": removed}
        )
        return JSONResponse({"revoked": removed, "upstream": upstream})

    @app.post("/admin/api/catalog/install")
    async def admin_api_catalog_install(request: Request) -> JSONResponse:
        await require_admin_auth(request)
        if state.catalog is None:
            raise HTTPException(status_code=503, detail="catalog_unavailable")
        body = await request.json()
        entry_id = body.get("id")
        name = body.get("name")
        variables = body.get("variables") or {}
        replace = bool(body.get("replace", False))
        if not entry_id:
            raise HTTPException(status_code=400, detail="body requires 'id'")
        entry = state.catalog.get(str(entry_id))
        if entry is None:
            raise HTTPException(status_code=404, detail=f"catalog entry '{entry_id}' not found")
        try:
            resolved_name, definition = entry.materialize(
                name=name if name else None,
                variables=variables,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            result = await state.registration.add(
                name=resolved_name,
                definition=definition,
                replace=replace,
                source=f"admin.api.catalog:{entry_id}",
            )
        except RegistrationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not result.get("applied"):
            raise HTTPException(status_code=400, detail=result.get("error", "catalog install failed"))
        return JSONResponse({**result, "installed": {"id": entry_id, "name": resolved_name}})

    # SPA catch-all registered LAST so specific /admin/api/* and /admin/static/*
    # routes match first. Handles deep-link navigation like /admin/traffic.
    @app.get("/admin/{path:path}", response_class=HTMLResponse)
    async def admin_spa(_request: Request, path: str) -> HTMLResponse:
        if path.startswith("api/") or path.startswith("static/"):
            raise HTTPException(status_code=404, detail="not_found")
        # SPA HTML is public; /admin/api/* remains auth-gated.
        return HTMLResponse(_dashboard_html())

    return app
