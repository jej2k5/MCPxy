# MCPxy Troubleshooting

Quick answers to common problems. For configuration details, see
[`configuration.md`](configuration.md); for deployment details, see
[`admin-guide.md`](admin-guide.md).

---

## Log locations and levels

| Deployment | How to view logs |
|---|---|
| pip / systemd | `journalctl -u mcpxy -f` or terminal stdout |
| Docker Compose | `docker compose logs -f mcpxy` |
| Docker (direct) | `docker logs -f mcpxy` |

**Increase log verbosity:**
```bash
MCPXY_LOG_LEVEL=debug mcpxy-proxy serve ...
```

**Key log patterns to watch:**

| Pattern | Meaning |
|---|---|
| `upstream <name> started` | Subprocess upstream started successfully |
| `upstream <name> exited (code N)` | Upstream process crashed; MCPxy will restart it |
| `config applied (version N)` | Hot-reload succeeded |
| `config rollback: <reason>` | Hot-reload failed; previous config is active |
| `rate limit exceeded` | A request was rate-limited |
| `redaction applied` | PII/PCI redaction ran on a request or response |
| `onboarding: TTL expired` | Onboarding wizard timed out; restart to reset |

---

## Client won't connect

**Symptom:** MCP client (Claude Desktop, Cursor, etc.) fails to connect to MCPxy.

**Check 1 — URL and port**
```bash
curl -k https://127.0.0.1:8000/health
# should return: {"status":"ok"}
```

If this fails, MCPxy is not running or bound to a different address. Check with:
```bash
mcpxy-proxy serve --listen 127.0.0.1:8000  # verify address matches client config
```

**Check 2 — self-signed cert**

Most MCP clients do not trust self-signed certs. Options:
- Use `http://` for loopback (pass `--no-tls` to MCPxy)
- Trust the cert: `<state-dir>/tls/cert.pem` — import into system/app keychain
- Use a real cert with `--ssl-certfile` / `--ssl-keyfile`

**Check 3 — auth header**

If MCPxy is configured with a bearer token, the client must send it:
```
Authorization: Bearer <your-token>
```

Install the client correctly with:
```bash
mcpxy-proxy install --client claude-desktop --url https://127.0.0.1:8000
```

**Check 4 — container vs host**

The `mcpxy-proxy install` command must run on the **host**, not inside the
container. It writes to host client config files.

---

## TLS errors

**"certificate verify failed" or "SSL: CERTIFICATE_VERIFY_FAILED"**

The client does not trust MCPxy's certificate.
- For dev/loopback: switch to `--no-tls` and use `http://`
- For production: replace the auto-generated cert with a CA-signed one
- For testing with curl: use the `-k` / `--insecure` flag

**"restart required" when applying config**

The `tls` block in your config changed. TLS settings are not hot-reloadable.
Restart MCPxy to apply the new TLS settings:
```bash
# systemd:
sudo systemctl restart mcpxy
# Docker:
docker compose restart mcpxy
```

**"cert file not found" on startup**

The paths in `tls.certfile` / `tls.keyfile` do not exist or are not readable
by the MCPxy process. Check permissions and that the paths are absolute.

---

## OAuth upstream stuck / login loop

**Symptom:** Redirected to the OAuth provider but stuck in a loop after
authenticating, or "OAuth state mismatch" / "400 Bad Request" error.

**Check 1 — redirect URI mismatch**

The `redirect_uri` in your config must exactly match what is registered at
the OAuth provider (Google Cloud Console, Azure portal, etc.), including the
scheme and path. The correct callback URL is:
```
https://<your-mcpxy-host>/admin/api/authy/callback
```

**Check 2 — clock skew**

OAuth PKCE and JWT validation are time-sensitive. If the MCPxy server's clock
is off by more than ~30 seconds, tokens will be rejected. Sync time:
```bash
sudo timedatectl set-ntp true   # Linux
```

**Check 3 — DCR (dynamic client registration) failure**

If `dynamic_registration: true`, MCPxy attempts to register itself at the
auth server on first use. If the registration endpoint rejects it (403, 405),
switch to a pre-registered `client_id`/`client_secret`.

**Check 4 — refresh token revoked**

If a user's upstream OAuth token was revoked at the provider, MCPxy's stored
refresh token is invalid. The user must re-authorize:
1. Dashboard → TokenMappings → remove the affected mapping
2. User logs in again and re-authorizes the upstream connection

---

## Admin dashboard locked out

