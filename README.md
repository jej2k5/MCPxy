# MCPy Proxy

A production-ready **multi-upstream MCP proxy** with a modern live
dashboard, a policy engine, a bundled MCP server catalog, and one-line
installers for Claude Desktop, Claude Code, and ChatGPT.

![MCPy Overview dashboard](docs/screenshots/dashboard-overview.png)

## Highlights

- **Multi-upstream MCP proxy** — multiplex JSON-RPC 2.0 MCP traffic to
  many upstream MCP servers (stdio subprocesses or HTTP endpoints) behind
  a single URL, with precedence-based routing (path > header > in-band >
  default).
- **Modern live dashboard** at `/admin` — Overview, Routes, Live Traffic,
  Policies, Browse, Import, Connect, Logs, and Config pages. React +
  Vite + Tailwind, shipped pre-built so `pip install` gives you a
  working UI.
- **Live traffic observability** — every forwarded request is recorded
  (metadata only — bodies are never stored) and streamed to the
  dashboard over Server-Sent Events. Per-upstream p50/p95/p99 latency,
  error rate, and a rolling 5-minute traffic chart.
- **Policy engine** — per-upstream method ACLs with wildcards, token-
  bucket rate limits (scoped per upstream, per client IP, or both), and
  max request-size caps. Edited live from the Policies page and hot-
  reloaded through the same atomic apply + rollback pipeline as the
  config file.
- **Bundled MCP catalog** — a curated JSON catalog of well-known MCP
  servers (filesystem, git, github, postgres, sqlite, brave-search,
  fetch, puppeteer, slack, memory, and more). Browse, search, and
  install with one click from the Browse page; or
  `mcp-proxy catalog install <id> --var key=value`.
- **Import from existing clients** — the Import page scans your local
  Claude Desktop, Claude Code, Cursor, Windsurf, and Continue config
  files, surfaces the MCP servers already configured there, and lets
  you bring them into MCPy with one click. Also available as
  `mcp-proxy discover` and `mcp-proxy import --client <id>`.
- **Runtime registration API** — `POST /admin/api/upstreams` /
  `DELETE /admin/api/upstreams/{name}` plus a file-drop directory at
  `~/.mcpy/upstreams.d/` (drop a JSON file and the proxy picks it up
  on the next poll; delete it to remove the upstream). Also available
  as `mcp-proxy register --name foo --stdio "cmd args"` /
  `mcp-proxy unregister --name foo`.
- **One-line install into MCP clients** — `mcp-proxy install --client
  claude-desktop | claude-code | chatgpt` backs up your existing client
  config and registers MCPy as a tool. A bundled **stdio adapter**
  (`mcp-proxy stdio --connect URL`) lets stdio-only clients like Claude
  Desktop talk to the HTTP proxy without a separate shim.
- **Hot reload** of the full config (upstreams, telemetry, policies)
  with atomic apply and rollback on failure.

## Project Overview

MCPy Proxy multiplexes requests to heterogeneous upstream MCP servers
(stdio and HTTP built-in), includes a privileged internal admin MCP
interface, and ships with an asynchronous telemetry pipeline, a v1
policy engine, and a live React dashboard.

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

- **FastAPI server** handling `/mcp`, `/mcp/{name}`, `/health`, `/status`,
  and the admin surface under `/admin/*`.
- **Routing engine** with precedence: path > header > in-band > default.
- **Upstream manager** for plugin-based transport instances (stdio +
  HTTP built in; additional transports discoverable via Python entry
  points).
- **Admin MCP handler** mounted as `/mcp/__admin__` by default.
- **Traffic recorder** instrumented at the single forwarding chokepoint
  (`ProxyBridge.forward`). Metadata-only ring buffer (2000 entries) plus
  per-subscriber fan-out for live SSE streaming.
- **Policy engine** (`src/mcp_proxy/policy/engine.py`) — size → method
  ACL → rate limit, first-match-deny-wins, buckets preserved across hot
  reloads when configuration is unchanged.
