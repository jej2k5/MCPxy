# MCPxy Developer Guide

This guide covers setting up a local development environment, running the
frontend and backend, writing tests, and extending MCPxy with custom plugins.

For the architecture overview, see [`architecture.md`](architecture.md).
For contributing workflow, see [`CONTRIBUTING.md`](../CONTRIBUTING.md).

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | ≥ 3.11 | Backend runtime and tests |
| Node.js | ≥ 20 | Frontend build (`npm run build`) |
| npm | ≥ 10 | Frontend dependencies |
| `uv` / `uvx` | optional | Running catalog entries locally |
| git | any | Source control |

---

## Clone and install

```bash
git clone https://github.com/jej2k5/mcpxy && cd mcpxy
python -m venv .venv
source .venv/bin/activate

# Install the proxy + dev dependencies (pytest, ruff, mypy):
pip install -e ".[dev]"

# Optional: Postgres or MySQL driver for testing against those backends:
pip install -e ".[dev,postgres]"
```

**Note on the `authy` dependency:**

`authy` is pulled from a private Git source:
```
authy @ git+https://github.com/jej2k5/authy#subdirectory=python
```

`pip install -e .` fetches it automatically. If you're in an environment
without internet access, mirror or vendor `authy` and update `pyproject.toml`
accordingly.

---

## Running the proxy in dev

```bash
# Quickest start — plain HTTP, auto-seed config, sqlite state at ~/.mcpxy:
mcpxy-proxy serve --no-tls

# With a seed config:
mcpxy-proxy serve --no-tls --config config.example.json

# Bind to a network address (e.g. for testing from another machine):
mcpxy-proxy serve --no-tls --listen 0.0.0.0:8000
```

Open `http://127.0.0.1:8000/admin`. The Onboarding wizard runs on first start.

**State directory in dev:** Defaults to `~/.mcpxy`. Override with
`MCPXY_STATE_DIR=/tmp/mcpxy-dev` to keep dev state separate from production.

**Hot-reload:** Config changes via the dashboard or `config import` take effect
without a restart. The only exception is the `tls` block.

---

## Frontend dev loop

The dashboard is a React/TypeScript SPA built with Vite. During development
you can run the frontend dev server alongside the backend to get HMR.

```bash
# Start the backend (must be running first):
mcpxy-proxy serve --no-tls --listen 127.0.0.1:8000

# In a separate terminal:
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/admin/api/*`, `/mcp/*`, and `/health` to the
backend at `127.0.0.1:8000` (see `frontend/vite.config.ts`). Open the URL
Vite prints (usually `http://localhost:5173/admin`).

### Building and committing the dist

The pre-built dashboard is committed to the repo as `src/mcpxy_proxy/web/dist/`
and bundled into the Python package. After frontend changes, rebuild and commit:

```bash
cd frontend
npm run build         # writes to ../src/mcpxy_proxy/web/dist/
cd ..
git add src/mcpxy_proxy/web/dist/
git commit -m "Build: update dashboard"
```

The Dockerfile runs `npm run build` during the image build (Stage 1), so the
committed dist is only needed for pip installs, not for Docker.

---

## Test suite

```bash
# Run the full suite:
pytest

# Faster: run only the most targeted tests for a given area:
pytest tests/test_routing_precedence.py tests/test_admin_auth.py
pytest tests/test_atomic_apply_rollback.py tests/test_hot_reload.py
pytest tests/test_redaction.py
pytest tests/test_authn_pats.py tests/test_config_authy.py tests/test_oauth_endpoints.py
pytest tests/test_plugin_discovery.py
pytest tests/test_telemetry_queue_flush.py
pytest tests/test_stdio_restart.py tests/test_overload_handling.py
pytest tests/test_bridge_shutdown_sync.py tests/test_server_streaming.py
```

Tests use `pytest-asyncio` for async test functions. The `pythonpath` is set
to `src/` in `pyproject.toml` so imports work without installation.

**Parallel test run:**
```bash
pip install pytest-xdist
pytest -n auto
```

---

## Linters and type checks

```bash
# Lint and auto-fix:
ruff check src/ tests/ --fix
ruff format src/ tests/

# Type check:
mypy src/
```

CI runs `ruff check` and `mypy` on every PR. Ensure both pass before opening
a pull request.

---

## Writing an upstream transport

Upstream transports are plugins loaded via the `mcpxy_proxy.upstreams` entry
point group. Built-in transports: `stdio` and `http`.

### Step 1: implement the interface

Create a class inheriting from `UpstreamTransport` (in
`src/mcpxy_proxy/proxy/base.py`). Required methods:

```python
class MyTransport(UpstreamTransport):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def request(self, message: dict) -> dict: ...
    async def notify(self, message: dict) -> None: ...
```

### Step 2: define a config model

Add a Pydantic config model with `type: Literal["my_transport"]` and extend
the `UpstreamConfig` union in `config.py`. Your config model is validated
on every config apply.

### Step 3: register the entry point

In your package's `pyproject.toml`:

```toml
[project.entry-points."mcpxy_proxy.upstreams"]
my_transport = "mypackage.transport:MyTransport"
```

After `pip install -e .`, MCPxy's plugin registry discovers the transport
via `importlib.metadata.entry_points`.

### Step 4: use it in config

```json
{
  "upstreams": {
    "my-server": {
      "type": "my_transport",
      "my_field": "value"
    }
  }
}
```

---

## Writing a telemetry sink

Telemetry sinks receive batches of request metadata events (never request
bodies). They are loaded via the `mcpxy_proxy.telemetry_sinks` entry point
group.

### Step 1: implement the interface

```python
from mcpxy_proxy.telemetry.pipeline import TelemetrySink, TelemetryEvent

class MyTelemetrySink(TelemetrySink):
    async def send(self, events: list[TelemetryEvent]) -> None:
        # write to your observability backend
        ...
```

### Step 2: register the entry point

```toml
[project.entry-points."mcpxy_proxy.telemetry_sinks"]
my_sink = "mypackage.sink:MyTelemetrySink"
```

### Step 3: configure it

```json
{
  "telemetry": {
    "enabled": true,
    "sink": "my_sink"
  }
}
```

---

## Database schema migrations

MCPxy uses SQLAlchemy Core (no ORM). The schema is defined in
`src/mcpxy_proxy/storage/schema.py`.

**Adding a table or column:**

1. Add the new `Table` definition or `Column` to `schema.py`
2. In `bootstrap.py`, add the new table to the `create_tables()` function
   using `CREATE TABLE IF NOT EXISTS` — this is safe to run on existing databases
3. For column additions to existing tables, add an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
   call in a `migrate_schema()` function (also in `bootstrap.py`)
4. Bump the schema version constant in `schema.py`
5. Add a test to verify the migration runs cleanly against an existing schema

MCPxy runs schema bootstrap on every start, so migrations are applied
automatically without a separate migration step.

---

## Releasing

1. Update `version` in `pyproject.toml` following semver
2. Update `CHANGELOG.md` — move items from `[Unreleased]` to a new version
   heading with today's date
3. Commit with `chore: bump version to X.Y.Z`
4. Tag: `git tag -a vX.Y.Z -m "vX.Y.Z"`
5. Build sdist and wheel:
   ```bash
   # Build the frontend first:
   cd frontend && npm run build && cd ..
   pip install build
   python -m build
   ```
6. Publish to PyPI:
   ```bash
   pip install twine
   twine upload dist/mcpxy_proxy-X.Y.Z*
   ```
7. Push the tag: `git push origin vX.Y.Z`