**Lost admin token (legacy bearer mode):**
```bash
# Stop MCPxy, then reset:
mcpxy-proxy secrets set ADMIN_TOKEN
# Use the new token value and restart
```

**Can't log in (authy mode):**
```bash
# Reset a local user's password:
mcpxy-proxy config show  # confirm authy is enabled
# If you have a PAT, use it to call the admin API to reset the password
# Otherwise: stop MCPxy, delete the DB users table row, restart → onboarding
```

**PAT lost or expired:**
- PATs do not expire by default but can be revoked from the Tokens dashboard page
- Issue a replacement: Dashboard → Tokens → New Token
- From CLI: `mcpxy-proxy secrets set --generate-pat`

---

## Hot-reload rejected

**Symptom:** Config change via dashboard or `config import` returns a validation error.

**Read the error message** — MCPxy returns a structured error explaining exactly
which field failed validation. Common causes:

| Error message | Fix |
|---|---|
| `default_upstream must exist in upstreams` | The `default_upstream` name doesn't match any key in `upstreams` |
| `authy.primary_provider is required` | Set `primary_provider` when `authy.enabled: true` |
| `tls.enabled requires certfile and keyfile` | Provide both cert and key paths when `tls.enabled: true` |
| `custom_patterns[x]: invalid regex` | Fix the regex syntax in the `redaction.custom_patterns` entry |
| `Restart required: tls block changed` | TLS cannot be hot-reloaded; restart the process |
| `Secret 'NAME' not found` | Run `mcpxy-proxy secrets set NAME` before applying the config |

**Roll back to a previous version:**
```bash
mcpxy-proxy config history  # find the last good version
# Dashboard Config page → History → select version → Restore
```

---

## Catalog install fails

**"uvx: command not found" or "npx: command not found"**

Most catalog entries use `uvx` (Python-based) or `npx` (Node-based). These
must be available on the PATH of the MCPxy process.

- **Docker:** The official image bundles both. Use `docker compose up` from
  the repo root.
- **Bare metal:** Install `uv` (`pip install uv`) and Node.js.

**"Install must run on the host"**

The `mcpxy-proxy install --client ...` command writes to host client config
files and must be run on the host, not inside the container:
```bash
# On the host (not inside Docker):
mcpxy-proxy install --client claude-desktop
```

---

## Rate limit or size cap surprises

**Debugging rate limits:**
- Open the **Traffic** page in the dashboard and look for `rate_limit_exceeded`
  events filtered by upstream
- Temporarily increase `burst` to see if that resolves the issue
- Use `scope: "upstream"` for a shared limit or `scope: "client_ip"` for
  per-client limits

**Requests larger than expected being rejected:**
- Check the `size.max_request_bytes` setting; the default in the example
  config is 1 MiB (1048576 bytes)
- Increase it for upstreams that legitimately handle large payloads:
  ```json
  { "policies": { "per_upstream": { "filesystem": { "size": { "max_request_bytes": 10485760 } } } } }
  ```

---

## Database issues

**SQLite "database is locked"**

SQLite allows only one writer at a time. If multiple MCPxy instances share the
same SQLite file, you'll see lock errors. Either:
- Run a single MCPxy instance (recommended for SQLite)
- Switch to Postgres/MySQL for multi-instance setups

**Postgres/MySQL connection refused**

Check the `MCPXY_DB_URL` value:
```
postgresql://user:password@hostname:5432/dbname
mysql+pymysql://user:password@hostname:3306/dbname
```

Confirm the database server is reachable and the credentials are correct:
```bash
psql postgresql://user:password@hostname:5432/dbname -c "SELECT 1;"
```

**"driver not installed" / "No module named psycopg2"**

Install the optional extras:
```bash
pip install "mcpxy-proxy[postgres]"  # for PostgreSQL
pip install "mcpxy-proxy[mysql]"     # for MySQL/MariaDB
```

In Docker, use the image built with `pip install ".[postgres,mysql]"` (the
default Dockerfile does this).

---

## Collecting a support bundle

When reporting an issue, include:

```bash
# 1. Version
mcpxy-proxy --version

# 2. Config (secrets automatically redacted):
mcpxy-proxy config show > config-redacted.json

# 3. Recent logs (last 200 lines):
journalctl -u mcpxy -n 200 --no-pager > mcpxy.log
# or: docker compose logs --tail=200 mcpxy > mcpxy.log

# 4. Health status:
curl -ks https://127.0.0.1:8000/status | python3 -m json.tool
```

Remove any remaining sensitive values before sharing.