- **Telemetry pipeline** with bounded queue + sink plugins.
- **Plugin registry** loading built-ins and Python entry points.
- **React + Vite dashboard** under `frontend/`, built to
  `src/mcp_proxy/web/dist/` and served from `/admin`.


## Repository Layout

- Design notes: `docs/Design.md`
- Screenshots: `docs/screenshots/`
- Frontend source: `frontend/` (React + Vite + Tailwind)
- Built dashboard: `src/mcp_proxy/web/dist/`
- Plugin registry: `src/mcp_proxy/plugins/registry.py`
- Traffic + route discovery: `src/mcp_proxy/observability/`
- Policy engine: `src/mcp_proxy/policy/engine.py`
- Install helpers: `src/mcp_proxy/install/clients.py`
- Stdio adapter: `src/mcp_proxy/stdio_adapter.py`
- MCP catalog + importers + runtime registration: `src/mcp_proxy/discovery/`
- Bundled catalog JSON: `src/mcp_proxy/data/mcp_catalog.json`
- Behavior tests: `tests/test_routing_precedence.py`,
  `tests/test_admin_auth.py`, `tests/test_atomic_apply_rollback.py`,
  `tests/test_redaction.py`, `tests/test_plugin_discovery.py`,
  `tests/test_telemetry_queue_flush.py`, `tests/test_stdio_restart.py`,
  `tests/test_overload_handling.py`, `tests/test_hot_reload.py`,
  `tests/test_admin_ui_auth.py`, `tests/test_admin_ui_dist.py`,
  `tests/test_traffic_recorder.py`, `tests/test_traffic_endpoints.py`,
  `tests/test_bridge_instrumentation.py`, `tests/test_route_discovery.py`,
  `tests/test_policy_*.py`, `tests/test_install_*.py`,
  `tests/test_stdio_adapter.py`, `tests/test_cli_install.py`.

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]

# Generate a starter config
mcp-proxy init --output config.json

