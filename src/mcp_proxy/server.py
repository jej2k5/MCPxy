"""FastAPI server implementation."""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from codecs import getincrementaldecoder
import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from mcp_proxy.auth.oauth import (
    OAuthError,
    OAuthManager,
    OAuthNotAuthorizedError,
)
from mcp_proxy.config import (
    AppConfig,
    HttpUpstreamConfig,
    OAuth2AuthConfig,
    find_secret_references,
    resolve_admin_token,
)
from mcp_proxy.discovery.catalog import Catalog, load_catalog
from mcp_proxy.discovery.importers import IMPORTERS, discover_all, get_importer
from mcp_proxy.discovery.registration import (
    DEFAULT_DROP_DIR,
    FileDropWatcher,
    RegistrationError,
    RegistrationService,
)
from mcp_proxy.install.clients import InstallOptions, get_adapter, list_clients
from mcp_proxy.jsonrpc import JsonRpcError, is_notification
from mcp_proxy.observability.discovery import RouteDiscoverer
from mcp_proxy.observability.traffic import TrafficRecorder
from mcp_proxy.policy.engine import PolicyEngine
from mcp_proxy.proxy.admin import AdminService
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.routing import resolve_upstream
from mcp_proxy.runtime import RuntimeConfigManager
from mcp_proxy.secrets import SecretsManager, SecretStoreError
from mcp_proxy.storage.config_store import ConfigStore, OnboardingState
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


# Onboarding bypass TTL: if the wizard isn't completed within this window
# after the first-run row is created, the onboarding endpoints stop
# accepting writes and return 410. Operators who need longer can set
# MCPY_ONBOARDING_TTL_S (seconds) before starting the proxy.
DEFAULT_ONBOARDING_TTL_S = 30 * 60

# Client IPs that are allowed to hit the onboarding endpoints. Limited
# to loopback by default because the endpoints are unauthenticated;
# operators running the proxy behind an ingress that rewrites the
# client IP can override via MCPY_ONBOARDING_ALLOWED_CLIENTS
# (comma-separated list, e.g. ``127.0.0.1,10.0.0.5``).
_DEFAULT_ONBOARDING_ALLOWED_CLIENTS: tuple[str, ...] = (
    "127.0.0.1",
    "::1",
    "localhost",
    "testclient",  # FastAPI TestClient
)


def _onboarding_ttl() -> float:
    raw_env = os.getenv("MCPY_ONBOARDING_TTL_S")
    if raw_env is None:
        return float(DEFAULT_ONBOARDING_TTL_S)
    try:
        return max(60.0, float(raw_env))
    except ValueError:
        return float(DEFAULT_ONBOARDING_TTL_S)


