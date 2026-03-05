"""Internal admin MCP handler."""

from __future__ import annotations

import inspect
import logging
from copy import deepcopy
from typing import Any

from mcp_proxy.config import AppConfig, redact_secrets, validate_config_payload


class AdminService:
    """MCP admin service methods for runtime operations."""

    def __init__(self, config: AppConfig, manager: Any, telemetry: Any, raw_config: dict[str, Any]) -> None:
        self.config = config
        self.manager = manager
        self.telemetry = telemetry
        self.raw_config = raw_config

    async def handle(self, message: dict[str, Any], health_provider: Any) -> dict[str, Any]:
        """Handle admin JSON-RPC request."""
        method = message.get("method")
        params = message.get("params", {})
        msg_id = message.get("id")

        methods: dict[str, Any] = {
            "admin.get_config": self.get_config,
            "admin.validate_config": self.validate_config,
            "admin.apply_config": self.apply_config,
            "admin.list_upstreams": self.list_upstreams,
            "admin.restart_upstream": self.restart_upstream,
            "admin.set_log_level": self.set_log_level,
            "admin.send_telemetry": self.send_telemetry,
            "admin.get_health": lambda _params: health_provider(),
        }
        fn = methods.get(method)
        if fn is None:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Method not found"}}
        try:
            value = fn(params)
            result = await value if inspect.isawaitable(value) else value
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(exc)}}

    def get_config(self, _params: dict[str, Any]) -> dict[str, Any]:
        return redact_secrets(deepcopy(self.raw_config))

    def validate_config(self, params: dict[str, Any]) -> dict[str, Any]:
        candidate = params.get("config", {})
        ok, error = validate_config_payload(candidate)
        return {"valid": ok, "error": error}

    def apply_config(self, params: dict[str, Any]) -> dict[str, Any]:
        candidate = params.get("config", {})
        dry_run = bool(params.get("dry_run", False))
        ok, error = validate_config_payload(candidate)
        if not ok:
            return {"applied": False, "error": error, "rolled_back": True}
        if dry_run:
            return {"applied": False, "dry_run": True, "rolled_back": False}
        backup = deepcopy(self.raw_config)
        try:
            self.raw_config.clear()
            self.raw_config.update(candidate)
            return {"applied": True, "rolled_back": False}
        except Exception as exc:
            self.raw_config.clear()
            self.raw_config.update(backup)
            return {"applied": False, "error": str(exc), "rolled_back": True}

    def list_upstreams(self, _params: dict[str, Any]) -> dict[str, Any]:
        return self.manager.health()

    async def restart_upstream(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not name:
            return {"restarted": False, "error": "missing upstream name"}
        ok = await self.manager.restart(name)
        return {"restarted": ok}

    def set_log_level(self, params: dict[str, Any]) -> dict[str, Any]:
        level = str(params.get("level", "INFO"))
        logging.getLogger().setLevel(level.upper())
        return {"level": level.upper()}

    def send_telemetry(self, params: dict[str, Any]) -> dict[str, Any]:
        event = params.get("event", {})
        enq = self.telemetry.emit_nowait({"source": "admin", **event})
        return {"enqueued": enq}
