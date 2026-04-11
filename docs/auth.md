# MCPxy Authentication Guide

This guide covers admin authentication (who can access the MCPxy dashboard and
API) and upstream OAuth (how MCPxy authenticates against HTTP MCP servers).

---

## Overview

MCPxy uses the **authy** library for admin authentication. This is an internal
authentication framework (a dependency at
`git+https://github.com/jej2k5/authy`) — it is **not** Twilio Authy or any
commercial auth product.

Supported admin auth modes:
- **Local** — username and password stored in the MCPxy database
- **Google** — Google OAuth 2.0 sign-in
- **Microsoft 365** — Azure AD / M365 OAuth 2.0
- **OIDC** — any OpenID Connect–compatible identity provider
- **SAML** — SAML 2.0 enterprise SSO

All modes coexist with three credential types that clients can present:

| Credential type | Format | Typical use |
|---|---|---|
| **Bearer JWT** | `Authorization: Bearer <jwt>` | Programmatic clients, short-lived sessions |
| **Session cookie** | `mcpxy_session=<jwt>` | Browser dashboard sessions |
| **PAT** | `Authorization: Bearer pat_<token>` | Headless/CI clients needing long-lived access |

If `auth.authy.enabled` is `false` (the default), MCPxy falls back to a single
shared bearer token configured via `auth.token` or `auth.token_env`. This
legacy mode has no user management and is not recommended for multi-user
deployments.

---

## Credential types

### PAT (Personal Access Token)

PATs are long-lived, revocable credentials intended for CI systems, scripts,
and MCP clients that cannot participate in browser-based OAuth flows.

**Create a PAT from the dashboard:** Tokens page → New Token → copy the
`pat_…` value immediately (shown once).

**Create a PAT from the CLI:**
```bash
mcpxy-proxy secrets set --generate-pat  # prints the pat_ value once
```

**Use a PAT:**
```
Authorization: Bearer pat_<token>
```

**Revoke a PAT:** Tokens page → revoke, or
`mcpxy-proxy secrets delete <pat-name>`.

### Session cookie

Set automatically by the dashboard on successful login. The cookie name is
`mcpxy_session` (configurable via `auth.authy.cookie_name`). Cookie settings:

| Config field | Default | Notes |
|---|---|---|
| `cookie_secure` | `true` | Set `Secure` flag; requires HTTPS |
| `cookie_same_site` | `"lax"` | `"strict"` for higher isolation; `"none"` for cross-site (requires `Secure`) |

### JWT bearer

JWTs are minted by the login flow and returned to the frontend. The expiry is
controlled by `auth.authy.token_ttl_s` (default: 86400 seconds / 24 hours).
JWTs are signed with `auth.authy.jwt_secret` — keep this value secret and
store it as `${secret:JWT_SECRET}`.

---

## Local users

Local auth stores usernames and bcrypt-hashed passwords in the MCPxy database.

**Enable local auth:**
```json
{
  "auth": {
    "authy": {
      "enabled": true,
      "primary_provider": "local",
      "jwt_secret": "${secret:JWT_SECRET}",
      "local": {
        "token_ttl": 3600
      }
    }
  }
}
```

**First admin user:** Created during the onboarding wizard. The admin sets a
username and password; subsequent users are added via invites.

**Invites:** Dashboard → Users page → Invite User → copy the invite link.
The invited user sets their own password on first login.

**`auth.authy.local` fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `token_ttl` | `int` (≥300) | `3600` | Session token lifetime in seconds |

---

## Google sign-in

### 1. Create a Google OAuth client

