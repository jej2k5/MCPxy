#!/usr/bin/env bash
# MCPy container entrypoint.
#
# Wraps `mcp-proxy` so operators can parameterise the config path and
# listen address via env (MCPY_CONFIG / MCPY_LISTEN) without rebuilding
# the image. The first positional arg selects the subcommand; anything
# after it is forwarded verbatim to the CLI.
set -euo pipefail

MCPY_CONFIG=${MCPY_CONFIG:-/etc/mcpy/config.json}
MCPY_LISTEN=${MCPY_LISTEN:-0.0.0.0:8000}

cmd=${1:-serve}
shift || true

case "$cmd" in
    serve)
        if [[ ! -f "$MCPY_CONFIG" ]]; then
            echo "mcpy: config file not found at $MCPY_CONFIG" >&2
            echo "mcpy: mount one at /etc/mcpy/config.json or set MCPY_CONFIG" >&2
            exit 2
        fi
        exec mcp-proxy serve \
            --config "$MCPY_CONFIG" \
            --listen "$MCPY_LISTEN" \
            "$@"
        ;;
    *)
        # Anything else (init, catalog, register, discover, ...) is forwarded
        # to the CLI untouched so `docker compose run --rm mcpy catalog list`
        # works as an operator UX.
        exec mcp-proxy "$cmd" "$@"
        ;;
esac
