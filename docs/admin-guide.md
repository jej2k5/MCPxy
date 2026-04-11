# MCPxy Administrator Guide

This guide walks an IT operator from zero to a running MCPxy deployment. It
covers local installation, Docker, TLS, adding MCP servers, secrets, and
day-2 operations.

For a full config field reference, see [`configuration.md`](configuration.md).
For authentication provider setup, see [`auth.md`](auth.md).
For policy authoring, see [`policies.md`](policies.md).
For troubleshooting, see [`troubleshooting.md`](troubleshooting.md).

---

## Who this guide is for

You have a Linux/macOS host (or Docker host) and want to run MCPxy so that
your team's AI clients all point at one endpoint instead of each maintaining
their own list of MCP servers. You do not need to be a Python developer —
`pip install` and a text editor are enough.

---

## Deployment options

| Option | Best for |
|---|---|
| **pip, single host** | Personal use; developer workstation; quick evaluation |
| **Docker Compose** | Single-server team deployment; easiest to maintain |
| **Behind a reverse proxy** (nginx/Caddy/Traefik) | When you need a real TLS cert, hostname routing, or to share port 443 with other services |
| **systemd service** | Bare-metal server without Docker |

All options use the same config format and dashboard.

---

## Install: pip path

### Requirements

- Python 3.11 or newer
- `pip`
- (Optional) `uv` / `uvx` and `node` / `npx` — only needed if you want to
  install catalog entries that use them (most do). The Docker image bundles
  both automatically.

### Install

```bash
python -m venv /opt/mcpxy
source /opt/mcpxy/bin/activate
pip install mcpxy-proxy
```

For Postgres:
```bash
pip install "mcpxy-proxy[postgres]"
```

For MySQL/MariaDB:
```bash
pip install "mcpxy-proxy[mysql]"
```

### First run

```bash
mcpxy-proxy serve
```

MCPxy will:
1. Create a state directory at `~/.mcpxy/` (override with `MCPXY_STATE_DIR`)
2. Generate a self-signed TLS certificate for `localhost` / `127.0.0.1` / `::1`
3. Start an HTTPS server on `127.0.0.1:8000`
4. Open the Onboarding wizard at `https://127.0.0.1:8000/admin` on first run

The state directory layout after first run:

```
~/.mcpxy/
├── mcpxy.db          # SQLite database (config, secrets, users)
├── secrets.key       # Fernet encryption key — back this up
├── tls/
│   ├── cert.pem      # Auto-generated self-signed cert
│   └── key.pem
└── upstreams.d/      # File-drop directory for provisioning
```

### systemd service (optional)

Create `/etc/systemd/system/mcpxy.service`:

```ini
[Unit]
Description=MCPxy MCP Proxy
After=network.target

[Service]
Type=simple
User=mcpxy
Group=mcpxy
Environment=MCPXY_STATE_DIR=/var/lib/mcpxy
ExecStart=/opt/mcpxy/bin/mcpxy-proxy serve --listen 0.0.0.0:8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mcpxy
```

---

## Install: Docker path

The Docker image bundles Python, Node, `uv`/`uvx`, and `git` so every
catalog entry installs from the dashboard with no host dependencies.

### Quick start

```bash
# Clone or download the repo, then:
docker compose up -d
```

The compose file at the repo root brings up MCPxy on port 8000 with:
- Config bind-mounted from `deploy/docker/config.json` (read-only)
- State persisted in the `mcpxy_data` named volume
- HTTPS with an auto-generated self-signed cert

Open `https://localhost:8000/admin` (accept the cert warning, or use `-k`
with curl).

### Environment variables

Set these in a `.env` file next to `docker-compose.yml`:

| Variable | Default | Description |
|---|---|---|
| `MCPXY_HOST_PORT` | `8000` | Host port to publish |
| `MCP_PROXY_TOKEN` | _(empty)_ | Legacy bearer token (leave empty to use onboarding wizard instead) |
| `MCPXY_DB_URL` | sqlite in volume | Override to use Postgres/MySQL |
| `MCPXY_SECRETS_KEY` | auto-generated | Fernet key — set explicitly if you need the key to survive a volume wipe |
| `MCPXY_ONBOARDING_ALLOWED_CLIENTS` | `127.0.0.1,::1` | IPs allowed to access the onboarding wizard |

### Using a custom config

