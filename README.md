# MCPxy — one URL for every MCP server

**Stop configuring the same MCP servers five different ways.** MCPxy is a
multi-upstream MCP proxy that sits between your clients (Claude Desktop,
Claude Code, Cursor, Windsurf, Continue, ChatGPT) and every MCP server you
use. Run MCPxy once, point every client at it, and manage everything from a
single live dashboard.

![MCPxy dashboard](docs/screenshots/dashboard-overview.png)

## Why MCPxy

Every MCP client keeps its own private list of servers. Installing a new
server — or rotating a token, or debugging a misbehaving one — means
editing a JSON file in a different location for each client. MCPxy
consolidates that into one endpoint and one dashboard: install servers
from a bundled catalog with one click, see live traffic across every
client, enforce policies, rotate secrets, and hot-reload without
restarting anything.

## Features

- **Multi-upstream MCP proxy** — multiplexes JSON-RPC 2.0 MCP traffic to
  many upstreams (stdio subprocesses or HTTP endpoints) behind one URL,
  with precedence-based routing (path > header > in-band > default).
- **Live dashboard** at `/admin` with 10 pages: Onboarding (first-run
  wizard), Overview, Routes, Traffic, Policies, Browse, Import, Connect,
  Logs, Config. Ships pre-built, so `pip install` gives you a working UI.
- **Bundled catalog** of 14 well-known MCP servers — filesystem, git,
  github, gitlab, memory, postgres, sqlite, brave-search, fetch,
  puppeteer, slack, time, everart, sentry — installable with one click.
- **Import from existing clients** — scans Claude Desktop, Claude Code,
  Cursor, Windsurf, and Continue for servers you already have and
  imports them in one click.
- **One-line installers** for Claude Desktop, Claude Code, and ChatGPT.
  A bundled stdio adapter lets stdio-only clients talk to the HTTP proxy.
- **Policy engine** — per-upstream method ACLs with wildcards,
  token-bucket rate limits, and request-size caps. Edited live and
  hot-reloaded atomically with rollback on failure.
- **Live observability** — every forwarded request is recorded
  (metadata only, never bodies) and streamed to the dashboard over SSE.
  Per-upstream p50/p95/p99 latency, error rate, rolling traffic chart.
- **OAuth 2.1 client** for upstream HTTP MCP servers — discovery
  (RFC 8414), dynamic client registration (RFC 7591), authorization
  code + PKCE (RFC 7636), token refresh with encrypted storage.
- **Encrypted secrets store** (Fernet, rotatable) and a DB-backed
  config store — survives restarts, exports cleanly, atomic apply
  with rollback.

## Quickstart

```bash
pip install mcpxy-proxy
mcpxy-proxy serve
```

Open <http://127.0.0.1:8000/admin>. On first run the Onboarding wizard
generates your admin token, walks you through installing your first MCP
server from the catalog, and hands you the one-line command to connect
your first client. That's it.

Running from source:

```bash
git clone https://github.com/jej2k5/mcpxy && cd mcpxy
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
mcpxy-proxy serve
```

## Connect a client

```bash
mcpxy-proxy install --client claude-desktop
mcpxy-proxy install --client claude-code
mcpxy-proxy install --client chatgpt
```

Each command backs up the target client's config and registers MCPxy as
an MCP server. The dashboard's **Connect** page shows the same commands
and copy-paste snippets for each client.

## Docker

```bash
docker compose up -d
```

The image bundles Node (for `npx`-based catalog entries like
`filesystem`, `github`, `puppeteer`) and `uv`/`uvx` (for Python-based
entries like `mcp-server-git`), so every catalog entry installs from the
dashboard with zero host dependencies. Config is mounted read-only at
`/etc/mcpxy/config.json`; runtime state lives in the `mcpxy_data` volume.
The install CLI (`mcpxy-proxy install --client ...`) writes to your host's
client config files and must run on the host, not inside the container.

## Dashboard tour

