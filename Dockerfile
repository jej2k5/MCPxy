# syntax=docker/dockerfile:1.6
#
# MCPxy container image.
#
# Stage 1 builds the React dashboard with Node and emits static assets into
# the Python package tree (vite.config.ts writes to ../src/mcpxy_proxy/web/dist).
# Stage 2 is an ubuntu:24.04 runtime that installs the proxy package, the
# built dashboard, plus `node`/`npm`/`uv` so stdio upstreams that shell out
# to `npx`/`uvx` (the vast majority of the bundled catalog) work out of the
# box.
#
# The desktop install helpers (`mcpxy-proxy install ...`) are intentionally
# *not* run from the container: they write to host client config files
# (Claude Desktop, Cursor, Continue, ...) and must execute on the host. The
# container exposes the proxy on :8000 so host-side installers can wire
# clients to it over HTTP.

# ---------------------------------------------------------------------------
# Stage 1 — build the dashboard SPA
# ---------------------------------------------------------------------------
FROM node:20-alpine AS frontend

WORKDIR /build/frontend

# Install JS deps (including devDependencies, which hold vite and the type
# checker). `npm install` is used rather than `npm ci` because recent npm
# releases have a known "Exit handler never called!" bug on some lockfile
# shapes that leaves node_modules half-populated.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm install --no-audit --no-fund --include=dev

# Bring in the rest of the frontend tree and build.  We invoke vite
# directly (rather than `npm run build`) so the in-package `tsc --noEmit`
# type check is not a hard dependency of the image build — type checking
# is a CI concern, not a deployment concern.  vite.config.ts writes the
# output to /build/src/mcpxy_proxy/web/dist, which is where stage 2 looks
# for it.
COPY frontend/ ./
RUN npx vite build


# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
# We use ubuntu:24.04 (Python 3.12 + Node 18 in the default repos) rather
# than python:3.11-slim because we need git, nodejs, npm, and curl
# alongside Python, and Ubuntu's package set gives us all of them from a
# single trusted mirror. Python 3.12 still satisfies `requires-python >=
# 3.11` in pyproject.toml.
FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/mcpxy/.venv/bin:$PATH" \
    MCPXY_CONFIG=/etc/mcpxy/config.json \
    MCPXY_LISTEN=0.0.0.0:8000

# System deps:
#   ca-certificates + curl    — TLS trust store + /health curl in the
#                               HEALTHCHECK
#   git                       — stdio upstreams that clone repos at runtime
#   python3 + python3-venv    — interpreter + venv support (Ubuntu 24.04
#                               enforces PEP 668, so we install into a venv
#                               instead of --break-system-packages)
#   nodejs + npm              — `npx`-based catalog upstreams
# build-essential is intentionally omitted: all runtime wheels are pure
# Python or arrive precompiled.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        nodejs \
        npm \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Dedicated venv at /opt/mcpxy/.venv. `uv` drives `uvx`, which a number of
# catalog entries rely on, so we seed it into the same venv.
RUN python3 -m venv /opt/mcpxy/.venv \
    && /opt/mcpxy/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/mcpxy/.venv/bin/pip install --no-cache-dir uv

WORKDIR /opt/mcpxy

# Copy only what `pip install .` needs. README.md is referenced by
# pyproject.toml (`readme = "README.md"`) so the build fails without it.
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Pull the built dashboard from the frontend stage into the package tree so
# setuptools' `package-data` glob (`web/dist/**/*`) picks it up.
COPY --from=frontend /build/src/mcpxy_proxy/web/dist/ ./src/mcpxy_proxy/web/dist/

RUN /opt/mcpxy/.venv/bin/pip install --no-cache-dir ".[postgres,mysql]"

# The config path and listen address are parameterised via env so operators
# can override without rebuilding; the entrypoint script below reads them.
COPY deploy/docker/entrypoint.sh /usr/local/bin/mcpxy-entrypoint
RUN chmod 0755 /usr/local/bin/mcpxy-entrypoint

# Non-root runtime user with a writable data dir for runtime artefacts
# (file-drop watcher directory, telemetry buffers, etc).  Ubuntu 24.04
# ships with an unused `ubuntu` user at uid 1000; we use `--no-log-init`
# + `--system` to create a fresh service account at a different uid.
RUN groupadd --system mcpxy \
    && useradd --system --no-log-init --gid mcpxy --home /var/lib/mcpxy \
               --shell /usr/sbin/nologin mcpxy \
    && mkdir -p /etc/mcpxy /var/lib/mcpxy \
    && chown -R mcpxy:mcpxy /var/lib/mcpxy /etc/mcpxy

USER mcpxy
WORKDIR /var/lib/mcpxy

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/mcpxy-entrypoint"]
CMD ["serve"]
