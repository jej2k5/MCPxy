# Contributing

See [`docs/development.md`](docs/development.md) for the full development
environment setup, frontend build loop, and plugin authoring guide.

## Development Setup

1. Create and activate a virtual environment.
2. Install dependencies:
   ```bash
   pip install -e .[dev]
   ```
3. Run the proxy locally:
   ```bash
   mcpxy-proxy serve --config config.example.json
   ```

## How to Run Tests

```bash
pytest
```


## Required Paths

Keep these paths stable when editing architecture and UI assets:
- `src/mcpxy_proxy/server.py` — FastAPI app factory and all HTTP handlers
- `src/mcpxy_proxy/config.py` — Pydantic config models (schema changes affect all consumers)
- `src/mcpxy_proxy/proxy/` — upstream transports and request bridge
- `src/mcpxy_proxy/policy/` — policy engine and redaction
- `src/mcpxy_proxy/authn/` — admin authentication (authy integration)
- `src/mcpxy_proxy/storage/schema.py` — DB schema v2; migrations are additive
- `src/mcpxy_proxy/plugins/registry.py` — plugin discovery
- `frontend/src/` — Vite/React dashboard source; build output (`npm run build`) goes to `src/mcpxy_proxy/web/dist/` and must be committed alongside source changes
- `docs/architecture.md` — primary design reference

## Targeted Test Commands

Run behavior tests explicitly:
```bash
pytest tests/test_routing_precedence.py tests/test_admin_auth.py tests/test_atomic_apply_rollback.py tests/test_redaction.py tests/test_plugin_discovery.py tests/test_telemetry_queue_flush.py tests/test_stdio_restart.py tests/test_overload_handling.py tests/test_hot_reload.py tests/test_admin_ui_auth.py
```

Run the full suite before opening a PR:
```bash
pytest
```

## Code Style Guidelines

- Python 3.11+.
- Type hints required across the codebase.
- Docstrings required for public classes/functions.
- Keep modules cohesive and testable.
- Prefer explicit error handling and JSON-RPC compliant errors.

## Pull Request Workflow

1. Fork and branch from `main`.
2. Add tests for behavior changes.
3. Run test suite and linters locally.
4. Submit PR with clear summary and rationale.
5. Address reviewer feedback promptly.

## Issue Reporting Guidelines

When opening issues, include:
- Expected behavior
- Actual behavior
- Reproduction steps
- Configuration snippet (with secrets removed)
- Logs or stack traces

## Feature Proposal Process

Open a discussion or issue with:
- Problem statement
- Proposed API/UX
- Backward compatibility notes
- Testing plan