1. Open [Google Cloud Console](https://console.cloud.google.com/) →
   APIs & Services → Credentials → Create Credentials → OAuth client ID
2. Application type: **Web application**
3. Authorized redirect URIs: add `https://<your-mcpxy-host>/admin/api/authy/callback`
4. Copy the **Client ID** and **Client secret**

### 2. Store the secret

```bash
mcpxy-proxy secrets set GOOGLE_CLIENT_SECRET
# paste the client secret when prompted
```

### 3. Configure MCPxy

```json
{
  "auth": {
    "authy": {
      "enabled": true,
      "primary_provider": "google",
      "jwt_secret": "${secret:JWT_SECRET}",
      "google": {
        "client_id": "1234567890-abc123.apps.googleusercontent.com",
        "client_secret": "${secret:GOOGLE_CLIENT_SECRET}",
        "redirect_uri": "https://mcp.example.com/admin/api/authy/callback"
      }
    }
  }
}
```

**Note:** `redirect_uri` must exactly match what you registered in Google
Cloud Console, including the trailing path.

---

## Microsoft 365 / Azure AD

### 1. Register an Azure app

1. Azure Portal → Azure Active Directory → App registrations → New registration
2. Redirect URI: `https://<your-mcpxy-host>/admin/api/authy/callback` (Web)
3. Under **Certificates & secrets** → New client secret → copy the value
4. Note the **Application (client) ID** and **Directory (tenant) ID**

### 2. Configure MCPxy

```json
{
  "auth": {
    "authy": {
      "enabled": true,
      "primary_provider": "m365",
      "jwt_secret": "${secret:JWT_SECRET}",
      "m365": {
        "client_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "client_secret": "${secret:M365_CLIENT_SECRET}",
        "tenant_id": "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy",
        "redirect_uri": "https://mcp.example.com/admin/api/authy/callback"
      }
    }
  }
}
```

`auth.authy.m365` fields:

| Field | Description |
|---|---|
| `client_id` | Azure application (client) ID |
| `client_secret` | Azure client secret |
| `tenant_id` | Azure directory (tenant) ID |
| `redirect_uri` | Must match the redirect URI registered in Azure |

---

## Generic OIDC SSO

Use this for any OpenID Connect–compatible IdP (Okta, Auth0, Keycloak, etc.).

```json
{
  "auth": {
    "authy": {
      "enabled": true,
      "primary_provider": "sso_oidc",
      "jwt_secret": "${secret:JWT_SECRET}",
      "sso_oidc": {
        "issuer_url": "https://accounts.example.com",
        "client_id": "mcpxy",
        "client_secret": "${secret:OIDC_CLIENT_SECRET}",
        "redirect_uri": "https://mcp.example.com/admin/api/authy/callback"
      }
    }
  }
}
```

`auth.authy.sso_oidc` fields:

| Field | Description |
|---|---|
| `issuer_url` | OIDC issuer base URL (MCPxy fetches `/.well-known/openid-configuration` from here) |
| `client_id` | Client ID registered at the IdP |
| `client_secret` | Client secret |
| `redirect_uri` | Callback URL registered at the IdP |

---

## SAML 2.0 SSO

```json
{
  "auth": {
    "authy": {
      "enabled": true,
      "primary_provider": "sso_saml",
      "jwt_secret": "${secret:JWT_SECRET}",
      "sso_saml": {
        "sp_entity_id": "https://mcp.example.com",
        "idp_sso_url": "https://saml.idp.example.com/sso",
        "idp_cert": "${secret:SAML_IDP_CERT}",
        "sp_private_key": "${secret:SAML_SP_KEY}"
      }
    }
  }
}
```

`auth.authy.sso_saml` fields:

| Field | Description |
|---|---|
| `sp_entity_id` | Service provider entity ID (typically your MCPxy base URL) |
| `idp_sso_url` | Identity provider SSO endpoint URL |
| `idp_cert` | IdP signing certificate in PEM format |
| `sp_private_key` | SP private key for signing/encrypting assertions (optional) |

Register the SP with your IdP using the entity ID and the ACS URL:
`https://<your-mcpxy-host>/admin/api/authy/callback`

---

## Upstream OAuth 2.1 client

This is separate from admin auth. MCPxy also acts as an **OAuth 2.1 client**
when connecting to upstream MCP servers that require OAuth authentication.

Supported specs:
- **RFC 8414** — Authorization server metadata discovery
- **RFC 7591** — Dynamic client registration
- **RFC 7636** — Authorization code + PKCE

Tokens are encrypted with Fernet and stored in the `secrets` table.

**Config example:**
```json
{
  "upstreams": {
    "my-api": {
      "type": "http",
      "url": "https://api.example.com/mcp",
      "auth": {
        "type": "oauth2",
        "issuer": "https://auth.example.com",
        "client_id": "mcpxy-client",
        "client_secret": "${secret:OAUTH_SECRET}",
        "scopes": ["mcp:access"],
        "dynamic_registration": false
      }
    }
  }
}
```

**Dynamic client registration:** Set `"dynamic_registration": true` to skip
pre-registering a `client_id` — MCPxy registers itself at the auth server's
registration endpoint on first use.

**Token refresh:** MCPxy automatically refreshes access tokens when they expire.
Refreshed tokens are stored encrypted in the database.

See [`configuration.md`](configuration.md) for the full `oauth2` auth field
reference.

---

## Token transformation

Token transformation controls how the client-facing proxy token is mapped to
the upstream credential for HTTP upstreams.

| Strategy | Behavior |
|---|---|
| `static` (default) | Use the upstream's own `auth` config. Client token is validated for proxy access only. |
| `passthrough` | Forward the client's incoming `Authorization` bearer token verbatim to the upstream. |
| `map` | Look up the authenticated user in the token mappings table; inject the mapped upstream token. |
| `header_inject` | Keep the upstream's static auth AND inject the user identity as an extra header. |

**Config example (per-user mapping):**
```json
{
  "upstreams": {
    "my-api": {
      "type": "http",
      "url": "https://api.example.com/mcp",
      "auth": { "type": "none" },
      "token_transform": {
        "strategy": "map",
        "fallback_on_missing_map": "deny"
      }
    }
  }
}
```

**Managing token mappings:** Dashboard → TokenMappings page, or via the
`/admin/api/token-mappings` REST endpoint. Each mapping is a
`(user_id, upstream_name, token)` triple stored encrypted in the database.

---

## Rotating and revoking credentials

### Rotate a PAT
```bash
# From dashboard: Tokens page → Revoke → New Token
# From CLI:
mcpxy-proxy secrets delete <pat-name>
mcpxy-proxy secrets set --generate-pat
```

### Invalidate a session
Sessions expire after `auth.authy.token_ttl_s` seconds. To invalidate a session
immediately, rotate `auth.authy.jwt_secret` — all existing JWTs signed with the
old key will fail verification immediately.

```bash
# Generate new JWT secret and apply:
python -c "import secrets; print(secrets.token_urlsafe(32))"
mcpxy-proxy secrets set JWT_SECRET  # paste the new value
mcpxy-proxy config import <current-config>.json  # triggers hot-reload
```

### Rotate the Fernet secrets key

```bash
# Generate a new Fernet key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Set the new key BEFORE restarting:
export MCPXY_SECRETS_KEY=<new-key>

# After restart, re-enter any secrets that were encrypted with the old key
# (PATs, OAuth tokens, upstream credentials) via the dashboard or CLI.
```

> **Warning:** Rotating the Fernet key invalidates all previously encrypted
> secrets. You must re-enter them after rotation. Export them first if needed.
