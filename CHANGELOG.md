# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Project renamed MCPy → MCPxy.** The PyPI package is now `mcpxy-proxy`,
  the CLI command is `mcpxy-proxy`, all environment variables use the
  `MCPXY_` prefix, and the default state directory is `~/.mcpxy` (host) /
  `/var/lib/mcpxy` (container). The old `MCPy`, `mcp-proxy`, and `MCP_PROXY_*`
  names have been removed with no backward-compatibility aliases.

### Added
- **Multi-provider admin authentication** via the authy library (local
  username/password, Google OAuth, Microsoft 365/Azure AD, generic OIDC
  SSO, SAML). Three credential types are supported in parallel: PAT
  (Personal Access Token, prefix `pat_`), session cookie
  (`mcpxy_session`), and JWT bearer. Invite-based user registration and a
  Users page in the dashboard are included. Configure via the `auth.authy`
  block in config; see `docs/auth.md`.
- **PII/PCI redaction policy** with built-in patterns (email, US/intl
  phone, SSN, IPv4; card PAN, CVV, expiry) and support for arbitrary
  custom regex patterns. Applied independently on the request path (to
  upstreams) and the response path (to clients) via `redact_request` /
  `redact_response` toggles. Configure under `policies.*.redaction`.
- **Token transformation policy** for HTTP upstreams — maps the
  client-facing bearer token to a per-user upstream credential at request
  time. Strategies: `static` (use upstream's own auth, default),
  `passthrough` (forward the incoming token verbatim), `map` (look up a
  stored per-user mapping), `header_inject` (inject user identity as a
  header alongside static upstream auth). Token mappings are stored
  encrypted in the DB and managed from the TokenMappings dashboard page.
- **Postgres and MySQL support** as optional database backends.
  Install `mcpxy-proxy[postgres]` (psycopg2) or `mcpxy-proxy[mysql]`
  (PyMySQL) and set `MCPXY_DB_URL` or configure via the onboarding wizard.
  SQLite remains the default and requires no extra dependencies.
- **OAuth 2.1 upstream client** — full implementation of RFC 8414
  (discovery), RFC 7591 (dynamic client registration), RFC 7636
  (authorization code + PKCE), and token refresh with Fernet-encrypted
  storage. Used for upstream HTTP MCP servers that require OAuth 2.0.
  Multiple bug fixes to the callback flow, session detection, catalog
  loading during onboarding, and federated JWT decoding.
- **React/Vite admin dashboard** with 14 pages: Onboarding, Overview,
  Routes, Traffic, Policies, Browse, Import, Connect, Logs, Config,
  Tokens, TokenMappings, Users, Graph. Shipped pre-built in the Python
  package; `pip install` includes a working UI with no Node dependency at
  runtime.
- **Bundled catalog** of 14 well-known MCP servers (filesystem, git,
  github, gitlab, memory, postgres, sqlite, brave-search, fetch,
  puppeteer, slack, time, everart, sentry). One-click install from the
  Browse page; variable prompts for secrets and paths.
- **Client import** — scans Claude Desktop, Claude Code, Cursor,
  Windsurf, and Continue for MCP servers you already have configured and
  brings them into MCPxy in one click from the Import page.
- **Self-signed TLS auto-generation** for `localhost`, `127.0.0.1`, and
  `::1` on first run. Cert is cached in `<state-dir>/tls/` and reused on
  subsequent starts. Production cert/key paths and mTLS for upstream HTTPS
  connections are also supported.
- **DB-backed hot-reloadable config** with atomic apply and rollback on
  validation failure. Config history is preserved for audit and rollback.
  TLS listener settings are the only changes that require a restart.

## [0.1.0] - 2026-03-05

### Added
- Initial release with multi-upstream MCP proxy.
- Routing precedence via path, header, in-band params, and default upstream.
- FastAPI HTTP interface with JSON and NDJSON support.
- Internal admin MCP interface for runtime config, health, and telemetry.
- Plugin system for upstream transports and telemetry sinks.
- Telemetry pipeline with bounded queue and retrying HTTP sink.
- Test suite for routing, auth, config application, plugin loading, telemetry, restart, and backpressure.
