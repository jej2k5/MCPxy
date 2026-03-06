"""FastAPI server implementation."""

from __future__ import annotations

import json
import logging
import signal
import time
from codecs import getincrementaldecoder
import asyncio
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from mcp_proxy.config import AppConfig
from mcp_proxy.jsonrpc import JsonRpcError, is_notification
from mcp_proxy.proxy.admin import AdminService
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.routing import resolve_upstream
from mcp_proxy.runtime import RuntimeConfigManager
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


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
    ) -> None:
        self.config = config
        self.raw_config = raw_config
        self.manager = manager
        self.bridge = bridge
        self.telemetry = telemetry
        self.registry = registry
        self.config_path = config_path
        self.started_at = time.time()
        self.log_buffer: deque[dict[str, Any]] = deque(maxlen=400)
        self.runtime_config = RuntimeConfigManager(
            raw_config=self.raw_config,
            config=self.config,
            manager=self.manager,
            telemetry=self.telemetry,
            registry=self.registry,
            config_path=self.config_path,
        )


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
    app = FastAPI(title="MCPy Proxy")
    admin_service = AdminService(state.manager, state.telemetry, state.raw_config, state.runtime_config, state.log_buffer)
    state.bridge.set_telemetry_emitter(state.runtime_config.telemetry.emit_nowait)

    root_logger = logging.getLogger()
    root_logger.addHandler(InMemoryLogHandler(state.log_buffer))

    def require_auth_if_needed(request: Request) -> None:
        token_env = state.runtime_config.config.auth.token_env
        if token_env:
            expected = __import__("os").getenv(token_env)
            if expected and _get_bearer(request) != expected:
                raise HTTPException(status_code=401, detail="unauthorized")

    def require_admin_auth(request: Request) -> None:
        admin = state.runtime_config.config.admin
        if admin.allowed_clients and _client_ip(request) not in admin.allowed_clients:
            raise HTTPException(status_code=403, detail="forbidden")
        if admin.require_token:
            token_env = state.runtime_config.config.auth.token_env
            expected = __import__("os").getenv(token_env) if token_env else None
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
                    out = await state.bridge.forward(upstream, cleaned)
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

    def install_signal_handlers() -> None:
        try:
            signal.signal(signal.SIGINT, handle_shutdown_signal)
            signal.signal(signal.SIGTERM, handle_shutdown_signal)
        except ValueError:
            return

    app.state.handle_shutdown_signal = handle_shutdown_signal

    web_root = Path(__file__).parent / "web"
    app.mount("/admin/static", StaticFiles(directory=str(web_root / "static")), name="admin-static")

    @app.on_event("startup")
    async def on_startup() -> None:
        install_signal_handlers()
        await state.manager.start()
        await state.runtime_config.telemetry.start()
        state.runtime_config.telemetry.emit_nowait({"event": "proxy_startup"})
        await state.runtime_config.start()

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        state.bridge.start_shutdown()
        state.runtime_config.telemetry.emit_nowait({"event": "proxy_shutdown_start"})
        await state.runtime_config.stop()
        await state.manager.stop()
        state.runtime_config.telemetry.emit_nowait({"event": "proxy_shutdown_complete"})
        await state.runtime_config.telemetry.stop()

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

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_index(request: Request) -> HTMLResponse:
        require_admin_auth(request)
        return HTMLResponse((web_root / "templates" / "admin.html").read_text(encoding="utf-8"))

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

    return app
