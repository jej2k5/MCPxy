# MCPxy Policy Guide

Policies control what requests MCPxy allows through, at what rate, and what
sensitive data is scrubbed in transit. All policies are configured under the
`policies` key in the config and are **hot-reloaded atomically** — a bad
policy is rejected and the previous version stays active.

For the full field reference, see [`configuration.md`](configuration.md).

---

## Policy model

Policies are organized in two scopes:

- **`policies.global`** — applied to every upstream unless overridden
- **`policies.per_upstream.<name>`** — applied only to the named upstream;
  overrides the global policy for that upstream

```json
{
  "policies": {
    "global": {
      "size": { "max_request_bytes": 1048576 }
    },
    "per_upstream": {
      "search": {
        "rate_limit": { "requests_per_second": 5.0, "burst": 10 }
      }
    }
  }
}
```

**Evaluation order:**
1. Method ACL (global)
2. Method ACL (per-upstream, if present)
3. Rate limit (per-upstream if present, else global)
4. Size cap (per-upstream if present, else global)
5. Request redaction (per-upstream if present, else global)
6. Request forwarded to upstream
7. Response redaction (per-upstream if present, else global)
8. Response returned to client

Within a scope, `deny` always takes precedence over `allow`. A method that
matches a `deny` pattern is rejected even if it also matches an `allow` pattern.

---

## Method ACLs

Method ACLs control which JSON-RPC method names are allowed through MCPxy.
Patterns use `fnmatch` wildcards (`*` matches any string, `?` matches one
character).

### Allow-only (whitelist)

```json
{
  "policies": {
    "per_upstream": {
      "github": {
        "methods": {
          "allow": ["tools/list", "tools/call", "resources/list"]
        }
      }
    }
  }
}
```

Only the listed methods pass through; all others are rejected with a JSON-RPC
`-32601 Method not found` error.

### Deny-only (blacklist)

```json
{
  "policies": {
    "global": {
      "methods": {
        "deny": ["notifications/*", "sampling/*"]
      }
    }
  }
}
```

### Allow + deny (combined)

```json
{
  "policies": {
    "per_upstream": {
      "filesystem": {
        "methods": {
          "allow": ["tools/*", "resources/*"],
          "deny": ["tools/call"]
        }
      }
    }
  }
}
```

`deny` wins: `tools/call` is denied even though `tools/*` would allow it.

### Allow all except specific methods

```json
{
  "methods": {
    "allow": ["*"],
    "deny": ["sampling/*"]
  }
}
```

---

## Rate limits

MCPxy uses a **token bucket** algorithm. The bucket refills at
`requests_per_second` tokens per second, up to a maximum of `burst` tokens.
Each request consumes one token. When the bucket is empty, the request is
rejected with JSON-RPC error `-32000 rate limit exceeded`.

### Scopes

| Scope | Counts separately per |
|---|---|
| `upstream` | Upstream name (all clients share one bucket per upstream) |
| `client_ip` | Client IP address (each client gets its own bucket) |
| `both` | Both upstream and client IP (more restrictive) |

### Examples

**Shared limit across all clients for one upstream:**
```json
{
  "policies": {
    "per_upstream": {
      "brave-search": {
        "rate_limit": {
          "requests_per_second": 1.0,
          "burst": 5,
          "scope": "upstream"
        }
      }
    }
  }
}
```

**Per-client limit (all upstreams):**
```json
{
  "policies": {
    "global": {
      "rate_limit": {
        "requests_per_second": 10.0,
        "burst": 20,
        "scope": "client_ip"
      }
    }
  }
}
```

**Choosing burst:** Set `burst` to the maximum number of requests you want to
allow in a short burst before the steady-state limit kicks in. A burst of 10
with a refill rate of 1 req/s means a client can send 10 requests instantly but
then must wait 1 second between each additional request.

---

## Size caps

Reject requests whose JSON body exceeds a byte limit. Oversized requests are
rejected with JSON-RPC error `-32700 Request too large` before they reach the
upstream.

