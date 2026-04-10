#!/usr/bin/env bash
# MCPxy container entrypoint.
#
# Wraps `mcpxy-proxy` so operators can parameterise the config seed path
# and listen address via env (MCPXY_CONFIG / MCPXY_LISTEN) without
# rebuilding the image. The first positional arg selects the subcommand;
# anything after it is forwarded verbatim to the CLI.
#
# Config model:
#   - The DB at $MCPXY_STATE_DIR/mcpxy.db (default /var/lib/mcpxy/mcpxy.db)
#     is the source of truth.
#   - On the very first start, if MCPXY_CONFIG points at a real file and
#     the DB is empty, the proxy imports it and renames it (or logs a
#     warning if the bind mount is read-only — the import still
#     succeeds).
#   - On subsequent starts the file (if any) is ignored.
set -euo pipefail

MCPXY_CONFIG=${MCPXY_CONFIG:-/etc/mcpxy/config.json}
MCPXY_LISTEN=${MCPXY_LISTEN:-0.0.0.0:8000}

cmd=${1:-serve}
shift || true

case "$cmd" in
    serve)
        # The seed file is optional now: if it isn't there we just don't
        # pass --config at all and let the DB / minimal default kick in.
        serve_args=(--listen "$MCPXY_LISTEN")
        if [[ -f "$MCPXY_CONFIG" ]]; then
            serve_args+=(--config "$MCPXY_CONFIG")
        else
            echo "mcpxy: no seed config at $MCPXY_CONFIG (will use DB or write defaults)" >&2
        fi
        exec mcpxy-proxy serve "${serve_args[@]}" "$@"
        ;;
    *)
        # Anything else (init, catalog, register, discover, config, secrets, ...)
        # is forwarded to the CLI untouched so e.g.
        # `docker compose run --rm mcpxy config show` works as an operator UX.
        exec mcpxy-proxy "$cmd" "$@"
        ;;
esac
