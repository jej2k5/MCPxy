# MCPxy Configuration Reference

This document covers every configuration field, environment variable, and
placeholder syntax MCPxy supports. For a deployment walkthrough, see
[`admin-guide.md`](admin-guide.md). For authentication provider setup, see
[`auth.md`](auth.md). For policy authoring, see [`policies.md`](policies.md).

---

## Where config lives

MCPxy is configured through a live **database** (SQLite, Postgres, or MySQL),
not a static file. The JSON file is only used to seed the database on first run.

```
Config lifecycle:
  JSON file  →  first-run import  →  DB (config_kv table)  →  live process
                                      ↑
                              hot-reload applies here
```

**Database location** (resolved in priority order):
1. `MCPXY_DB_URL` environment variable
2. `<state-dir>/bootstrap.json` (written by onboarding wizard)
3. `sqlite:///<state-dir>/mcpxy.db` (default)

**CLI config commands:**
```bash
mcpxy-proxy config show              # print active config (secrets redacted)
mcpxy-proxy config import path.json  # apply a new config from file
mcpxy-proxy config export path.json  # save active config to file
mcpxy-proxy config history           # list recent applies with version numbers
```

---

## Placeholder expansion

MCPxy expands two kinds of placeholders in string config values before
validating the config:

| Syntax | Source | Notes |
|---|---|---|
| `${env:NAME}` | Environment variable | Replaced at load time; empty string if unset |
| `${secret:NAME}` | Encrypted secrets store | Replaced at apply time; validation fails if secret is missing |

Placeholders can appear in any string field — tokens, passwords, paths, URLs.

**Example:**
```json
{
  "auth": {
    "token": "${secret:ADMIN_TOKEN}"
  },
  "upstreams": {
    "github": {
      "type": "stdio",
      "env": { "GITHUB_TOKEN": "${env:GITHUB_TOKEN}" }
    }
  }
}
```

Secret-like fields (containing `key`, `token`, `auth`, `secret`, `password`,
`credential`) are **automatically redacted** (`***REDACTED***`) in all admin
API responses so they are never returned to the dashboard.

---

## Top-level schema (`AppConfig`)

| Field | Type | Default | Description |
|---|---|---|---|
| `default_upstream` | `string \| null` | `null` | Upstream name to use when no routing hint is present. Must exist in `upstreams`. |
| `auth` | `AuthConfig` | see below | Authentication settings |
| `admin` | `AdminConfig` | see below | Admin MCP endpoint settings |
| `telemetry` | `TelemetryConfig` | see below | Telemetry pipeline settings |
| `upstreams` | `dict[name → UpstreamConfig]` | `{}` | Named upstream MCP servers |
| `policies` | `PoliciesConfig` | see below | Global and per-upstream policies |
| `tls` | `TlsConfig` | see below | Inbound HTTPS listener settings |

---

## `auth` block

Controls how clients authenticate against MCPxy.

| Field | Type | Default | Description |
|---|---|---|---|
| `token` | `string \| null` | `null` | Literal bearer token. Use `${secret:NAME}` in production. |
| `token_env` | `string \| null` | `null` | Name of an env var whose value is the bearer token (e.g. `MCP_PROXY_TOKEN`). |
| `authy` | `AuthyConfig` | disabled | Multi-provider auth block. When `authy.enabled` is `true`, `token` and `token_env` are ignored. |

**Priority:** `auth.authy.enabled=true` > `auth.token` > `auth.token_env`.

If nothing is configured, MCPxy operates without authentication (dev only;
the dashboard displays a warning).

### `auth.authy` block (`AuthyConfig`)

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `false` | Enable authy multi-provider auth |
| `primary_provider` | `"local" \| "google" \| "m365" \| "sso_oidc" \| "sso_saml"` | required when enabled | Provider to use |
| `jwt_secret` | `string` | required when enabled | Secret for signing session JWTs. Use `${secret:JWT_SECRET}`. |
| `token_ttl_s` | `int` (≥300) | `86400` | JWT lifetime in seconds |
| `cookie_name` | `string` | `"mcpxy_session"` | Session cookie name |
| `cookie_secure` | `bool` | `true` | Set `Secure` flag on cookie |
| `cookie_same_site` | `"lax" \| "strict" \| "none"` | `"lax"` | SameSite cookie policy |
| `local` | `AuthyLocalConfig \| null` | auto-created | Local user/password provider config |
| `google` | `AuthyGoogleConfig \| null` | `null` | Google OAuth config |
| `m365` | `AuthyM365Config \| null` | `null` | Microsoft 365 OAuth config |
| `sso_oidc` | `AuthyOidcConfig \| null` | `null` | Generic OIDC SSO config |
| `sso_saml` | `AuthySamlConfig \| null` | `null` | SAML SSO config |

