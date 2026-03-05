"""Configuration models and loading logic."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator


ENV_RE = re.compile(r"\$\{env:([A-Z0-9_]+)\}")


class AuthConfig(BaseModel):
    """Authentication settings."""

    token_env: str | None = None


class AdminConfig(BaseModel):
    """Admin MCP endpoint settings."""

    mount_name: str = "__admin__"
    enabled: bool = True
    require_token: bool = True
    allowed_clients: list[str] = Field(default_factory=list)


class TelemetryConfig(BaseModel):
    """Telemetry pipeline settings."""

    enabled: bool = True
    sink: str = "noop"
    endpoint: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    batch_size: int = Field(default=50, strict=True, ge=1)
    flush_interval_ms: int = Field(default=2000, strict=True, ge=1)
    queue_max: int = Field(default=1000, strict=True, ge=1)
    drop_policy: Literal["drop_oldest", "drop_newest"] = "drop_newest"


class StdioUpstreamConfig(BaseModel):
    """Stdio upstream configuration."""

    type: Literal["stdio"]
    command: str
    args: list[str] = Field(default_factory=list)
    queue_size: int = 200


class HttpUpstreamConfig(BaseModel):
    """HTTP upstream configuration."""

    type: Literal["http"]
    url: str
    timeout_s: float = 30.0


UpstreamConfig = StdioUpstreamConfig | HttpUpstreamConfig | dict[str, Any]


class AppConfig(BaseModel):
    """Top-level application configuration."""

    default_upstream: str | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    upstreams: dict[str, UpstreamConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_default_upstream(self) -> "AppConfig":
        if self.default_upstream and self.default_upstream not in self.upstreams:
            raise ValueError("default_upstream must exist in upstreams")
        return self


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            return os.getenv(match.group(1), "")
        return ENV_RE.sub(repl, value)
    return value


def load_config(path: str | Path) -> AppConfig:
    """Load and validate config from JSON file."""
    data = json.loads(Path(path).read_text())
    expanded = _expand_env(data)
    return AppConfig.model_validate(expanded)


def validate_config_payload(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Validate an in-memory config payload."""
    try:
        AppConfig.model_validate(_expand_env(deepcopy(payload)))
    except ValidationError as exc:
        return False, str(exc)
    except ValueError as exc:
        return False, str(exc)
    return True, None


def redact_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact secret-like values from config payload."""
    redacted = deepcopy(payload)
    headers = redacted.get("telemetry", {}).get("headers", {})
    for key in list(headers.keys()):
        if "key" in key.lower() or "token" in key.lower() or "auth" in key.lower():
            headers[key] = "***REDACTED***"
    token_env = redacted.get("auth", {}).get("token_env")
    if token_env:
        redacted["auth"]["token_env"] = "***REDACTED_ENV***"
    return redacted