Edit `deploy/docker/config.json` before starting, or after starting use the
dashboard Config page to apply changes hot. The JSON file is only read once
on first run (when the DB is empty); subsequent starts use the DB as the
source of truth.

### Important: client installers run on the host

The `mcpxy-proxy install --client ...` commands write to host client config
files (Claude Desktop, Cursor, etc.). Run these on the host, not inside the
container:

```bash
mcpxy-proxy install --client claude-desktop --url https://localhost:8000
```

---

## First-run onboarding wizard

The wizard runs automatically on first start when the DB is empty. It is
only accessible from loopback by default.

**Steps:**

1. **Storage backend** — choose SQLite (default, no setup) or enter a
   Postgres/MySQL connection string. MCPxy tests the connection before
   proceeding.
2. **Admin token** — click "Generate" to create your first admin credential,
   or paste an existing bearer token. Store it somewhere safe.
3. **First MCP server** (optional) — pick from a curated catalog slice or
   skip to add servers later from the Browse page.
4. **Finish** — MCPxy marks setup complete and redirects to the dashboard.

**Wizard timeout:** The onboarding endpoints expire after 30 minutes
(configurable via `MCPXY_ONBOARDING_TTL_S`). If the wizard times out, restart
MCPxy — it will re-enter onboarding mode because the DB shows an incomplete
setup.

**If you lose your admin token:**

```bash
# Reset from the CLI (MCPxy must be stopped):
mcpxy-proxy secrets set ADMIN_TOKEN
# Then restart and use the new token.
# Or, if using authy local auth, use the CLI to reset a user's password.
```

---

## Admin authentication

By default MCPxy uses a single bearer token for admin access. For teams,
enable authy multi-provider authentication to give each user their own
identity (Google, Microsoft 365, OIDC SSO, or SAML).

See [`auth.md`](auth.md) for full provider setup walkthroughs.

---

## TLS

### Auto-generated self-signed cert (default)

MCPxy generates a cert for loopback addresses on first run and caches it in
`<state-dir>/tls/`. It is reused on subsequent starts. Clients must either
trust the cert in their OS keychain or use `--insecure` / `-k`.

### Production cert (recommended)

Supply your cert and key with CLI flags:

```bash
mcpxy-proxy serve --listen 0.0.0.0:443 \
    --ssl-certfile /etc/mcpxy/cert.pem \
    --ssl-keyfile  /etc/mcpxy/key.pem
```

Or via the config file (supports `${env:}` / `${secret:}` for the key password):

```json
{
  "tls": {
    "enabled": true,
    "certfile": "/etc/mcpxy/cert.pem",
    "keyfile":  "/etc/mcpxy/key.pem",
    "keyfile_password": "${secret:TLS_KEY_PW}"
  }
}
```

> **TLS changes require a restart.** The `tls` block is not hot-reloadable.
> The dashboard will show a "restart required" error if you try to apply TLS
> changes live.

### Disable TLS (behind a reverse proxy)

```bash
mcpxy-proxy serve --no-tls
```

Use this when a reverse proxy (nginx, Caddy, Traefik) terminates TLS upstream.

### Behind a reverse proxy

Sample nginx snippet (port 443 → MCPxy on 8000 plain HTTP):

```nginx
server {
    listen 443 ssl;
    server_name mcp.example.com;

    ssl_certificate     /etc/ssl/mcp.example.com.crt;
    ssl_certificate_key /etc/ssl/mcp.example.com.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        # SSE requires these:
        proxy_buffering       off;
        proxy_read_timeout    3600s;
    }
}
```

Start MCPxy with `--no-tls --listen 127.0.0.1:8000` so it does not bind on a
public interface.

### Outbound mTLS to upstream servers

```json
{
  "upstreams": {
    "internal": {
      "type": "http",
      "url": "https://mcp.internal.corp/rpc",
      "tls": {
        "verify": "/etc/mcpxy/corp-ca.pem",
        "client_cert": "/etc/mcpxy/mcpxy-client.pem",
        "client_key":  "/etc/mcpxy/mcpxy-client.key"
      }
    }
  }
}
```

Set `"verify": false` only for testing; MCPxy logs a loud warning and it is
not recommended for production.

---

## Adding MCP servers

There are four ways to add an upstream MCP server. All paths go through the
same atomic apply + rollback pipeline.

### 1. Browse the bundled catalog

Open the dashboard Browse page, search or filter by category, click **Install**,
fill in any variable prompts (paths, tokens), and click **Add**. MCPxy starts
the server immediately.

