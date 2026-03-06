# MCPy Proxy Design

## Source layout

- Runtime/server entrypoint: `src/mcp_proxy/server.py`
- Plugin registry: `src/mcp_proxy/plugins/registry.py`
- Admin web template: `src/mcp_proxy/web/templates/admin.html`
- Admin static assets: `src/mcp_proxy/web/static/admin.css`, `src/mcp_proxy/web/static/admin.js`

## Routing and control plane

Request routing precedence is strict and deterministic:
1. Path (`/mcp/{name}`)
2. Header (`X-MCP-Upstream`)
3. In-band parameter (`params.mcp_upstream`)
4. `default_upstream`

Admin controls are exposed through:
- MCP methods at `/mcp/admin`
- UI at `/admin`
- UI helper APIs at `/admin/api/*`

## Reliability rules

- Runtime config changes are applied atomically with rollback on failure.
- Stdio upstreams can restart after process exit.
- Telemetry is non-blocking with bounded queue and drop policy.
- Overload handling returns structured JSON-RPC errors instead of blocking.

## Test mapping

Behavior-specific tests live in `tests/`:
- `test_routing_precedence.py`
- `test_admin_auth.py`
- `test_atomic_apply_rollback.py`
- `test_redaction.py`
- `test_plugin_discovery.py`
- `test_telemetry_queue_flush.py`
- `test_stdio_restart.py`
- `test_overload_handling.py`
- `test_hot_reload.py`
- `test_admin_ui_auth.py`