# Run the proxy + dashboard
export MCP_PROXY_TOKEN=$(openssl rand -hex 16)
mcp-proxy serve --config config.json
```

The dashboard is now available at <http://127.0.0.1:8000/admin>. The
SPA shell is public so the in-page login form can render; paste the
bearer token above to sign in. Every `/admin/api/*` endpoint remains
token-gated.

## Running with Docker Compose

The proxy (FastAPI backend + React dashboard) also ships as a single
container image. Bring up the full stack with:

```bash
# optional: generate a token and persist it in a .env file
echo "MCP_PROXY_TOKEN=$(openssl rand -hex 16)" > .env

docker compose up -d --build
docker compose logs -f mcpy
```

The container listens on `0.0.0.0:8000` and Compose publishes it on
`http://127.0.0.1:8000` by default (override with `MCPY_HOST_PORT`).
The dashboard is reachable at <http://127.0.0.1:8000/admin>.

**What's mounted:**

- `./deploy/docker/config.json` → `/etc/mcpy/config.json` (read-only).
  Edit on the host; the runtime config watcher hot-reloads most
  changes, otherwise `docker compose restart mcpy`.
- Named volume `mcpy_data` → `/var/lib/mcpy` for runtime state (the
  file-drop watcher directory, telemetry buffers, etc.).

**Stdio upstreams work inside the container.** The image bundles Node
(for `npx`-based catalog entries like `filesystem`, `github`,
`puppeteer`) and `uv`/`uvx` (for Python-based entries like
`mcp-server-git`), so everything in `mcp-proxy catalog list` is
installable from the dashboard's Browse page with no host dependencies.

**Desktop install helpers run on the host, not in the container.** The
`mcp-proxy install --client claude-desktop ...` command writes to your
host's client config files (Claude Desktop, Cursor, Continue, ...) and
must execute on the host. After `docker compose up`, point those
installers at the container with:

```bash
pip install mcpy-proxy     # host-side, just for the install CLI
mcp-proxy install --client claude-desktop \
  --url http://127.0.0.1:8000 --token-env MCP_PROXY_TOKEN
```

**Running one-shot CLI commands in the container.** Anything other than
`serve` is forwarded straight through the entrypoint, so operator
workflows like catalog browsing or runtime registration work via
`docker compose run`:

```bash
docker compose run --rm mcpy catalog list
docker compose run --rm mcpy register --name gh \
  --stdio "npx -y @modelcontextprotocol/server-github" \
  --url http://mcpy:8000
```

## Dashboard tour

| | |
| --- | --- |
| **Overview** — uptime, total requests, error rate, p95 latency, live traffic chart, per-upstream latency table. | ![Overview](docs/screenshots/dashboard-overview.png) |
| **Routes** — one card per upstream with health, transport, discovered tools (via periodic `tools/list` probe), and a restart button. | ![Routes](docs/screenshots/dashboard-routes.png) |
| **Live Traffic** — Server-Sent-Events stream of every forwarded request (metadata only). Filter by upstream, method, or status; pause and clear. | ![Traffic](docs/screenshots/dashboard-traffic.png) |
| **Policies** — method allow/deny lists (with wildcards), token-bucket rate limits, and size caps. Global and per-upstream. Hot-reloaded through the same atomic apply pipeline as the config file. | ![Policies](docs/screenshots/dashboard-policies.png) |
| **Browse** — bundled catalog of well-known MCP servers with search, categories, and one-click install. Prompts for any required variables (paths, API keys). | ![Browse](docs/screenshots/dashboard-browse.png) |
| **Import** — scans installed clients (Claude Desktop, Claude Code, Cursor, Windsurf, Continue) for MCP servers you already have and lets you bring them in with one click. | ![Import](docs/screenshots/dashboard-import.png) |
| **Connect** — one-click snippets and install commands for Claude Desktop, Claude Code, and ChatGPT. | ![Connect](docs/screenshots/dashboard-connect.png) |

## Connecting Claude / ChatGPT

```bash
# One-line install for Claude Desktop (uses the bundled stdio adapter)
mcp-proxy install --client claude-desktop --url http://127.0.0.1:8000 \
  --token-env MCP_PROXY_TOKEN

# Claude Code (HTTP transport)
mcp-proxy install --client claude-code --url http://127.0.0.1:8000 \
  --token-env MCP_PROXY_TOKEN

# ChatGPT — prints a snippet to paste into the connector UI
mcp-proxy install --client chatgpt --url http://127.0.0.1:8000
```

The dashboard's **Connect** page (`/admin/connect`) shows the same
snippets and the exact `mcp-proxy install` command for each client.

## Building the dashboard

The dashboard is built from `frontend/` (React + Vite + Tailwind) and the
compiled assets are committed under `src/mcp_proxy/web/dist/` so
`pip install` ships a working UI. To rebuild after editing the frontend:

```bash
cd frontend
npm install
npm run build
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
- `admin.get_logs`
- `admin.get_policies`
- `admin.update_policies`

Admin requests are never forwarded to external upstreams.


## Admin Web UI

The dashboard at `/admin` is a React SPA (source: `frontend/`, built to
`src/mcp_proxy/web/dist/`) that talks to the same admin MCP surface via
internal `/admin/api/*` helper endpoints.

Pages: **Overview**, **Routes**, **Traffic**, **Policies**, **Connect**,
**Logs**, **Config**.

### Admin UI Architecture

```text
Browser (/admin — public SPA shell, in-page LoginGate)
   -> fetch /admin/api/* with Bearer token
       -> FastAPI admin helper endpoints
          -> AdminService / TrafficRecorder / PolicyEngine
             -> RuntimeConfigManager / UpstreamManager / TelemetryPipeline
   -> EventSource-style streaming fetch /admin/api/traffic/stream
      for live-traffic SSE
```

### Access and Security

- The SPA **shell** at `/admin` is public so the in-page login gate can
  render in a browser. Every `/admin/api/*` endpoint enforces the same
  bearer-token and `admin.allowed_clients` rules as `/mcp/__admin__`.
- Request bodies are **never** captured by the traffic recorder —
  only method name, upstream, status, latency, byte counts, and client
  IP.
- Secrets are redacted from returned config payloads.
- Public read-only status is exposed at `/status` (no authentication).

## Discovering and registering MCP servers

MCPy ships three independent ways to bring MCP servers into the proxy,
all of which route through the same atomic apply + rollback pipeline as
the rest of the config.

### 1. Browse the bundled catalog

```bash
mcp-proxy catalog list                 # pretty-printed list
mcp-proxy catalog list -q git          # search
mcp-proxy catalog install filesystem \
  --var allowed_path=/home/me/projects \
  --name my-fs                         # materialise + register via HTTP

# or from the dashboard: Browse page → Install
```

The catalog lives at `src/mcp_proxy/data/mcp_catalog.json` and ships
with ~15 well-known MCP servers. Entries declare a transport (stdio or
http), a command line with `${variable}` placeholders, and a list of
required/optional variables with descriptions and `secret` flags. The
dashboard renders these as a form; missing required variables are
rejected before anything is applied.

### 2. Import from existing clients

```bash
mcp-proxy discover                     # local scan, pretty output
mcp-proxy discover --json              # machine-readable

# Import one, several, or all upstreams from another client:
mcp-proxy import --client claude-desktop
mcp-proxy import --client cursor --name postgres-prod
```

Importers read well-known config paths for Claude Desktop, Claude Code,
Cursor, Windsurf, and Continue (per-OS paths are baked in) and map
each `mcpServers` entry to an MCPy upstream. The Import page in the
dashboard shows a checklist with paths and transport details.

### 3. Runtime registration API

```bash
# Add an upstream on a running proxy:
mcp-proxy register --name git --stdio "uvx mcp-server-git --repository /repo"
mcp-proxy register --name api --http http://internal.example.com/mcp
mcp-proxy unregister --name git

# Or POST directly:
curl -X POST http://127.0.0.1:8000/admin/api/upstreams \
  -H "Authorization: Bearer $MCP_PROXY_TOKEN" \
  -d '{"name":"git","config":{"type":"stdio","command":"uvx","args":["mcp-server-git"]}}'
```

**File drop.** Drop a JSON file into `~/.mcpy/upstreams.d/` and the
running proxy picks it up on the next poll (default interval: 1s).
Deleting the file removes the upstream. Useful for scripting,
provisioning, and CI workflows.

```bash
mkdir -p ~/.mcpy/upstreams.d
cat > ~/.mcpy/upstreams.d/memory.json <<'EOF'
{"type": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]}
EOF
```

The file-drop watcher can be disabled by setting
`registration.file_drop_enabled = false` in the main config.

## Policies

MCPy ships a v1 policy engine that evaluates every forwarded request:

1. **Size cap** — reject requests above `max_request_bytes`.
2. **Method ACL** — per-upstream `allow` / `deny` lists with `fnmatch`
   wildcards (e.g. `tools/*`). Global policies apply first; per-upstream
   policies override the global block for that upstream.
3. **Rate limit** — token bucket with configurable `requests_per_second`
   and `burst`, scoped to the upstream, the client IP, or both. Buckets
   survive hot reloads when the rate config is unchanged, and idle
   client-IP buckets are evicted to bound cardinality.

Denied requests surface as JSON-RPC error `-32003 policy_blocked:<reason>`
and appear in the Live Traffic page with `status=denied`.

```json
{
  "policies": {
    "global": {
      "size": {"max_request_bytes": 1048576}
    },
    "per_upstream": {
      "git": {
        "methods": {"deny": ["tools/dangerous_*"]},
        "rate_limit": {"requests_per_second": 10, "burst": 20, "scope": "upstream"}
      }
    }
  }
}
```

## Hot Reload

Runtime config updates are supported without process restart through:

1. `admin.apply_config`
2. Config file watcher when `--config` is used
3. The dashboard's Config and Policies pages

All paths use the same validation + diff + apply pipeline with rollback-on-failure semantics.

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
