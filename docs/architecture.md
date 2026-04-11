# MCPxy Architecture

This document describes MCPxy's internal design for contributors and plugin
authors. For deployment and configuration, see
[`admin-guide.md`](admin-guide.md) and [`configuration.md`](configuration.md).

---

## Goals and non-goals

**Goals:**
- Multiplex JSON-RPC 2.0 MCP traffic from many clients to many upstream servers
  behind a single URL
- Provide a live admin dashboard for managing upstreams, policies, and users
- Apply security policies (ACLs, rate limits, redaction) without modifying
  upstream or client code
- Hot-reload configuration atomically with rollback on failure
- Support enterprise auth (OAuth, SAML, OIDC) for both the admin UI and for
  upstream connections

**Non-goals:**
- MCPxy does not interpret or cache MCP tool results — it is a transparent proxy
- MCPxy does not manage the lifecycle of external services (databases, APIs, etc.)
  used by upstream MCP servers
- MCPxy is not a general-purpose reverse proxy (no URL rewriting, load balancing,
  or traffic splitting beyond upstream selection)

---

## High-level diagram

```
  MCP Clients                    MCPxy                     Upstream MCP Servers
  ─────────────      ┌───────────────────────────┐      ──────────────────────────
  Claude Desktop ──► │                           │ ───► github (stdio / npx)
  Claude Code    ──► │  ┌─────────────────────┐  │ ───► filesystem (stdio / uvx)
  Cursor         ──► │  │   Policy Engine      │  │ ───► internal API (HTTP+OAuth)
  ChatGPT        ──► │  │   (ACL/rate/redact)  │  │ ───► sqlite (stdio)
  custom client  ──► │  └─────────────────────┘  │      ──────────────────────────
                     │                           │
  Admin Browser  ──► │  React dashboard (/admin) │
                     │  Admin REST (/admin/api/) │
                     └───────────────────────────┘
                           │            │
                        SQLite      Secrets
                      /Postgres     (Fernet)
                      /MySQL
```

---

## Module map

All source lives under `src/mcpxy_proxy/`.