| Page | |
| --- | --- |
| **Overview** — uptime, total requests, error rate, p95 latency, rolling traffic chart, per-upstream latency table. | ![Overview](docs/screenshots/dashboard-overview.png) |
| **Routes** — one card per upstream with health, transport, discovered tools, restart button. | ![Routes](docs/screenshots/dashboard-routes.png) |
| **Traffic** — SSE stream of every forwarded request (metadata only). Filter by upstream, method, status; pause and clear. | ![Traffic](docs/screenshots/dashboard-traffic.png) |
| **Policies** — method ACLs, token-bucket rate limits, and size caps. Global and per-upstream, hot-reloaded atomically. | ![Policies](docs/screenshots/dashboard-policies.png) |
| **Browse** — catalog of 14 well-known MCP servers with search, categories, one-click install with variable prompts. | ![Browse](docs/screenshots/dashboard-browse.png) |
| **Import** — scans installed clients for MCP servers you already have and brings them in with one click. | ![Import](docs/screenshots/dashboard-import.png) |
| **Connect** — one-click snippets and install commands for Claude Desktop, Claude Code, and ChatGPT. | ![Connect](docs/screenshots/dashboard-connect.png) |

## Configuration

The active config lives in the local DB (default:
`sqlite:///~/.mcpxy/mcpxy.db`, override with `MCPXY_DB_URL`). Bootstrap
from a JSON file with `mcpxy-proxy serve --config config.json` on first
run, or `mcpxy-proxy config import config.json` at any time. A minimal
config looks like:

```json
{
  "default_upstream": "git",
  "auth": {"token_env": "MCP_PROXY_TOKEN"},
  "upstreams": {
    "git": {"type": "stdio", "command": "uvx", "args": ["mcp-server-git", "--repository", "/repo"]},
    "search": {"type": "http", "url": "https://example.com/mcp"}
  }
}
```

See [`docs/Design.md`](docs/Design.md) for the full schema, including
the policy engine, telemetry sinks, admin MCP method reference, plugin
entry points, and architecture diagrams.

## Serving HTTPS

**MCPxy serves HTTPS by default.** On first run, `mcpxy-proxy serve`
auto-generates a self-signed certificate for `localhost`, `127.0.0.1`,
and `::1` and caches it under `<state-dir>/tls/cert.pem` +
`<state-dir>/tls/key.pem` (default state dir: `~/.mcpxy`). Subsequent
runs reuse the same cert. Clients need to pass `-k` to curl, or trust
the cert via their OS keychain, until you swap in a real one.

```bash
mcpxy-proxy serve                              # HTTPS with auto-gen cert
curl -k https://127.0.0.1:8000/health
```

For production, point MCPxy at a real cert/key pair from the command
line:

```bash
mcpxy-proxy serve --listen 0.0.0.0:8443 \
    --ssl-certfile /etc/mcpxy/cert.pem \
    --ssl-keyfile /etc/mcpxy/key.pem
```

…or from the config file, where the keyfile password can flow through
the normal `${env:NAME}` / `${secret:NAME}` expansion so it never sits
in cleartext:

```json
{
  "tls": {
    "enabled": true,
    "certfile": "/etc/mcpxy/cert.pem",
    "keyfile": "/etc/mcpxy/key.pem",
    "keyfile_password": "${secret:TLS_KEY_PW}"
  }
}
```

CLI flags override the config values, and an explicit `tls` block in
the config overrides the auto-gen default, so you never end up with a
stray self-signed cert when you've set real ones.

To opt out of TLS entirely — e.g. behind a reverse proxy that
terminates TLS upstream — pass `--no-tls`:

```bash
mcpxy-proxy serve --no-tls
```