def _onboarding_allowed_clients() -> set[str]:
    raw = os.getenv("MCPY_ONBOARDING_ALLOWED_CLIENTS")
    if not raw:
        return set(_DEFAULT_ONBOARDING_ALLOWED_CLIENTS)
    return {item.strip() for item in raw.split(",") if item.strip()}


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
        # MCPY_SECRETS_KEY is the single point of control for all
        # upstream auth state.
        self.oauth_manager = oauth_manager or OAuthManager(secrets=self.secrets_manager)
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
            on_config_applied=self._register_oauth_configs,
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

    app = FastAPI(title="MCPy Proxy", lifespan=lifespan)
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

    def require_auth_if_needed(request: Request) -> None:
        expected = _resolve_admin_bearer()
        if expected and _get_bearer(request) != expected:
            raise HTTPException(status_code=401, detail="unauthorized")

    def require_admin_auth(request: Request) -> None:
        admin = state.runtime_config.config.admin
        if admin.allowed_clients and _client_ip(request) not in admin.allowed_clients:
            raise HTTPException(status_code=403, detail="forbidden")
        if admin.require_token:
            expected = _resolve_admin_bearer()
            if not expected:
                raise HTTPException(status_code=500, detail="admin_token_not_configured")
            if _get_bearer(request) != expected:
                raise HTTPException(status_code=401, detail="unauthorized")

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
        require_auth_if_needed(request)
        client_ip = _client_ip(request)
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
                    require_admin_auth(request)
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
            "<!doctype html><meta charset='utf-8'><title>MCPy Admin</title>"
            "<h1>MCPy Admin</h1><p>Dashboard assets are not built. "
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

    def _current_onboarding() -> OnboardingState | None:
        return state.config_store.get_onboarding_state()

    def _onboarding_public(obstate: OnboardingState | None) -> dict[str, Any]:
        if obstate is None:
            return {
                "active": False,
                "completed": False,
                "expired": False,
                "required": False,
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
        allowed = _onboarding_allowed_clients()
        client_ip = _client_ip(request)
        if allowed and client_ip not in allowed:
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
        """Block non-onboarding admin API traffic while the wizard is active.

        While onboarding is still pending we intentionally run with
        ``auth.require_token=False`` so the wizard is reachable. The
        side effect is that every other admin endpoint would be
        unauthenticated too — which is exactly the footgun this
        middleware closes. Non-onboarding ``/admin/api/*`` calls get a
        503 with ``onboarding_required=true`` so the dashboard client
        redirects to the wizard instead of rendering broken data.

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
        obstate = _current_onboarding()
        if obstate is None:
            return await call_next(request)
        public = _onboarding_public(obstate)
        if public["required"]:
            return JSONResponse(
                {
                    "detail": "onboarding_required",
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

    @app.get("/admin/api/config")
    async def admin_api_config(request: Request) -> JSONResponse:
        require_admin_auth(request)
        return JSONResponse(await call_admin_method("admin.get_config", {}))

    @app.post("/admin/api/config")
    async def admin_api_apply_config(request: Request) -> JSONResponse:
        require_admin_auth(request)
        body = await request.json()
        return JSONResponse(await call_admin_method("admin.apply_config", body))

    @app.post("/admin/api/config/validate")
    async def admin_api_validate_config(request: Request) -> JSONResponse:
        require_admin_auth(request)
        body = await request.json()
        return JSONResponse(await call_admin_method("admin.validate_config", body))

    @app.get("/admin/api/upstreams")
    async def admin_api_upstreams(request: Request) -> JSONResponse:
        require_admin_auth(request)
        return JSONResponse(await call_admin_method("admin.list_upstreams", {}))

    @app.post("/admin/api/restart")
    async def admin_api_restart(request: Request) -> JSONResponse:
        require_admin_auth(request)
        body = await request.json()
        return JSONResponse(await call_admin_method("admin.restart_upstream", body))

    @app.get("/admin/api/telemetry")
    async def admin_api_telemetry(request: Request) -> JSONResponse:
        require_admin_auth(request)
        return JSONResponse(state.runtime_config.telemetry.health())

    @app.post("/admin/api/telemetry")
    async def admin_api_send_telemetry(request: Request) -> JSONResponse:
        require_admin_auth(request)
        body = await request.json()
        return JSONResponse(await call_admin_method("admin.send_telemetry", body))

    @app.get("/admin/api/logs")
    async def admin_api_logs(request: Request, upstream: str | None = None, level: str | None = None) -> JSONResponse:
        require_admin_auth(request)
        return JSONResponse(await call_admin_method("admin.get_logs", {"upstream": upstream, "level": level}))

    @app.get("/admin/api/traffic")
    async def admin_api_traffic(
        request: Request,
        limit: int = 200,
        upstream: str | None = None,
        method: str | None = None,
        status: str | None = None,
    ) -> JSONResponse:
        require_admin_auth(request)
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
        require_admin_auth(request)
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
        require_admin_auth(request)
        metrics = state.traffic.metrics()
        metrics["uptime_s"] = round(time.time() - state.started_at, 3)
        return JSONResponse(metrics)

    @app.get("/admin/api/routes")
    async def admin_api_routes(request: Request) -> JSONResponse:
        require_admin_auth(request)
        return JSONResponse(state.route_discovery.snapshot())

    @app.post("/admin/api/routes/refresh")
    async def admin_api_routes_refresh(request: Request) -> JSONResponse:
        require_admin_auth(request)
        await state.route_discovery.refresh_now()
        return JSONResponse(state.route_discovery.snapshot())

    @app.get("/admin/api/policies")
    async def admin_api_policies_get(request: Request) -> JSONResponse:
        require_admin_auth(request)
        return JSONResponse(
            state.runtime_config.config.policies.model_dump(by_alias=True, mode="json")
        )

    @app.post("/admin/api/policies")
    async def admin_api_policies_set(request: Request) -> JSONResponse:
        require_admin_auth(request)
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
        require_admin_auth(request)
        return JSONResponse({"clients": list_clients()})

    @app.get("/admin/api/install/{client}")
    async def admin_api_install_snippet(
        request: Request,
        client: str,
        url: str | None = None,
        token_env: str | None = None,
        upstream: str | None = None,
        name: str = "mcpy",
    ) -> JSONResponse:
        require_admin_auth(request)
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
        require_admin_auth(request)
        return JSONResponse(state.registration.snapshot())

    @app.post("/admin/api/upstreams")
    async def admin_api_register(request: Request) -> JSONResponse:
        require_admin_auth(request)
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
        require_admin_auth(request)
        try:
            result = await state.registration.remove(name=name, source="admin.api.unregister")
        except RegistrationError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not result.get("applied"):
            raise HTTPException(status_code=400, detail=result.get("error", "unregister failed"))
        return JSONResponse(result)

    @app.get("/admin/api/discovery/clients")
    async def admin_api_discovery_clients(request: Request) -> JSONResponse:
        require_admin_auth(request)
        return JSONResponse(discover_all())

    @app.get("/admin/api/discovery/clients/{client}")
    async def admin_api_discovery_client(request: Request, client: str) -> JSONResponse:
        require_admin_auth(request)
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
        require_admin_auth(request)
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
        require_admin_auth(request)
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
        require_admin_auth(request)
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
        require_admin_auth(request)
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
        require_admin_auth(request)
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
        require_admin_auth(request)
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
        require_admin_auth(request)
        return JSONResponse(state.oauth_manager.status(upstream))

    @app.post("/admin/api/oauth/{upstream}/start")
    async def admin_api_oauth_start(request: Request, upstream: str) -> JSONResponse:
        require_admin_auth(request)
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
            "<!doctype html><title>MCPy OAuth</title>"
            "<h1>Authorization complete</h1>"
            "<p>You can close this tab and return to MCPy.</p>",
            status_code=200,
        )

    @app.delete("/admin/api/oauth/{upstream}/token")
    async def admin_api_oauth_revoke(request: Request, upstream: str) -> JSONResponse:
        require_admin_auth(request)
        removed = await state.oauth_manager.revoke_tokens(upstream)
        state.runtime_config.telemetry.emit_nowait(
            {"event": "oauth_revoke", "upstream": upstream, "had_token": removed}
        )
        return JSONResponse({"revoked": removed, "upstream": upstream})

    @app.post("/admin/api/catalog/install")
    async def admin_api_catalog_install(request: Request) -> JSONResponse:
        require_admin_auth(request)
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