See [`auth.md`](auth.md) for provider-specific field details and setup walkthroughs.

---

## `admin` block

| Field | Type | Default | Description |
|---|---|---|---|
| `mount_name` | `string` | `"__admin__"` | Name of the admin MCP upstream at `/mcp/<mount_name>` |
| `enabled` | `bool` | `true` | Enable the admin MCP interface |
| `require_token` | `bool` | `true` | Require auth for admin MCP calls |
| `allowed_clients` | `list[string]` | `[]` | IP allowlist for admin access; empty means all IPs allowed |

---

## `telemetry` block

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `true` | Enable telemetry recording |
| `sink` | `string` | `"noop"` | Sink plugin name: `"noop"` (discard) or `"http"` (POST to endpoint) |
| `endpoint` | `string \| null` | `null` | Target URL for the `http` sink |
| `headers` | `dict[string, string]` | `{}` | Extra headers for `http` sink requests; secret-shaped keys are redacted in API responses |
| `batch_size` | `int` (≥1) | `50` | Events to accumulate before flushing |
| `flush_interval_ms` | `int` (≥1) | `2000` | Max time between flushes (ms) |
| `queue_max` | `int` (≥1) | `1000` | Bounded queue depth; overflow handled by `drop_policy` |
| `drop_policy` | `"drop_oldest" \| "drop_newest"` | `"drop_newest"` | Which end to drop when queue is full |

Custom telemetry sinks can be added via the `mcpxy_proxy.telemetry_sinks`
entry point group. See [`architecture.md`](architecture.md).

---

## `upstreams` block

A dictionary of named upstream MCP servers. Each entry is either a **stdio**
upstream (subprocess) or an **http** upstream (remote URL).

### Stdio upstream (`type: "stdio"`)

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | `"stdio"` | required | Transport type |
| `command` | `string` | required | Executable to run (e.g. `uvx`, `python`, `npx`) |
| `args` | `list[string]` | `[]` | Command arguments |
| `env` | `dict[string, string]` | `{}` | Extra environment variables; use `${env:FOO}` or `${secret:FOO}` for secrets |
| `queue_size` | `int` | `200` | In-flight request queue depth |

**Example:**
```json
{
  "upstreams": {
    "git": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-server-git", "--repository", "/repo"],
      "env": {}
    },
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${secret:GITHUB_PAT}" }
    }
  }
}
```

### HTTP upstream (`type: "http"`)

| Field | Type | Default | Description |
|---|---|---|---|
| `type` | `"http"` | required | Transport type |
| `url` | `string` | required | Base URL of the upstream MCP server |
| `headers` | `dict[string, string]` | `{}` | Static extra HTTP headers |
| `auth` | `HttpAuthConfig \| null` | `null` | Upstream authentication (see below) |
| `timeout_s` | `float` | `30.0` | Per-request timeout in seconds |
| `tls` | `HttpUpstreamTlsConfig \| null` | `null` | Outbound TLS settings (see below) |
| `token_transform` | `TokenTransformConfig \| null` | `null` | Token transformation policy (see below) |

#### `auth` sub-block (HTTP upstream auth)

One of the following shapes, discriminated by `type`:

**Bearer token** (most common):
```json
{ "type": "bearer", "token": "${secret:MY_API_TOKEN}" }
```

**API key header:**
```json
{ "type": "api_key", "header": "X-Api-Key", "value": "${secret:MY_KEY}" }
```

**Basic auth:**
```json
{ "type": "basic", "username": "myuser", "password": "${secret:MY_PASS}" }
```

**OAuth 2.1** (discovery + PKCE):
```json
{
  "type": "oauth2",
  "issuer": "https://auth.example.com",
  "client_id": "mcpxy-client",
  "client_secret": "${secret:OAUTH_SECRET}",
  "scopes": ["mcp:read", "mcp:write"]
}
```

OAuth 2.1 fields:

| Field | Type | Default | Description |
|---|---|---|---|
| `issuer` | `string \| null` | `null` | Auth server base URL for RFC 8414 discovery |
| `authorization_endpoint` | `string \| null` | `null` | Explicit auth endpoint (alternative to `issuer`) |
| `token_endpoint` | `string \| null` | `null` | Explicit token endpoint (alternative to `issuer`) |
| `client_id` | `string \| null` | `null` | Pre-issued client ID; not needed if `dynamic_registration: true` |
| `client_secret` | `string \| null` | `null` | Client secret |
| `scopes` | `list[string]` | `[]` | OAuth scopes to request |
| `redirect_uri` | `string \| null` | `null` | Callback URL; defaults to `<proxy-base>/admin/api/authy/callback` |
| `dynamic_registration` | `bool` | `false` | Use RFC 7591 dynamic client registration |

#### `tls` sub-block (outbound TLS for HTTP upstreams)

| Field | Type | Default | Description |
|---|---|---|---|
| `verify` | `bool \| string` | `true` | `true` = system CA bundle, `false` = disable (not recommended), string = path to CA PEM |
| `client_cert` | `string \| null` | `null` | Path to client certificate PEM (for mTLS) |
| `client_key` | `string \| null` | `null` | Path to client private key PEM |
| `client_key_password` | `string \| null` | `null` | Password for encrypted client key; supports `${secret:}` expansion |

#### `token_transform` sub-block

| Field | Type | Default | Description |
|---|---|---|---|
| `strategy` | `"static" \| "passthrough" \| "map" \| "header_inject"` | `"static"` | How to map the client token to the upstream credential |
| `inject_header` | `string` | `"X-MCPxy-User"` | Header name for `header_inject` strategy |
| `fallback_on_missing_map` | `"deny" \| "static"` | `"deny"` | What to do when `strategy=map` and no mapping exists for the user |

See [`auth.md`](auth.md) for token transformation workflow.

---

## `policies` block

Controls method ACLs, rate limits, size caps, and redaction. See
[`policies.md`](policies.md) for authoring guidance.

**Shape:**
```json
{
  "policies": {
    "global": { ... },
    "per_upstream": {
      "upstream-name": { ... }
    }
  }
}
```

Both `global` and per-upstream entries are `UpstreamPolicies` objects:

| Field | Type | Description |
|---|---|---|
| `methods` | `MethodPolicy \| null` | Method ACL (allow/deny lists with wildcard support) |
| `rate_limit` | `RateLimitPolicy \| null` | Token-bucket rate limit |
| `size` | `SizePolicy \| null` | Request payload size cap |
| `redaction` | `RedactionPolicy \| null` | PII/PCI redaction config |

### `methods` (method ACL)

| Field | Type | Description |
|---|---|---|
| `allow` | `list[string] \| null` | Whitelist; `null` = allow all |
| `deny` | `list[string] \| null` | Blacklist; `null` = deny none |

Patterns use `fnmatch` wildcards (`*`, `?`). Deny takes precedence over allow.

### `rate_limit`

| Field | Type | Description |
|---|---|---|
| `requests_per_second` | `float` (>0) | Refill rate of the token bucket |
| `burst` | `int` (>0) | Maximum burst capacity |
| `scope` | `"upstream" \| "client_ip" \| "both"` | Counting scope |

### `size`

| Field | Type | Description |
|---|---|---|
| `max_request_bytes` | `int` (>0) | Maximum request payload size in bytes; requests above this are rejected |

### `redaction`

| Field | Type | Default | Description |
|---|---|---|---|
| `pii` | `bool` | `true` | Enable built-in PII patterns (email, phone, SSN, IPv4) |
| `pci` | `bool` | `true` | Enable built-in PCI patterns (card PAN, CVV, expiry) |
| `redact_request` | `bool` | `true` | Scrub outgoing requests to upstreams |
| `redact_response` | `bool` | `true` | Scrub incoming responses from upstreams |
| `replacement` | `string` | `"[REDACTED]"` | Replacement text for matched values |
| `custom_patterns` | `dict[label → regex]` | `{}` | Additional patterns compiled at apply time; invalid regex fails validation |

---

## `tls` block (inbound HTTPS)

> **Not hot-reloadable.** TLS changes require a process restart.

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `false` | Enable HTTPS listener (auto-gen cert is separate from this block) |
| `certfile` | `string \| null` | `null` | Path to PEM certificate |
| `keyfile` | `string \| null` | `null` | Path to PEM private key |
| `keyfile_password` | `string \| null` | `null` | Password for encrypted key; supports `${secret:}` expansion |