> **Note on MCP clients and self-signed certs.** Client applications
> (Claude Desktop, Cursor, Continue, ...) won't trust the auto-generated
> cert out of the box. `mcpxy-proxy install --client ... --url ...` still
> defaults to `http://127.0.0.1:8000`; point it at your HTTPS URL
> explicitly (`--url https://127.0.0.1:8000`) and either trust the cert
> in the client's OS keychain or pass `--no-tls` to MCPxy if you'd
> rather keep the loopback plaintext.

### Outbound TLS to upstream MCP servers

For HTTPS upstreams behind a private CA or that require mutual TLS,
each upstream can carry a `tls` block:

```json
{
  "upstreams": {
    "internal-mcp": {
      "type": "http",
      "url": "https://mcp.internal.corp/rpc",
      "tls": {
        "verify": "/etc/mcpxy/corp-ca.pem",
        "client_cert": "/etc/mcpxy/mcpxy-client.pem",
        "client_key": "/etc/mcpxy/mcpxy-client.key",
        "client_key_password": "${secret:INTERNAL_CLIENT_KEY_PW}"
      }
    }
  }
}
```

Fields:

| field | type | notes |
|---|---|---|
| `verify` | `true` \| `false` \| `str` | `true` (default) uses the system CA bundle via certifi; `false` disables verification entirely and logs a loud warning — **not recommended**; a string is treated as a path to a PEM-encoded CA bundle. |
| `client_cert` | `str` | Path to the client certificate PEM. Presented during the TLS handshake for mTLS. |
| `client_key` | `str` | Path to the client private key PEM. Required when `client_cert` is a standalone cert; omit it if the cert PEM bundles the key inline. |
| `client_key_password` | `str` | Password for an encrypted `client_key`. Flows through `${env:}` / `${secret:}` expansion and is redacted in admin API responses. |

Missing cert/key/CA files fail fast at upstream start time with a
clear `RuntimeError` instead of a connection reset on the first
request.

TLS settings are **not hot-reloadable** — the listener's SSL context is
bound at startup, so the atomic-apply pipeline rejects any config
change that alters the `tls` block with a clear "restart required"
error. For production deployments that need certificate auto-rotation
(Let's Encrypt, etc.), fronting MCPxy with nginx / Caddy / Traefik is
still the simplest path.

## CLI reference

```
mcpxy-proxy serve                 Run the proxy + dashboard
mcpxy-proxy init                  Generate a starter config file
mcpxy-proxy install --client ...  Install MCPxy into Claude Desktop / Code / ChatGPT
mcpxy-proxy stdio --connect URL   Stdio adapter for stdio-only clients
mcpxy-proxy register --name ...   Register an upstream on a running proxy
mcpxy-proxy unregister --name ... Remove an upstream
mcpxy-proxy discover              Scan local clients for MCP servers
mcpxy-proxy import --client ...   Import upstreams from another client
mcpxy-proxy catalog list          List bundled catalog entries
mcpxy-proxy catalog install ID    Install a catalog entry as an upstream
mcpxy-proxy config show           Print the active DB config as JSON
mcpxy-proxy config import PATH    Import a JSON config into the DB
mcpxy-proxy config export PATH    Export the active DB config to JSON
mcpxy-proxy config history        List recent config applies
mcpxy-proxy secrets list          List encrypted secrets (values never printed)
mcpxy-proxy secrets set NAME      Create or rotate a secret
mcpxy-proxy secrets delete NAME   Delete a secret
```

Drop a JSON file into `~/.mcpxy/upstreams.d/` and the running proxy picks
it up on the next poll; delete it to remove the upstream. Useful for
provisioning and CI. All three paths — catalog, import, file-drop — go
through the same atomic apply + rollback pipeline.

## Learn more

- **Architecture & design:** [`docs/Design.md`](docs/Design.md)
- **Contributing:** [`CONTRIBUTING.md`](CONTRIBUTING.md)
- **Security policy:** [`SECURITY.md`](SECURITY.md)
- **Changelog:** [`CHANGELOG.md`](CHANGELOG.md)
- **License:** MIT — see [`LICENSE`](LICENSE)