Bundled servers include: filesystem, git, github, gitlab, memory, postgres,
sqlite, brave-search, fetch, puppeteer, slack, time, everart, sentry.

### 2. Import from an existing client

Open the dashboard Import page. MCPxy scans your local Claude Desktop, Claude
Code, Cursor, Windsurf, and Continue configs and lists all MCP servers it finds.
Select the ones you want and click **Import**.

### 3. File-drop (provisioning / CI)

Drop a JSON file into `<state-dir>/upstreams.d/`. MCPxy polls the directory and
picks up additions and removals automatically — no restart required.

```bash
cat > ~/.mcpxy/upstreams.d/my-server.json << 'EOF'
{
  "my-server": {
    "type": "stdio",
    "command": "uvx",
    "args": ["my-mcp-server"]
  }
}
EOF
```

Delete the file to remove the upstream.

### 4. CLI

```bash
mcpxy-proxy register --name my-server --command uvx --args my-mcp-server
mcpxy-proxy unregister --name my-server
```

---

## Secrets management

MCPxy stores all secrets (upstream credentials, OAuth tokens, PAT hashes) in the
database, encrypted with a Fernet key. The key is stored in `MCPXY_SECRETS_KEY`
(auto-generated on first run; printed once to stderr and cached in the state dir).

**Back up `secrets.key`** — you cannot decrypt stored secrets without it.

### CLI commands

```bash
mcpxy-proxy secrets list              # list secret names (values never printed)
mcpxy-proxy secrets set MY_TOKEN      # create or replace; prompted for value
mcpxy-proxy secrets delete MY_TOKEN   # remove
```

### Referencing secrets in config

```json
{
  "auth": {
    "authy": {
      "google": {
        "client_secret": "${secret:GOOGLE_CLIENT_SECRET}"
      }
    }
  }
}
```

The `${env:NAME}` syntax expands from environment variables; `${secret:NAME}`
expands from the encrypted store. Both are redacted in admin API responses.

### Key rotation

To rotate the Fernet key:

```bash
# 1. Export all current secrets in plaintext to a temp file
mcpxy-proxy secrets export --plaintext /tmp/secrets-backup.json  # if available

# 2. Generate a new key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 3. Set MCPXY_SECRETS_KEY to the new value and restart MCPxy

# 4. Re-import secrets under the new key
# (or re-enter them via the dashboard)
```

---

## Day-2 operations

### Hot-reload config

Most config changes (upstreams, policies, auth settings) take effect immediately
without a restart. Apply them from the dashboard Config page or the CLI:

```bash
mcpxy-proxy config import new-config.json
```

MCPxy validates the new config, applies it atomically, and rolls back if
validation fails. The TLS block is the only exception — it requires a restart.

### Config history and rollback

```bash
mcpxy-proxy config history         # list recent applies with version numbers
mcpxy-proxy config show            # print the current active config
mcpxy-proxy config export out.json # save current config to a file
```

To roll back to a previous version, export it from history and re-import:

```bash
# From the dashboard: Config page → History tab → select version → Restore
```

### Log locations

| Deployment | Log location |
|---|---|
| pip / systemd | stdout/stderr → `journalctl -u mcpxy -f` |
| Docker Compose | `docker compose logs -f mcpxy` |

Increase log verbosity by setting `MCPXY_LOG_LEVEL=debug` before starting.

### Health check

```bash
curl -k https://127.0.0.1:8000/health    # {"status": "ok"}
curl -k https://127.0.0.1:8000/status    # detailed: upstreams, version, uptime
```

### Upgrade

```bash
# pip:
pip install --upgrade mcpxy-proxy
mcpxy-proxy serve  # schema migrations run automatically on start

# Docker:
docker compose pull && docker compose up -d
```

Database migrations are additive (`CREATE TABLE IF NOT EXISTS`). No manual
migration steps are required between patch versions.

### Backup

Minimum backup set:
1. `<state-dir>/mcpxy.db` (or your Postgres/MySQL dump)
2. `<state-dir>/secrets.key` (or `MCPXY_SECRETS_KEY` env var value)

Without both you cannot restore a working instance. Back them up together.

### Uninstall

```bash
# pip:
pip uninstall mcpxy-proxy
rm -rf ~/.mcpxy

# Docker:
docker compose down -v   # -v removes the mcpxy_data volume
```