| Module / package | Role |
|---|---|
| `server.py` | `create_app()` FastAPI factory; mounts all routes including `/mcp`, `/mcp/{name}`, `/admin`, `/admin/api/*`, `/health`, `/status`, onboarding, authy endpoints |
| `cli.py` | Entry point (`mcpxy-proxy`); subcommands: `serve`, `init`, `install`, `register`, `unregister`, `discover`, `import`, `catalog`, `config`, `secrets`, `stdio` |
| `config.py` | Pydantic models for every config block: `AppConfig`, `UpstreamConfig`, `AuthyConfig`, `TlsConfig`, `RedactionPolicy`, `TokenTransformConfig`, `PoliciesConfig`, etc.; `${env:}` / `${secret:}` placeholder expansion |
| `runtime.py` | `RuntimeConfigManager` — hot-reload orchestrator; validates, applies, and if validation fails rolls back; wraps `UpstreamManager`, `PolicyEngine`, and telemetry |
| `routing.py` | `resolve_upstream()` — deterministic upstream selection (path → header → in-band → default) |
| `jsonrpc.py` | JSON-RPC 2.0 codec, error helpers, NDJSON streaming |
| `secrets.py` | Fernet-backed encrypted store; `SecretsManager` reads/writes `secrets` table; key lives in `MCPXY_SECRETS_KEY` env var |
| `tls.py` | Auto-generate self-signed cert for loopback; load user cert/key; TLS config helpers for uvicorn |
| `proxy/bridge.py` | `ProxyBridge.forward()` — core request/response loop: resolve upstream, apply request redaction, send, apply response redaction, record telemetry |
| `proxy/manager.py` | `UpstreamManager` — lifecycle (start/stop/restart) for all configured upstreams |
| `proxy/stdio.py` | `StdioUpstreamTransport` — manages a subprocess, JSON-RPC over stdin/stdout, restart on exit |
| `proxy/http.py` | `HttpUpstreamTransport` — httpx-based HTTP/SSE transport; handles token transformation, outbound TLS, upstream OAuth token injection |
| `proxy/admin.py` | Admin MCP service; implements internal MCP methods at `/mcp/admin` |
| `auth/oauth.py` | OAuth 2.1 upstream client: discovery (RFC 8414), DCR (RFC 7591), auth code + PKCE (RFC 7636), token refresh; tokens stored encrypted |
| `authn/manager.py` | `AuthnManager` — wraps the `authy` library; exposes `start_federated_login()`, `exchange_code_for_token()`, `verify_token()` |
| `authn/middleware.py` | Per-request credential extraction (PAT → session cookie → JWT bearer); injects `principal` into request state |
| `authn/users.py` | User record creation/sync on federated callback; invite handling |
| `policy/engine.py` | `PolicyEngine` — evaluates method ACLs (fnmatch), enforces token-bucket rate limits, checks size caps; exposes `check()`, `redact_request()`, `redact_response()` |
| `policy/redaction.py` | `build_redactor()` — compiles PII/PCI + custom regex patterns into a stateless in-place dict walker |
| `discovery/catalog.py` | Loads `data/mcp_catalog.json`; `Catalog.materialize()` substitutes user variables |
| `discovery/importers.py` | Scans Claude Desktop, Claude Code, Cursor, Windsurf, Continue config files for existing MCP servers |
| `discovery/registration.py` | File-drop watcher: polls `upstreams.d/` for JSON files, applies additions/removals via the hot-reload pipeline |
| `install/` | Client installer helpers: writes/patches Claude Desktop, Claude Code, ChatGPT config files |
| `storage/db.py` | `resolve_database_url()` — priority: explicit arg → `MCPXY_DB_URL` → `bootstrap.json` → sqlite default |
| `storage/schema.py` | SQLAlchemy Core table definitions (schema v2): `config_kv`, `config_history`, `upstreams`, `secrets`, `onboarding`, `users`, `invites`, `token_mappings` |
| `storage/config_store.py` | `ConfigStore` — atomic config read/write, version counter, history append |
| `storage/bootstrap.py` | Reads/writes `<state_dir>/bootstrap.json` for DB URL persistence across container restarts |
| `telemetry/pipeline.py` | Bounded async queue, batching, retry; non-blocking — drops on overflow per `drop_policy` |
| `telemetry/http_sink.py` | HTTP telemetry sink plugin (entry point: `http`) |
| `telemetry/noop_sink.py` | No-op telemetry sink plugin (entry point: `noop`) |
| `observability/traffic.py` | Per-request metadata recorder; streams to dashboard via SSE |
| `plugins/registry.py` | Entry-point-based plugin registry for upstream transports and telemetry sinks |
| `logging.py` | Logging setup utilities |
| `data/mcp_catalog.json` | Bundled catalog of 14 well-known MCP servers |
| `web/dist/` | Pre-built React/Vite dashboard (committed build output; not source) |

---

## Routing precedence

Upstream selection for an incoming JSON-RPC request is strict and deterministic:

1. **URL path** — `/mcp/{name}` selects the upstream named `name`
2. **HTTP header** — `X-MCP-Upstream: name`
3. **In-band parameter** — `params.mcp_upstream` in the JSON-RPC request body
4. **Default** — `config.default_upstream`

If none of the above resolves to a known upstream, the request is rejected with
a JSON-RPC error.

---

## Data plane — request flow

```
Client POST /mcp/{name}
  │
  ▼
server.py: mcp_handler()
  │  authenticate (authn/middleware.py: extract_principal)
  │
  ▼
routing.py: resolve_upstream()        ← path / header / param / default
  │
  ▼
policy/engine.py: PolicyEngine.check()
  │  method ACL (fnmatch allow/deny)
  │  token-bucket rate limit
  │  request size cap
  │
  ▼
policy/engine.py: redact_request()    ← PII/PCI + custom patterns (if enabled)
  │
  ▼
proxy/bridge.py: ProxyBridge.forward()
  │
  ├─► StdioUpstreamTransport.request()   (stdio subprocess)
  │     or
  └─► HttpUpstreamTransport.request()    (HTTP; injects token-transformed auth)
        │
        ▼
     Upstream MCP Server
        │
        ▼
proxy/bridge.py (response received)
  │
  ▼
policy/engine.py: redact_response()   ← response redaction (if enabled)
  │
  ▼
observability/traffic.py: record()   ← metadata only; never bodies
  │
  ▼
Client receives JSON-RPC response
```