When `tls.enabled=false` (the default), MCPxy auto-generates a self-signed
cert for loopback on first run. Pass `--no-tls` to disable TLS entirely
(useful behind a reverse proxy).

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MCPXY_CONFIG` | `/etc/mcpxy/config.json` (Docker) | Path to the seed config JSON file. Only read on first run when the DB is empty. |
| `MCPXY_LISTEN` | `127.0.0.1:8000` | `host:port` for the HTTP listener. Override to `0.0.0.0:8000` for network access. |
| `MCPXY_STATE_DIR` | `~/.mcpxy` (host) / `/var/lib/mcpxy` (Docker) | Directory for DB, TLS certs, secrets key, and `upstreams.d/`. |
| `MCPXY_DB_URL` | `sqlite:///<state-dir>/mcpxy.db` | SQLAlchemy DB URL. Use `postgresql://user:pass@host:5432/mcpxy` or `mysql+pymysql://...` for other backends. |
| `MCPXY_SECRETS_KEY` | auto-generated | Fernet encryption key (32 bytes, URL-safe base64). Auto-generated on first run; **back this up**. |
| `MCPXY_ONBOARDING_TTL_S` | `1800` | Seconds before the onboarding wizard endpoints expire (default 30 minutes). |
| `MCPXY_ONBOARDING_ALLOWED_CLIENTS` | `127.0.0.1,::1` | Comma-separated IPs/CIDRs allowed to access onboarding endpoints. |
| `MCP_PROXY_TOKEN` | _(none)_ | Legacy single bearer token (equivalent to `auth.token_env: MCP_PROXY_TOKEN`). Ignored when `auth.authy.enabled=true`. |

---

## Worked examples

### Minimal (dev/localhost)
```json
{
  "default_upstream": "git",
  "auth": { "token_env": "MCP_PROXY_TOKEN" },
  "upstreams": {
    "git": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-server-git", "--repository", "/repo"]
    }
  }
}
```

### Production with Postgres and Google auth
```json
{
  "default_upstream": "github",
  "auth": {
    "authy": {
      "enabled": true,
      "primary_provider": "google",
      "jwt_secret": "${secret:JWT_SECRET}",
      "token_ttl_s": 28800,
      "google": {
        "client_id": "1234567890.apps.googleusercontent.com",
        "client_secret": "${secret:GOOGLE_CLIENT_SECRET}",
        "redirect_uri": "https://mcp.example.com/admin/api/authy/callback"
      }
    }
  },
  "upstreams": {
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${secret:GITHUB_PAT}" }
    }
  }
}
```

Set `MCPXY_DB_URL=postgresql://mcpxy:pass@db:5432/mcpxy` in the environment.

### OAuth 2.1 upstream
```json
{
  "upstreams": {
    "internal-api": {
      "type": "http",
      "url": "https://mcp.internal.corp/rpc",
      "auth": {
        "type": "oauth2",
        "issuer": "https://auth.internal.corp",
        "client_id": "mcpxy",
        "client_secret": "${secret:INTERNAL_OAUTH_SECRET}"
      }
    }
  }
}
```

### mTLS upstream with private CA
```json
{
  "upstreams": {
    "secure-api": {
      "type": "http",
      "url": "https://mcp.internal.corp/rpc",
      "tls": {
        "verify": "/etc/mcpxy/corp-ca.pem",
        "client_cert": "/etc/mcpxy/mcpxy-client.pem",
        "client_key": "/etc/mcpxy/mcpxy-client.key",
        "client_key_password": "${secret:CLIENT_KEY_PW}"
      }
    }
  }
}
```

### PII and PCI redaction
```json
{
  "policies": {
    "global": {
      "redaction": {
        "pii": true,
        "pci": true,
        "redact_request": true,
        "redact_response": true,
        "replacement": "[REDACTED]",
        "custom_patterns": {
          "internal_id": "CORP-[0-9]{6}"
        }
      }
    }
  }
}
```

### Rate limit on a specific upstream
```json
{
  "policies": {
    "per_upstream": {
      "search": {
        "rate_limit": {
          "requests_per_second": 5.0,
          "burst": 10,
          "scope": "client_ip"
        }
      }
    }
  }
}
```
