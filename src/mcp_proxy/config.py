"""Configuration models and loading logic."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator


ENV_RE = re.compile(r"\$\{env:([A-Z0-9_]+)\}")
SECRET_RE = re.compile(r"\$\{secret:([A-Za-z0-9_][A-Za-z0-9_\-]*)\}")

# Resolver signature for ``${secret:NAME}`` expansion. Passed in by the
# caller (runtime / CLI / tests) so the config module has no hard dependency
# on :mod:`mcp_proxy.secrets`. A resolver returning ``None`` causes the
# placeholder to be replaced with an empty string — the same behaviour as
# missing ``${env:FOO}`` references, which keeps validation deterministic
# when secrets haven't been populated yet (e.g. during dry-run validation).
SecretResolver = Callable[[str], "str | None"]


class AuthConfig(BaseModel):
    """Authentication settings.

    Two ways to configure the admin bearer token, in priority order:

    - ``token`` — the literal bearer string, typically populated via a
      ``${secret:NAME}`` reference or (for the first-run onboarding
      wizard) directly into the DB config row. Always wins when set.
    - ``token_env`` — the name of an env var the proxy reads at
      request time. Left for backwards compatibility with file-based
      deployments that wire MCP_PROXY_TOKEN via ``.env`` or compose.

    :func:`mcp_proxy.config.resolve_admin_token` returns the effective
    bearer given an ``AuthConfig`` + an env lookup + a secret resolver,
    which is what the server's request-auth code calls. Direct callers
    should use that helper rather than reading either field directly.
    """

    token: str | None = None
    token_env: str | None = None


def resolve_admin_token(
    auth: "AuthConfig",
    *,
    env_lookup: Callable[[str], "str | None"] | None = None,
) -> str | None:
    """Return the configured bearer token, preferring ``auth.token``.

    ``auth.token`` has already been through ``${secret:NAME}``/
    ``${env:FOO}`` expansion by the time this runs, so we can treat it
    as a literal. If ``token`` is unset or empty we fall back to
    ``token_env`` (looked up via ``env_lookup`` so tests can inject a
    stub) for compatibility with the historical env-var path.
    """
    if auth.token:
        return auth.token
    if auth.token_env:
        if env_lookup is None:
            env_lookup = os.getenv
        return env_lookup(auth.token_env)
    return None


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


class MethodPolicy(BaseModel):
    """JSON-RPC method allow/deny lists with wildcard support."""

    allow: list[str] | None = None
    deny: list[str] | None = None


class RateLimitPolicy(BaseModel):
    """Per-upstream token-bucket rate limit."""

    requests_per_second: float = Field(gt=0)
    burst: int = Field(gt=0)
    scope: Literal["upstream", "client_ip", "both"] = "upstream"


class SizePolicy(BaseModel):
    """Request payload size cap."""

    max_request_bytes: int = Field(gt=0)


class UpstreamPolicies(BaseModel):
    """Policies applicable at a given scope (global or per-upstream)."""

    methods: MethodPolicy | None = None
    rate_limit: RateLimitPolicy | None = None
    size: SizePolicy | None = None


class PoliciesConfig(BaseModel):
    """Top-level policy configuration."""

    model_config = {"populate_by_name": True}

    global_: UpstreamPolicies | None = Field(default=None, alias="global")
    per_upstream: dict[str, UpstreamPolicies] = Field(default_factory=dict)


class StdioUpstreamConfig(BaseModel):
    """Stdio upstream configuration."""

    type: Literal["stdio"]
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    queue_size: int = 200


# ---------------------------------------------------------------------------
# HTTP upstream auth taxonomy
# ---------------------------------------------------------------------------
#
# Discriminated on ``type``. Every variant ultimately produces either a
# static set of HTTP request headers (``bearer``, ``api_key``, ``basic``,
# ``none``) or a dynamic per-request auth object (``oauth2``). The
# transport layer turns these into the actual outgoing ``Authorization``
# header; config validation only concerns itself with shape.
#
# All ``${env:FOO}`` and ``${secret:NAME}`` placeholders are expanded at
# config apply time, before these models are constructed, so the values
# you see inside a ``BearerAuthConfig.token`` are already the real tokens.


class NoAuthConfig(BaseModel):
    """Explicit ``{"type": "none"}`` — equivalent to omitting ``auth`` entirely."""

    type: Literal["none"] = "none"


class BearerAuthConfig(BaseModel):
    """HTTP ``Authorization: Bearer <token>`` static auth.

    The most common upstream auth shape (Notion, Linear, Anthropic, …).
    """

    type: Literal["bearer"]
    token: str = Field(min_length=1)


class ApiKeyAuthConfig(BaseModel):
    """Custom-header API key auth, e.g. ``X-Api-Key: <value>``."""

    type: Literal["api_key"]
    header: str = Field(default="X-Api-Key", min_length=1)
    value: str = Field(min_length=1)


class BasicAuthConfig(BaseModel):
    """HTTP Basic auth (RFC 7617) — username/password pair."""

    type: Literal["basic"]
    username: str = Field(min_length=1)
    password: str = Field(min_length=0)


class OAuth2AuthConfig(BaseModel):
    """OAuth 2.1 authorization-code + PKCE auth for HTTP upstreams.

    Fields mirror the MCP auth spec + RFC 8414 (OAuth 2.0 Authorization
    Server Metadata). ``issuer`` is preferred: if set, the runtime fetches
    ``<issuer>/.well-known/oauth-authorization-server`` to discover the
    authorization/token endpoints automatically. ``authorization_endpoint``
    and ``token_endpoint`` are escape hatches for providers that don't
    publish RFC 8414 metadata.

    ``dynamic_registration`` opts into RFC 7591 dynamic client
    registration — when true, the proxy registers itself at the auth
    server's registration endpoint instead of requiring a pre-issued
    ``client_id``/``client_secret`` pair.

    ``scopes`` is an optional list of OAuth scopes to request; leave empty
    to default to whatever the auth server offers.
    """

    type: Literal["oauth2"]
    issuer: str | None = None
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    registration_endpoint: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] = Field(default_factory=list)
    audience: str | None = None
    redirect_uri: str | None = None
    dynamic_registration: bool = False

    @model_validator(mode="after")
    def _check_endpoints(self) -> "OAuth2AuthConfig":
        has_issuer = bool(self.issuer)
        has_manual = bool(self.authorization_endpoint and self.token_endpoint)
        if not has_issuer and not has_manual:
            raise ValueError(
                "oauth2 auth requires either 'issuer' (for RFC 8414 discovery) "
                "or both 'authorization_endpoint' and 'token_endpoint'"
            )
        if not self.client_id and not self.dynamic_registration:
            raise ValueError(
                "oauth2 auth requires 'client_id' unless "
                "'dynamic_registration' is enabled"
            )
        return self


HttpAuthConfig = (
    NoAuthConfig
    | BearerAuthConfig
    | ApiKeyAuthConfig
    | BasicAuthConfig
    | OAuth2AuthConfig
)


class HttpUpstreamConfig(BaseModel):
    """HTTP upstream configuration."""

    type: Literal["http"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    auth: HttpAuthConfig | None = Field(default=None, discriminator="type")
    timeout_s: float = 30.0


UpstreamConfig = StdioUpstreamConfig | HttpUpstreamConfig | dict[str, Any]


class AppConfig(BaseModel):
    """Top-level application configuration."""

    default_upstream: str | None = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    upstreams: dict[str, UpstreamConfig] = Field(default_factory=dict)
    policies: PoliciesConfig = Field(default_factory=PoliciesConfig)

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


def _expand_secrets(value: Any, resolver: SecretResolver) -> Any:
    """Replace every ``${secret:NAME}`` placeholder with ``resolver(NAME)``.

    Walks the same nested (dict | list | str) tree as :func:`_expand_env`.
    A resolver miss produces an empty string so downstream pydantic
    validation can still run; the runtime layer is responsible for
    surfacing missing-secret errors at apply time, where it has enough
    context to blame a specific upstream.
    """
    if isinstance(value, dict):
        return {k: _expand_secrets(v, resolver) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_secrets(v, resolver) for v in value]
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            resolved = resolver(match.group(1))
            return "" if resolved is None else resolved
        return SECRET_RE.sub(repl, value)
    return value


def _apply_expansions(
    value: Any,
    *,
    secrets: SecretResolver | None,
) -> Any:
    expanded = _expand_env(value)
    if secrets is not None:
        expanded = _expand_secrets(expanded, secrets)
    return expanded


def find_secret_references(payload: Any) -> list[str]:
    """Return every ``${secret:NAME}`` placeholder referenced in ``payload``.

    Used by the runtime config applier to validate up front that referenced
    secrets actually exist before swapping in a new config, so operators
    get a clean error message instead of an empty-string silent failure in
    an upstream env var.
    """
    out: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, str):
            for match in SECRET_RE.finditer(v):
                out.add(match.group(1))

    walk(payload)
    return sorted(out)


def load_config(
    path: str | Path,
    *,
    secrets: SecretResolver | None = None,
) -> AppConfig:
    """Load and validate config from JSON file.

    If ``secrets`` is provided it is invoked to expand any ``${secret:NAME}``
    placeholders alongside the pre-existing ``${env:FOO}`` expansion. CLI
    callers leave it unset (and get the historical behaviour); the server
    runtime supplies a resolver backed by :class:`~mcp_proxy.secrets.SecretsManager`.
    """
    data = json.loads(Path(path).read_text())
    expanded = _apply_expansions(data, secrets=secrets)
    return AppConfig.model_validate(expanded)


def validate_config_payload(
    payload: dict[str, Any],
    *,
    secrets: SecretResolver | None = None,
) -> tuple[bool, str | None]:
    """Validate an in-memory config payload."""
    try:
        AppConfig.model_validate(
            _apply_expansions(deepcopy(payload), secrets=secrets)
        )
    except ValidationError as exc:
        return False, str(exc)
    except ValueError as exc:
        return False, str(exc)
    return True, None


_SECRET_KEY_HINTS = ("key", "token", "auth", "secret", "password", "credential")


def _looks_secret_shaped(key: str) -> bool:
    k = key.lower()
    return any(hint in k for hint in _SECRET_KEY_HINTS)


def redact_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact secret-like values from config payload.

    Covers:
      - ``auth.token``      the literal admin bearer (set by the
                            onboarding wizard and stored directly in the
                            config row).
      - ``auth.token_env`` (the env var *name* that holds the proxy bearer).
      - ``telemetry.headers[*]`` for header names that look secret-shaped.
      - ``upstreams[*].env[*]`` for stdio upstreams — any env key that looks
        secret-shaped gets its value replaced with a marker. This stops the
        Config page from leaking ``GITHUB_TOKEN`` etc. back to the dashboard.
      - ``upstreams[*].headers[*]`` for http upstreams — same treatment.
    """
    redacted = deepcopy(payload)
    headers = redacted.get("telemetry", {}).get("headers", {})
    for key in list(headers.keys()):
        if _looks_secret_shaped(key):
            headers[key] = "***REDACTED***"
    auth_block = redacted.get("auth") or {}
    if isinstance(auth_block, dict):
        if auth_block.get("token"):
            auth_block["token"] = "***REDACTED***"
        if auth_block.get("token_env"):
            auth_block["token_env"] = "***REDACTED_ENV***"
    upstreams = redacted.get("upstreams") or {}
    if isinstance(upstreams, dict):
        for _name, settings in upstreams.items():
            if not isinstance(settings, dict):
                continue
            env = settings.get("env")
            if isinstance(env, dict):
                for k in list(env.keys()):
                    if _looks_secret_shaped(k):
                        env[k] = "***REDACTED***"
            headers = settings.get("headers")
            if isinstance(headers, dict):
                for k in list(headers.keys()):
                    if _looks_secret_shaped(k):
                        headers[k] = "***REDACTED***"
    return redacted
