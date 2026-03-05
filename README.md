# MCPy Proxy

A production-ready **multi-upstream MCP proxy** that exposes a single endpoint and routes JSON-RPC 2.0 MCP traffic to many upstream MCP servers.

## Project Overview

MCPy Proxy multiplexes requests to heterogeneous upstream MCP servers (stdio and HTTP built-in), includes a privileged internal admin MCP interface, and ships with an asynchronous telemetry pipeline.

## What MCP Is

Model Context Protocol (MCP) is a protocol for tool/server interoperability. In this project, messages are handled as **JSON-RPC 2.0 over UTF-8 JSON**.

## Why This Proxy Exists

- Consolidate many MCP servers behind one endpoint.
- Enable policy-driven routing.
- Centralize health, authentication, and telemetry.
- Provide runtime config management without process restarts.

## Architecture Overview

- **FastAPI server** handling `/mcp`, `/mcp/{name}`, `/health`.
- **Routing engine** with precedence: path > header > in-band > default.
- **Upstream manager** for plugin-based transport instances.
- **Admin MCP handler** mounted as `/mcp/admin` by default.
- **Telemetry pipeline** with bounded queue + sink plugins.
- **Plugin registry** loading built-ins and Python entry points.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
cp config.example.json config.json
mcp-proxy --config config.json --host 127.0.0.1 --port 8080
```

## Configuration Examples

```json
{
  "default_upstream": "git",
  "auth": {"token_env": "MCP_PROXY_TOKEN"},
  "admin": {
    "mount_name": "admin",
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
    "queue_size": 1000
  },
  "upstreams": {
    "git": {"type": "stdio", "command": "python", "args": ["-m", "my_git_mcp_server"]},
    "search": {"type": "http", "url": "https://example.com/mcp"}
  }
}
```

## Admin MCP Interface

Mounted under `/mcp/{admin.mount_name}` (default `/mcp/admin`).

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
- Admin supports token requirement + client IP allowlist.
- Secret values are redacted in admin responses.
- Authorization headers are never logged.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Licensed under MIT. See [LICENSE](LICENSE).