**Backpressure:** If the upstream queue is full, `ProxyBridge` returns a
structured JSON-RPC error (`-32000 server overloaded`) rather than blocking.

**Stdio restart:** `StdioUpstreamTransport` restarts the subprocess on exit and
retries the in-flight request up to the configured retry limit.

---

## Control plane

| Path | Protocol | Purpose |
|---|---|---|
| `/mcp/admin` | MCP (JSON-RPC) | Internal admin MCP methods: `config/get`, `config/apply`, `config/history`, `health/status`, `upstreams/list`, `upstreams/restart`, `telemetry/flush` |
| `/admin` | HTTP (SPA) | React dashboard (serves `web/dist/index.html`) |
| `/admin/api/*` | HTTP REST | Dashboard helper APIs: config CRUD, upstreams, users, PATs, token mappings, catalog, onboarding wizard, authy auth flow |
| `/admin/api/sse` | HTTP SSE | Live traffic stream to the Traffic page |
| `/health` | HTTP GET | Liveness probe — returns `{"status": "ok"}` |
| `/status` | HTTP GET | Detailed status including upstream health, config version, uptime |

Onboarding endpoints (`/admin/api/onboarding/*`) are IP-gated by
`MCPXY_ONBOARDING_ALLOWED_CLIENTS` (default: loopback only) and expire after
`MCPXY_ONBOARDING_TTL_S` seconds (default: 1800).

---

## Storage — schema v2

All tables are created by `storage/schema.py` using SQLAlchemy Core (no ORM).
Schema migrations are additive; `bootstrap.py` runs `CREATE TABLE IF NOT EXISTS`
on every start.

| Table | Contents |
|---|---|
| `config_kv` | Single active config row: JSON blob + monotonic version counter |
| `config_history` | Append-only audit log of every `config apply` (timestamp, version, diff) |
| `upstreams` | Denormalized view synced from `config_kv` at each apply |
| `secrets` | Fernet-encrypted key/value store: upstream credentials, OAuth tokens, PAT hashes, token mappings |
| `onboarding` | First-run wizard state: timestamps for each step, completion flag |
| `users` | User identities (local or federated), password hashes, role |
| `invites` | Single-use invite tokens and their expiry |
| `token_mappings` | Per-user per-upstream credential pairs (values encrypted via secrets table) |

The active database URL is resolved in priority order by `storage/db.py`:
1. `MCPXY_DB_URL` environment variable
2. `<state_dir>/bootstrap.json` (written by onboarding wizard)
3. `sqlite:///<state_dir>/mcpxy.db` (default)

---

## Runtime config apply — atomicity and rollback

1. Client submits new config JSON (dashboard or CLI)
2. `RuntimeConfigManager.apply(new_config)` (`runtime.py`):
   a. Validate with Pydantic — reject on schema errors
   b. Validate secrets/env placeholders — reject if referenced secrets missing
   c. Check for TLS changes — reject with "restart required" error
   d. Apply to in-memory `UpstreamManager` (start new upstreams, stop removed ones)
   e. Apply to in-memory `PolicyEngine` (compile new rules, preserve rate-limit state)
   f. Persist atomically to `config_kv` table and append to `config_history`
3. On **any** failure in steps d–f: roll back in-memory state to previous config;
   return structured error to caller; DB is not written

Hot-reload exclusions: the TLS listener SSL context is bound at startup and
cannot change without a process restart.

---

## Plugin extension points

MCPxy uses Python package entry points for runtime extensibility.

### Upstream transports

Entry point group: `mcpxy_proxy.upstreams`

Each transport is a class implementing `UpstreamTransport` (from
`proxy/base.py`). Built-in transports:

| Entry point name | Class |
|---|---|
| `stdio` | `mcpxy_proxy.proxy.stdio.StdioUpstreamTransport` |
| `http` | `mcpxy_proxy.proxy.http.HttpUpstreamTransport` |

To add a custom transport, register it in your package's `pyproject.toml`:

```toml
[project.entry-points."mcpxy_proxy.upstreams"]
my_transport = "mypackage.transport:MyTransport"
```

Then reference it in config with `"type": "my_transport"`.

### Telemetry sinks

Entry point group: `mcpxy_proxy.telemetry_sinks`

Each sink implements `TelemetrySink` (from `telemetry/pipeline.py`). Built-in sinks:

| Entry point name | Class |
|---|---|
| `http` | `mcpxy_proxy.telemetry.http_sink.HttpTelemetrySink` |
| `noop` | `mcpxy_proxy.telemetry.noop_sink.NoopTelemetrySink` |

Register a custom sink the same way, then reference it with `"sink": "my_sink"`
in the telemetry config block.

---

## Frontend

The admin dashboard is a React/TypeScript SPA built with Vite and Tailwind CSS.

| Path | Contents |
|---|---|
| `frontend/src/pages/` | 14 page components (Browse, Config, Connect, Graph, Import, Logs, Onboarding, Overview, Policies, Routes, TokenMappings, Tokens, Traffic, Users) |
| `frontend/src/components/` | Shared components including `LoginGate.tsx` (auth state) |
| `frontend/src/api/` | Typed fetch wrappers and SSE client |
| `frontend/vite.config.ts` | Build target: `../src/mcpxy_proxy/web/dist/` |

The build output under `src/mcpxy_proxy/web/dist/` is committed to the repo and
bundled into the Python package via the `web/dist/**/*` package-data glob in
`pyproject.toml`. `server.py` serves `web/dist/index.html` for all `/admin`
requests; a legacy fallback path for `web/templates/admin.html` exists only for
pre-Vite dev builds.

For frontend development workflow, see [`development.md`](development.md).

---

## Reliability rules

- **Non-blocking telemetry** — the telemetry pipeline uses a bounded async queue.
  Overflow is handled by the configured `drop_policy` (`drop_newest` or
  `drop_oldest`). The pipeline never blocks the request path.
- **Stdio restart** — `StdioUpstreamTransport` restarts the subprocess after exit
  and retries in-flight requests. The restart interval is bounded to prevent
  tight loops.
- **Structured overload errors** — when a request cannot be processed (upstream
  queue full, circuit open), MCPxy returns a JSON-RPC error object instead of an
  HTTP 5xx so clients can distinguish proxy errors from upstream errors.
- **Atomic config apply** — see the [atomicity section](#runtime-config-apply--atomicity-and-rollback)
  above.

---

## Test map

Behavior-specific tests are in `tests/`:

| Test file | Behavior covered |
|---|---|
| `test_routing_precedence.py` | Path / header / in-band / default selection |
| `test_admin_auth.py` | Admin request gating (bearer, PAT, session) |
| `test_atomic_apply_rollback.py` | Hot-reload atomicity and rollback on error |
| `test_redaction.py` | PII/PCI redaction patterns and custom regex |
| `test_plugin_discovery.py` | Entry-point transport and sink loading |
| `test_telemetry_queue_flush.py` | Bounded queue and drop policy |
| `test_stdio_restart.py` | Subprocess restart and in-flight retry |
| `test_overload_handling.py` | Backpressure structured error response |
| `test_hot_reload.py` | Config version bump, upstream lifecycle during reload |
| `test_admin_ui_auth.py` | Dashboard login gate and session cookie |
| `test_authn_pats.py` | PAT issuance and verification |
| `test_config_authy.py` | Authy config validation |
| `test_oauth_endpoints.py` | Federated OAuth callback flow |
| `test_http_auth_static.py` | Static bearer / API-key upstream auth |
| `test_server_streaming.py` | NDJSON streaming responses |
| `test_bridge_shutdown_sync.py` | Graceful shutdown backpressure |

Run the full suite with `pytest` from the repo root. See
[`development.md`](development.md) for targeted commands by area.