```json
{
  "policies": {
    "global": {
      "size": {
        "max_request_bytes": 1048576
      }
    }
  }
}
```

`1048576` = 1 MiB. Size caps apply to the raw bytes of the JSON-RPC request
body.

---

## PII/PCI redaction

Redaction scans every string value in JSON-RPC payloads and replaces matches
with a placeholder before they cross a trust boundary.

**Directionality:**
- `redact_request: true` — scrub the request before it reaches the upstream
- `redact_response: true` — scrub the response before it reaches the client

Both default to `true` when the `redaction` block is present.

### Built-in patterns

**PII** (enabled by `"pii": true`):
| Pattern | Matches |
|---|---|
| Email | `user@example.com` |
| US/intl phone | `+1-555-867-5309`, `(555) 867-5309` |
| US Social Security Number | `123-45-6789` |
| IPv4 address | `192.168.1.100` |

**PCI** (enabled by `"pci": true`):
| Pattern | Matches |
|---|---|
| Card PAN | Visa/Mastercard/Amex/Discover card numbers with optional separators |
| CVV/CVC | `CVV: 123`, `cvc2=4321` |
| Card expiry | `Expires: 12/27`, `valid thru 09/2028` |

### Example: full redaction

```json
{
  "policies": {
    "global": {
      "redaction": {
        "pii": true,
        "pci": true,
        "redact_request": true,
        "redact_response": true,
        "replacement": "[REDACTED]"
      }
    }
  }
}
```

### Example: response-only (trust the upstream, protect the client)

```json
{
  "policies": {
    "per_upstream": {
      "trusted-internal": {
        "redaction": {
          "pii": true,
          "pci": false,
          "redact_request": false,
          "redact_response": true
        }
      }
    }
  }
}
```

### Custom patterns

Add arbitrary regex patterns. Patterns are compiled at config-apply time;
invalid regex fails validation and the config is rejected.

```json
{
  "policies": {
    "global": {
      "redaction": {
        "pii": false,
        "pci": false,
        "custom_patterns": {
          "corp_employee_id": "EMP-[0-9]{6}",
          "internal_ticket": "TICKET-[A-Z]{2}-[0-9]+"
        }
      }
    }
  }
}
```

The key (e.g. `"corp_employee_id"`) is used as a label in audit logs; it does
not appear in the replacement text.

---

## Token transformation

See [`auth.md`](auth.md) for the full token transformation guide. Token
transformation is configured per-upstream under `upstreams.<name>.token_transform`
(not under `policies`).

---

## Authoring workflow

### From the dashboard

1. Open the **Policies** page
2. Edit method ACLs, rate limits, or redaction rules in the UI form
3. Click **Apply** — MCPxy validates and applies atomically
4. If validation fails, an error is shown and the previous policy stays active

### From config import

```bash
# Edit your config file locally, then import:
mcpxy-proxy config import updated-config.json
```

### Rollback

If a newly applied policy causes problems:

```bash
mcpxy-proxy config history      # find the previous version number
# Dashboard Config page → History tab → select version → Restore
```

Or from the CLI (export the old version if you saved it):

```bash
mcpxy-proxy config import previous-config.json
```

---

## Example: combined production policy

```json
{
  "policies": {
    "global": {
      "methods": {
        "deny": ["sampling/*", "notifications/cancelled"]
      },
      "size": {
        "max_request_bytes": 524288
      },
      "redaction": {
        "pii": true,
        "pci": true,
        "redact_request": true,
        "redact_response": true
      }
    },
    "per_upstream": {
      "search": {
        "rate_limit": {
          "requests_per_second": 2.0,
          "burst": 5,
          "scope": "client_ip"
        }
      },
      "github": {
        "methods": {
          "allow": ["tools/list", "tools/call", "resources/*", "prompts/*"]
        }
      }
    }
  }
}
```

This config:
- Globally denies `sampling/*` and `notifications/cancelled`
- Caps all requests at 512 KiB
- Scrubs PII and PCI everywhere
- Limits each client to 2 requests/second to the `search` upstream (burst 5)
- Restricts `github` to a specific method allowlist
