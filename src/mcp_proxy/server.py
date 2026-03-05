"""FastAPI server implementation."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from mcp_proxy.config import AppConfig
from mcp_proxy.jsonrpc import JsonRpcError, is_notification
from mcp_proxy.proxy.admin import AdminService
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.routing import resolve_upstream
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


class AppState:
    """Runtime state container."""

    def __init__(
        self,
        config: AppConfig,
        raw_config: dict[str, Any],
        manager: UpstreamManager,
        bridge: ProxyBridge,
        telemetry: TelemetryPipeline,
    ) -> None:
        self.config = config
        self.raw_config = raw_config
        self.manager = manager
        self.bridge = bridge
        self.telemetry = telemetry


def _parse_ndjson(body: bytes) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for line in body.decode("utf-8").splitlines():
        if line.strip():
            messages.append(json.loads(line))
    return messages


def _get_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return auth.removeprefix("Bearer ").strip()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def create_app(state: AppState) -> FastAPI:
    """Create configured FastAPI app."""
    app = FastAPI(title="MCPy Proxy")
    admin_service = AdminService(state.config, state.manager, state.telemetry, state.raw_config)

    def require_auth_if_needed(request: Request) -> None:
        token_env = state.config.auth.token_env
        if token_env:
            expected = __import__("os").getenv(token_env)
            if expected and _get_bearer(request) != expected:
                raise HTTPException(status_code=401, detail="unauthorized")

    def require_admin_auth(request: Request) -> None:
        admin = state.config.admin
        if admin.allowed_clients and _client_ip(request) not in admin.allowed_clients:
            raise HTTPException(status_code=403, detail="forbidden")
        if admin.require_token:
            token_env = state.config.auth.token_env
            expected = __import__("os").getenv(token_env) if token_env else None
            if expected and _get_bearer(request) != expected:
                raise HTTPException(status_code=401, detail="unauthorized")

    async def parse_messages(request: Request) -> list[dict[str, Any]]:
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
        body = await request.body()
        if ctype == "application/x-ndjson":
            return _parse_ndjson(body)
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, list):
            return parsed
        return [parsed]

    async def stream_responses(responses: list[dict[str, Any]]) -> AsyncIterator[bytes]:
        for item in responses:
            yield (json.dumps(item) + "\n").encode("utf-8")

    async def handle_proxy(request: Request, path_name: str | None, x_mcp_upstream: str | None) -> Response:
        require_auth_if_needed(request)
        messages = await parse_messages(request)
        responses: list[dict[str, Any]] = []

        for msg in messages:
            upstream, cleaned = resolve_upstream(msg, state.config, path_name, x_mcp_upstream)
            if state.config.admin.enabled and upstream == state.config.admin.mount_name:
                require_admin_auth(request)
                resp = await admin_service.handle(cleaned, lambda: build_health())
                if not is_notification(msg):
                    responses.append(resp)
                continue
            if upstream is None:
                err = JsonRpcError(-32602, "upstream_not_resolved", request_id=msg.get("id")).to_response()
                if not is_notification(msg):
                    responses.append(err)
                continue
            try:
                out = await state.bridge.forward(upstream, cleaned)
                if out is not None:
                    responses.append(out)
            except JsonRpcError as exc:
                if not is_notification(msg):
                    responses.append(exc.to_response())

        if not responses:
            return Response(status_code=202)
        return StreamingResponse(stream_responses(responses), media_type="application/x-ndjson")

    def build_health() -> dict[str, Any]:
        return {
            "status": "ok",
            "upstreams": state.manager.health(),
            "telemetry": state.telemetry.health(),
        }

    @app.on_event("startup")
    async def on_startup() -> None:
        await state.manager.start()
        await state.telemetry.start()
        state.telemetry.emit_nowait({"event": "proxy_startup"})

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        await state.manager.stop()
        await state.telemetry.stop()

    @app.post("/mcp")
    async def post_mcp(request: Request, x_mcp_upstream: str | None = Header(default=None)) -> Response:
        return await handle_proxy(request, None, x_mcp_upstream)

    @app.post("/mcp/{name}")
    async def post_mcp_named(name: str, request: Request, x_mcp_upstream: str | None = Header(default=None)) -> Response:
        return await handle_proxy(request, name, x_mcp_upstream)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(build_health())

    return app
