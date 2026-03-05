# MCPy Proxy

A production-ready **multi-upstream MCP proxy** that exposes a single endpoint and routes JSON-RPC 2.0 MCP traffic to many upstream MCP servers.

## Project Overview

MCPy Proxy multiplexes requests to heterogeneous upstream MCP servers (stdio and HTTP built-in), includes a privileged internal admin MCP interface, and ships with an asynchronous telemetry pipeline.

## What MCP Is

Model Context Protocol (MCP) is a protocol for tool/server interoperability. In this project, messages are handled as **JSON-RPC 2.0 over UTF-8 JSON**.

### Request/Response Streaming Semantics

- `/mcp` accepts either `application/json` (single or batch payload) or `application/x-ndjson` (one JSON-RPC message per line).
- Incoming request bodies are parsed incrementally; each message is processed in arrival order.
- Responses are emitted as NDJSON chunks as soon as each request completes (no buffering until the full batch finishes).
- JSON-RPC correlation is preserved by emitting each upstream/admin response with the original request `id`.
- Ordering is explicit and stable: messages are forwarded sequentially and responses are returned in the same sequence they are processed.
- Notification-only requests (no `id` values) produce no response body and return HTTP `202 Accepted`.

## Why This Proxy Exists

- Consolidate many MCP servers behind one endpoint.
- Enable policy-driven routing.
- Centralize health, authentication, and telemetry.
- Provide runtime config management without process restarts.

## Architecture Overview

- **FastAPI server** handling `/mcp`, `/mcp/{name}`, `/health`.
- **Routing engine** with precedence: path > header > in-band > default.
- **Upstream manager** for plugin-based transport instances.
- **Admin MCP handler** mounted as `/mcp/__admin__` by default.
- **Telemetry pipeline** with bounded queue + sink plugins.
- **Plugin registry** loading built-ins and Python entry points.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp config.example.json config.json
mcp-proxy serve --config config.json --listen 127.0.0.1:8000
```

## Configuration Examples

```json
{
  "default_upstream": "git",
  "auth": {"token_env": "MCP_PROXY_TOKEN"},
  "admin": {
    "mount_name": "__admin__",
    "enabled": true,
    "require_token": true,
    "allowed_clients": ["127.0.0.1"]
  },
  "telemetry": {
    "enabled": true,
    "sink": "http",
    "endpoint": "https://telemetry.example.com/ingest",
    "headers": {"X-Api-Key": "${env:TELEM_KEY}"},
    "batch_size": 50,
    "flush_interval_ms": 2000,
    "queue_max": 1000,
    "drop_policy": "drop_newest"
  },
  "upstreams": {
    "git": {"type": "stdio", "command": "python", "args": ["-m", "my_git_mcp_server"]},
    "search": {"type": "http", "url": "https://example.com/mcp"}
  }
}
```

## Admin MCP Interface

Mounted under `/mcp/{admin.mount_name}` (default `/mcp/__admin__`).

Methods:
- `admin.get_config`
- `admin.validate_config`
- `admin.apply_config` (`dry_run` and rollback on failure)
- `admin.list_upstreams`
- `admin.restart_upstream`
- `admin.set_log_level`
- `admin.send_telemetry`
- `admin.get_health`

Admin requests are never forwarded to external upstreams.


## Admin Web UI

A lightweight admin UI is available at `/admin` and is auto-generated from the same admin MCP method surface (via internal `/admin/api/*` helper endpoints that proxy to admin MCP methods).

- Dashboard: upstream status, request/error metrics, telemetry status
- Upstream management: list + restart + health checks
- Config editor: view/edit JSON, validate, diff preview (`dry_run`), apply
- Telemetry panel: queue health + test event emission
- Logs viewer: recent structured logs with severity/upstream filters

### Admin UI Architecture (diagram)

```text
Browser (/admin)
   -> JS calls /admin/api/*
   -> FastAPI admin helper endpoints
   -> AdminService (MCP methods)
   -> RuntimeConfigManager / UpstreamManager / TelemetryPipeline
```

### Access and Security

- `/admin` and `/admin/api/*` enforce the same token and `allowed_clients` rules as `/mcp/__admin__`.
- Secrets are redacted from returned config payloads.
- Public read-only status is exposed at `/status` (no authentication).

## Hot Reload

Runtime config updates are supported without process restart through:

1. `admin.apply_config`
2. Config file watcher when `--config` is used

Both paths use the same validation + diff + apply pipeline with rollback-on-failure semantics.

## Telemetry

- Non-blocking enqueue from request path.
- Bounded queue with overload drop behavior.
- Batch flush on size or interval.
- Sink plugins: `http`, `noop`.
- Retry with exponential backoff + jitter for HTTP sink.

## Plugin System

Plugin entry point groups:
- `mcp_proxy.upstreams`
- `mcp_proxy.telemetry_sinks`

Built-ins are registered by default and can be overridden by external plugins installed with pip.

## Security Notes

- Default bind host: `127.0.0.1`.
- Optional bearer auth via `auth.token_env`.
- Admin supports token requirement + client IP allowlist, and fails closed when `admin.require_token=true` but the expected token is not configured.
- Secret values are redacted in admin responses.
- Authorization headers are never logged.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Licensed under MIT. See [LICENSE](LICENSE).
