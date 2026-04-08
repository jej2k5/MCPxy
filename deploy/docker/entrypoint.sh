#!/usr/bin/env bash
# MCPy container entrypoint.
#
# Wraps `mcp-proxy` so operators can parameterise the config seed path
# and listen address via env (MCPY_CONFIG / MCPY_LISTEN) without
# rebuilding the image. The first positional arg selects the subcommand;
# anything after it is forwarded verbatim to the CLI.
#
# Config model:
#   - The DB at $MCPY_STATE_DIR/mcpy.db (default /var/lib/mcpy/mcpy.db)
#     is the source of truth.
#   - On the very first start, if MCPY_CONFIG points at a real file and
#     the DB is empty, the proxy imports it and renames it (or logs a
#     warning if the bind mount is read-only — the import still
#     succeeds).
#   - On subsequent starts the file (if any) is ignored.
set -euo pipefail

MCPY_CONFIG=${MCPY_CONFIG:-/etc/mcpy/config.json}
MCPY_LISTEN=${MCPY_LISTEN:-0.0.0.0:8000}

cmd=${1:-serve}
shift || true

case "$cmd" in
    serve)
        # The seed file is optional now: if it isn't there we just don't
        # pass --config at all and let the DB / minimal default kick in.
        serve_args=(--listen "$MCPY_LISTEN")
        if [[ -f "$MCPY_CONFIG" ]]; then
            serve_args+=(--config "$MCPY_CONFIG")
        else
            echo "mcpy: no seed config at $MCPY_CONFIG (will use DB or write defaults)" >&2
        fi
        exec mcp-proxy serve "${serve_args[@]}" "$@"
        ;;
    *)
        # Anything else (init, catalog, register, discover, config, secrets, ...)
        # is forwarded to the CLI untouched so e.g.
        # `docker compose run --rm mcpy config show` works as an operator UX.
        exec mcp-proxy "$cmd" "$@"
        ;;
esac
