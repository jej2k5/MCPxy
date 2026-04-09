"""CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn

from mcp_proxy.auth.oauth import OAuthManager
from mcp_proxy.config import AppConfig, load_config, resolve_admin_token
from mcp_proxy.discovery.catalog import load_catalog
from mcp_proxy.discovery.importers import IMPORTERS, discover_all, get_importer
from mcp_proxy.install.clients import InstallOptions, get_adapter, list_clients
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.plugins.registry import PluginRegistry
from mcp_proxy.proxy.manager import UpstreamManager
from mcp_proxy.secrets import SecretsManager, load_fernet
from mcp_proxy.server import AppState, create_app
from mcp_proxy.stdio_adapter import run_stdio_adapter
from mcp_proxy.storage.bootstrap import BootstrapError, load_bootstrap
from mcp_proxy.storage.config_store import ConfigStore, open_store
from mcp_proxy.storage.db import _default_state_dir, sanitize_url
from mcp_proxy.telemetry.pipeline import TelemetryPipeline

logger = logging.getLogger(__name__)


_DEFAULT_BOOTSTRAP_CONFIG: dict[str, Any] = {
    "default_upstream": None,
    "auth": {"token_env": "MCP_PROXY_TOKEN"},
    "admin": {
        "mount_name": "__admin__",
        "enabled": True,
        "require_token": True,
        "allowed_clients": [],
    },
    "telemetry": {"enabled": True, "sink": "noop"},
    "upstreams": {},
}


def parse_listen(value: str) -> tuple[str, int]:
    """Parse host:port listen value."""
    host, sep, raw_port = value.rpartition(":")
    if not sep or not host or not raw_port:
        raise argparse.ArgumentTypeError("--listen must be in host:port format")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("listen port must be an integer") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("listen port must be between 1 and 65535")
    return host, port


def _bootstrap_config_payload(
    store: ConfigStore,
    seed_path: str | None,
) -> tuple[dict[str, Any], str]:
    """Resolve the initial config payload from the DB or a seed file.

    Returns ``(payload, source_label)``. ``source_label`` is one of:

    - ``"db"``                 — DB already had an active config; seed
                                  path (if any) is ignored on subsequent
                                  starts.
    - ``"seed:<path>"``        — DB was empty and the seed file existed,
                                  so we imported it. The seed file is then
                                  renamed to ``<path>.migrated`` so nobody
                                  thinks they can still edit it.
    - ``"default"``            — neither the DB nor a seed file is
                                  available; we wrote a minimal default
                                  so the proxy can start. Operators are
                                  expected to populate it via the admin
                                  API or ``mcp-proxy register`` next.
    """
    existing = store.get_active_config()
    if existing is not None:
        if seed_path:
            logger.info(
                "config: DB already populated (version %d); ignoring --config %s",
                store.active_version(),
                seed_path,
            )
        return existing, "db"

    if seed_path and Path(seed_path).is_file():
        raw = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        store.save_active_config(raw, source=f"bootstrap:{seed_path}")
        try:
            migrated_path = Path(seed_path).with_suffix(
                Path(seed_path).suffix + ".migrated"
            )
            Path(seed_path).rename(migrated_path)
            logger.info(
                "config: imported %s into DB (version %d); renamed file to %s",
                seed_path,
                store.active_version(),
                migrated_path,
            )
        except OSError as exc:
            logger.warning(
                "config: imported %s into DB but could not rename file: %s",
                seed_path,
                exc,
            )
        return store.get_active_config() or raw, f"seed:{seed_path}"

    # Last resort: write a minimal default so the proxy can start. The
    # operator must add upstreams via the admin API. We also deliberately
    # leave admin.require_token=False and no auth.token for the default
    # bootstrap so the onboarding wizard at /admin/onboarding is reachable
    # without any prior configuration — the wizard itself writes a real
    # token via /admin/api/onboarding/set_admin_token before finishing.
    payload = json.loads(json.dumps(_DEFAULT_BOOTSTRAP_CONFIG))
    payload["admin"]["require_token"] = False
    payload["auth"] = {"token": None, "token_env": None}
    store.save_active_config(payload, source="bootstrap:default")
    logger.warning(
        "config: DB empty and no --config seed provided; wrote a minimal "
        "default config (version %d) with admin auth DISABLED. Visit "
        "/admin/onboarding to set the admin token and complete first-run "
        "setup.",
        store.active_version(),
    )
    return payload, "default"


def build_state(config_path: str | None) -> AppState:
    """Build app state from the persistent ConfigStore.

    On the first run after upgrading from a file-based deployment we
    auto-import ``config_path`` and ``<state_dir>/secrets.json`` into
    the DB and then ignore them on subsequent starts. The Fernet key
    file (or ``MCPY_SECRETS_KEY``) is unchanged — see ``secrets.py``
    for the rationale.
    """
    state_dir_override = os.getenv("MCPY_STATE_DIR")
    state_dir = Path(state_dir_override) if state_dir_override else _default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)

    # Load the Fernet cipher first; both ConfigStore and SecretsManager
    # need it, and we want exactly one cipher instance shared across them.
    fernet = load_fernet(state_dir)
    # Resolve the database URL the *same way* at every entry point:
    #   1. MCPY_DB_URL env var (operator override in container deploys)
    #   2. <state_dir>/bootstrap.json db_url (written by the onboarding wizard)
    #   3. sqlite:///<state_dir>/mcpy.db default
    # We pre-compute the source label here so the startup log line and
    # later debugging can tell the three apart; the actual precedence
    # lives in ``resolve_database_url`` which is what ``open_store``
    # invokes internally.
    db_url_env = os.getenv("MCPY_DB_URL")
    bootstrap_cfg = None
    if not db_url_env:
        try:
            bootstrap_cfg = load_bootstrap(state_dir)
        except BootstrapError as exc:
            # Bail out loudly rather than silently dropping back to the
            # SQLite default — losing a Postgres URL the operator typed
            # into the wizard would be worse than failing to start.
            logger.error("bootstrap: %s", exc)
            raise
    if db_url_env:
        db_url_source = "env:MCPY_DB_URL"
        effective_db_url: str | None = db_url_env
    elif bootstrap_cfg is not None and bootstrap_cfg.db_url:
        db_url_source = "bootstrap.json"
        effective_db_url = bootstrap_cfg.db_url
    else:
        db_url_source = "default"
        effective_db_url = None  # ``open_store`` will build the SQLite default
    store = open_store(effective_db_url, fernet=fernet, state_dir=state_dir)
    # Log the masked URL (never the raw one — it may contain a password
    # the operator pasted into the wizard form).
    resolved_url = store.engine.url.render_as_string(hide_password=True)
    logger.info(
        "storage: opened database from %s (%s)",
        db_url_source,
        sanitize_url(str(resolved_url)),
    )

    raw_config, source_label = _bootstrap_config_payload(store, config_path)

    # SecretsManager wraps the same store so secrets writes from the
    # admin API and OAuth flows land in one DB, one Fernet, one cache.
    secrets_manager = SecretsManager(
        state_dir=state_dir, config_store=store
    )
    config = AppConfig.model_validate(_expand_for_bootstrap(raw_config, secrets_manager))

    # Seed the onboarding row whenever the proxy would otherwise come up
    # with no resolvable admin token. That covers three bootstrap paths:
    #
    #   1. Default bootstrap — empty DB, no seed file. ``_bootstrap_config_payload``
    #      wrote a minimal config with ``auth.token=None`` and
    #      ``token_env=None`` so the wizard is reachable.
    #   2. Seed-config bootstrap where the operator forgot the token —
    #      common on Docker deploys that ship ``deploy/docker/config.json``
    #      (which references ``MCP_PROXY_TOKEN``) but where the env var
    #      was never set. Without this branch the onboarding row never
    #      exists, the frontend routes to LoginGate, and the fail-closed
    #      middleware returns 503 for every admin API call — leaving the
    #      operator stuck on a token prompt they can't satisfy.
    #   3. Future footguns where a config rollback or manual edit leaves
    #      the proxy with no resolvable bearer.
    #
    # Uses the same ``resolve_admin_token`` helper as the fail-closed
    # middleware (``server.py``) so the two stay in lockstep.
    # ``ensure_onboarding_row`` is idempotent — a no-op on subsequent
    # starts once a row already exists.
    if resolve_admin_token(config.auth) is None:
        store.ensure_onboarding_row()

    registry = PluginRegistry()
    registry.load_entry_points()

    # OAuthManager persists its own state via the shared secrets store.
    # It must exist before UpstreamManager so HTTP transports with
    # oauth2 auth can reach it through the settings side-channel.
    oauth_manager = OAuthManager(secrets=secrets_manager)
    manager = UpstreamManager(config.upstreams, registry, oauth_manager=oauth_manager)
    bridge = ProxyBridge(manager)

    sink_name = config.telemetry.sink
    sink_cls = registry.validate_telemetry_sink_type(sink_name)
    sink = sink_cls() if sink_name == "noop" else sink_cls(config.telemetry.model_dump())

    telemetry = TelemetryPipeline(
        sink=sink,
        queue_max=config.telemetry.queue_max,
        drop_policy=config.telemetry.drop_policy,
        batch_size=config.telemetry.batch_size,
        flush_interval_ms=config.telemetry.flush_interval_ms,
    )
    state = AppState(
        config,
        raw_config,
        manager,
        bridge,
        telemetry,
        registry=registry,
        config_path=config_path if source_label.startswith("seed:") else None,
        secrets_manager=secrets_manager,
        oauth_manager=oauth_manager,
        config_store=store,
    )
    state.bootstrap_source = source_label  # type: ignore[attr-defined]
    return state


def _expand_for_bootstrap(
    raw_config: dict[str, Any], secrets_manager: SecretsManager
) -> dict[str, Any]:
    """Apply ${env:FOO} + ${secret:NAME} expansion at bootstrap time.

    Mirrors what ``load_config`` does for file-based loads, but takes a
    pre-parsed dict (the DB column already holds parsed JSON) so we
    don't double-decode. Returns a fresh dict so the caller can keep the
    original around as ``raw_config`` for the runtime apply path.
    """
    from mcp_proxy.config import _apply_expansions
    from copy import deepcopy

    return _apply_expansions(deepcopy(raw_config), secrets=secrets_manager.get)


# ---------------------------------------------------------------------------
# `serve`
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    logging.getLogger().setLevel(args.log_level.upper())
    state = build_state(args.config)
    app = create_app(state, health_path=args.health_path, request_timeout_s=args.request_timeout)
    host, port = args.listen
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=args.log_level,
        timeout_keep_alive=args.idle_timeout,
        backlog=args.max_queue,
        reload=args.reload,
        # Uvicorn's Proxy-Headers middleware defaults to trusting
        # ``X-Forwarded-For`` / ``X-Forwarded-Proto`` whenever the
        # immediate TCP peer is on its default allow-list (``127.0.0.1``).
        # On Docker Desktop for Mac the published-port peer is 127.0.0.1
        # inside the container, so Uvicorn was silently rewriting
        # ``request.client.host`` from any ``X-Forwarded-For`` header a
        # client cared to send — including fake ones from browser
        # extensions or upstream privacy proxies — and that spoofed IP
        # propagated into ``admin.allowed_clients``, the onboarding
        # loopback allowlist, policy rate-limit attribution, and the
        # traffic recorder. Zero code in MCPy reads forwarded headers
        # today, so disabling the middleware costs nothing and removes
        # the footgun. Proper trusted-proxy support (opt-in via
        # ``AdminConfig.trusted_proxies``) is tracked as a follow-up.
        proxy_headers=False,
    )
    return 0


# ---------------------------------------------------------------------------
# `init`
# ---------------------------------------------------------------------------


def _starter_config(upstream_specs: list[str]) -> dict[str, Any]:
    """Build a starter config dict from CLI --upstream specs of the form
    NAME=stdio:CMD ARG ARG  or  NAME=http:URL.
    """
    upstreams: dict[str, dict[str, Any]] = {}
    for spec in upstream_specs or []:
        if "=" not in spec:
            raise argparse.ArgumentTypeError(
                f"--upstream must be NAME=stdio:CMD or NAME=http:URL, got {spec!r}"
            )
        name, _, body = spec.partition("=")
        if body.startswith("stdio:"):
            parts = body[len("stdio:"):].split()
            if not parts:
                raise argparse.ArgumentTypeError(f"--upstream {name!r}: missing stdio command")
            upstreams[name] = {"type": "stdio", "command": parts[0], "args": parts[1:]}
        elif body.startswith("http:") or body.startswith("https:"):
            upstreams[name] = {"type": "http", "url": body}
        else:
            raise argparse.ArgumentTypeError(
                f"--upstream {name!r}: unknown transport (use stdio: or http:)"
            )

    return {
        "default_upstream": next(iter(upstreams), None),
        "auth": {"token_env": "MCP_PROXY_TOKEN"},
        "admin": {
            "mount_name": "__admin__",
            "enabled": True,
            "require_token": True,
            "allowed_clients": ["127.0.0.1"],
        },
        "telemetry": {"enabled": True, "sink": "noop"},
        "upstreams": upstreams,
    }


def cmd_init(args: argparse.Namespace) -> int:
    out_path = Path(args.output).expanduser()
    if out_path.exists() and not args.force:
        print(f"refusing to overwrite existing {out_path} (use --force)", file=sys.stderr)
        return 2
    cfg = _starter_config(args.upstream or [])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


# ---------------------------------------------------------------------------
# `install`
# ---------------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> int:
    try:
        adapter = get_adapter(args.client)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    opts = InstallOptions(
        name=args.name,
        url=args.url,
        token_env=args.token_env,
        upstream=args.upstream,
        proxy_command=args.proxy_command,
    )

    if not adapter.supports_auto_install():
        # Copy-paste only.
        snippet = adapter.format_entry(opts)
        print(
            "This client does not support automatic installation. "
            "Paste the following entry into the client's connector configuration:"
        )
        print(json.dumps(snippet, indent=2))
        return 0

    config_path = adapter.resolve_config_path(args.config_path)
    if config_path is None:
        print(f"could not determine config path for client {args.client!r}", file=sys.stderr)
        return 2

    existing: dict[str, Any] | None = None
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            print(f"existing config at {config_path} is not valid JSON: {exc}", file=sys.stderr)
            return 2

    merged = adapter.merge(existing, opts)
    diff = adapter.diff(existing, merged)

    if args.dry_run:
        print(f"# would write to {config_path}")
        print(diff or "(no changes)")
        return 0

    backup = adapter.backup(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if backup:
        print(f"wrote {config_path} (backup: {backup})")
    else:
        print(f"wrote {config_path}")
    return 0


# ---------------------------------------------------------------------------
# `stdio` (adapter mode for stdio-only clients)
# ---------------------------------------------------------------------------


def cmd_stdio(args: argparse.Namespace) -> int:
    return asyncio.run(
        run_stdio_adapter(
            url=args.connect,
            token_env=args.token_env,
            upstream=args.upstream,
            mount_path=args.mount_path,
        )
    )


# ---------------------------------------------------------------------------
# Remote admin helpers (used by register/import/catalog subcommands)
# ---------------------------------------------------------------------------


def _remote_headers(token_env: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    env_name = token_env or "MCP_PROXY_TOKEN"
    token = __import__("os").getenv(env_name)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _remote_call(method: str, url: str, token_env: str | None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    import httpx

    with httpx.Client(timeout=30.0) as client:
        res = client.request(method, url, headers=_remote_headers(token_env), json=body)
    try:
        data = res.json() if res.content else {}
    except ValueError:
        data = {"raw": res.text}
    if res.status_code >= 400:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise SystemExit(
            f"admin request {method} {url} failed: {res.status_code} {detail or res.text}"
        )
    return data if isinstance(data, dict) else {"result": data}


def _parse_upstream_spec(body: str) -> dict[str, Any]:
    if body.startswith("stdio:"):
        parts = body[len("stdio:"):].split()
        if not parts:
            raise SystemExit("stdio: requires a command")
        return {"type": "stdio", "command": parts[0], "args": parts[1:]}
    if body.startswith("http:") or body.startswith("https:"):
        return {"type": "http", "url": body}
    raise SystemExit(f"unknown transport in {body!r} (use stdio: or http:)")


def _parse_variables(raw: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw or []:
        if "=" not in item:
            raise SystemExit(f"--var must be KEY=VALUE, got {item!r}")
        k, _, v = item.partition("=")
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# `register` / `unregister` — remote runtime registration
# ---------------------------------------------------------------------------


def cmd_register(args: argparse.Namespace) -> int:
    if args.stdio and args.http:
        print("pass only one of --stdio or --http", file=sys.stderr)
        return 2
    if args.stdio:
        definition = _parse_upstream_spec("stdio:" + args.stdio)
    elif args.http:
        definition = _parse_upstream_spec(args.http if args.http.startswith(("http:", "https:")) else "http:" + args.http)
    elif args.config_json:
        try:
            definition = json.loads(args.config_json)
        except json.JSONDecodeError as exc:
            print(f"--config-json is not valid JSON: {exc}", file=sys.stderr)
            return 2
    else:
        print("pass --stdio, --http, or --config-json", file=sys.stderr)
        return 2
    body = {"name": args.name, "config": definition, "replace": args.replace}
    result = _remote_call(
        "POST",
        args.url.rstrip("/") + "/admin/api/upstreams",
        args.token_env,
        body=body,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_unregister(args: argparse.Namespace) -> int:
    result = _remote_call(
        "DELETE",
        args.url.rstrip("/") + f"/admin/api/upstreams/{args.name}",
        args.token_env,
    )
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# `discover` / `import` — bring MCP servers in from other clients
# ---------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    # Local-only: read client config files directly without talking to a proxy.
    summary = discover_all()
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    for client in summary["clients"]:
        header = f"{client['display_name']} ({client['client_id']})"
        if not client["detected"]:
            print(f"{header}: not detected")
            continue
        print(f"{header}: {client['config_path']}")
        for upstream in client["upstreams"]:
            transport = upstream["config"].get("type", "?")
            print(f"  - {upstream['name']} [{transport}]")
        if not client["upstreams"]:
            print("  (no MCP servers configured)")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        importer = get_importer(args.client)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    discovered = importer.read()
    if not discovered:
        print(f"no MCP servers found in {args.client}", file=sys.stderr)
        return 1
    wanted = set(args.name or [])
    entries = [u for u in discovered if not wanted or u.name in wanted]
    if not entries:
        print(
            f"none of --name {sorted(wanted)} matched {args.client} servers "
            f"{[u.name for u in discovered]}",
            file=sys.stderr,
        )
        return 1
    if args.dry_run:
        print(json.dumps({"imported": [u.to_dict() for u in entries]}, indent=2))
        return 0
    result = _remote_call(
        "POST",
        args.url.rstrip("/") + "/admin/api/discovery/import",
        args.token_env,
        body={
            "client": args.client,
            "upstreams": [u.name for u in entries],
            "replace": args.replace,
        },
    )
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# `catalog` — browse + install bundled MCP servers
# ---------------------------------------------------------------------------


def cmd_catalog_list(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    entries = catalog.search(args.query or "", category=args.category)
    if args.json:
        print(json.dumps({"entries": [e.to_dict() for e in entries]}, indent=2))
        return 0
    if not entries:
        print("no matching catalog entries")
        return 0
    for entry in entries:
        print(f"{entry.id:<18} [{entry.category:<12}] {entry.name}")
        if entry.description:
            print(f"  {entry.description}")
    return 0


def cmd_catalog_install(args: argparse.Namespace) -> int:
    catalog = load_catalog()
    entry = catalog.get(args.id)
    if entry is None:
        print(f"catalog entry {args.id!r} not found", file=sys.stderr)
        return 2
    variables = _parse_variables(args.var)
    try:
        name, definition = entry.materialize(name=args.name, variables=variables)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps({"name": name, "config": definition}, indent=2))
        return 0
    result = _remote_call(
        "POST",
        args.url.rstrip("/") + "/admin/api/catalog/install",
        args.token_env,
        body={
            "id": args.id,
            "name": args.name,
            "variables": variables,
            "replace": args.replace,
        },
    )
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCPy multi-upstream MCP proxy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the MCP proxy server")
    serve.add_argument("--listen", type=parse_listen, default=parse_listen("127.0.0.1:8000"))
    # ``--config`` is now a one-shot bootstrap seed: imported into the DB
    # if (and only if) the DB has no active config yet, then renamed to
    # ``<path>.migrated`` so subsequent restarts use the DB exclusively.
    # Optional once the DB is populated.
    serve.add_argument(
        "--config",
        default=None,
        help="Optional one-shot config seed file imported into the DB on first run",
    )
    serve.add_argument("--log-level", default="info")
    serve.add_argument("--health-path", default="/health")
    serve.add_argument("--request-timeout", type=float, default=30.0)
    serve.add_argument("--idle-timeout", type=int, default=5)
    serve.add_argument("--max-queue", type=int, default=2048)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    init = subparsers.add_parser("init", help="Generate a starter MCPy config file")
    init.add_argument("--output", default="config.json")
    init.add_argument("--force", action="store_true", help="Overwrite an existing file")
    init.add_argument(
        "--upstream",
        action="append",
        metavar="NAME=stdio:CMD or NAME=http:URL",
        help="Define an upstream entry (repeatable)",
    )
    init.set_defaults(func=cmd_init)

    install = subparsers.add_parser(
        "install",
        help="Install MCPy as an MCP server in a client app (Claude Desktop / Code / ChatGPT)",
    )
    install.add_argument(
        "--client",
        required=True,
        choices=list_clients(),
        help="Target client app",
    )
    install.add_argument("--name", default="mcpy", help="Entry name shown in the client")
    install.add_argument("--url", default="http://127.0.0.1:8000", help="MCPy proxy base URL")
    install.add_argument("--token-env", default=None, help="Env var holding the bearer token")
    install.add_argument("--upstream", default=None, help="Pin to a specific upstream")
    install.add_argument(
        "--proxy-command",
        default=None,
        help="Override the `mcp-proxy` command path used by stdio adapters",
    )
    install.add_argument("--config-path", default=None, help="Override client config file path")
    install.add_argument("--dry-run", action="store_true", help="Show diff without writing")
    install.set_defaults(func=cmd_install)

    stdio = subparsers.add_parser(
        "stdio",
        help="Run as a stdio MCP server forwarding to a running MCPy HTTP proxy",
    )
    stdio.add_argument("--connect", required=True, help="MCPy proxy base URL")
    stdio.add_argument("--token-env", default=None, help="Env var with bearer token to forward")
    stdio.add_argument("--upstream", default=None, help="Target upstream name")
    stdio.add_argument("--mount-path", default="/mcp", help="Proxy mount path")
    stdio.set_defaults(func=cmd_stdio)

    # register / unregister --------------------------------------------------

    register = subparsers.add_parser(
        "register",
        help="Register a new upstream on a running MCPy proxy",
    )
    register.add_argument("--url", default="http://127.0.0.1:8000", help="MCPy proxy base URL")
    register.add_argument("--token-env", default=None, help="Env var with bearer token")
    register.add_argument("--name", required=True, help="Upstream name")
    register.add_argument("--stdio", default=None, help="stdio command line, e.g. 'python -m foo'")
    register.add_argument("--http", default=None, help="HTTP upstream URL")
    register.add_argument(
        "--config-json",
        default=None,
        help="Full upstream definition as a JSON string (alternative to --stdio/--http)",
    )
    register.add_argument("--replace", action="store_true", help="Overwrite if upstream exists")
    register.set_defaults(func=cmd_register)

    unregister = subparsers.add_parser(
        "unregister",
        help="Remove an upstream from a running MCPy proxy",
    )
    unregister.add_argument("--url", default="http://127.0.0.1:8000", help="MCPy proxy base URL")
    unregister.add_argument("--token-env", default=None, help="Env var with bearer token")
    unregister.add_argument("--name", required=True, help="Upstream name to remove")
    unregister.set_defaults(func=cmd_unregister)

    # discover / import ------------------------------------------------------

    discover = subparsers.add_parser(
        "discover",
        help="Scan local client configs (Claude Desktop/Code, Cursor, Windsurf, Continue) for MCP servers",
    )
    discover.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    discover.set_defaults(func=cmd_discover)

    importer = subparsers.add_parser(
        "import",
        help="Import MCP servers from another client into a running MCPy proxy",
    )
    importer.add_argument(
        "--client",
        required=True,
        choices=list(IMPORTERS.keys()),
        help="Source client to import from",
    )
    importer.add_argument(
        "--name",
        action="append",
        help="Only import upstreams with this name (repeatable). Default: import all.",
    )
    importer.add_argument("--url", default="http://127.0.0.1:8000", help="MCPy proxy base URL")
    importer.add_argument("--token-env", default=None, help="Env var with bearer token")
    importer.add_argument("--replace", action="store_true", help="Overwrite existing upstreams")
    importer.add_argument("--dry-run", action="store_true", help="Preview without calling the proxy")
    importer.set_defaults(func=cmd_import)

    # catalog ----------------------------------------------------------------

    catalog = subparsers.add_parser(
        "catalog",
        help="Browse and install MCP servers from the bundled catalog",
    )
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)

    catalog_list = catalog_sub.add_parser("list", help="List catalog entries")
    catalog_list.add_argument("--query", "-q", default="", help="Search query")
    catalog_list.add_argument("--category", default=None, help="Filter by category")
    catalog_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    catalog_list.set_defaults(func=cmd_catalog_list)

    catalog_install = catalog_sub.add_parser(
        "install", help="Install a catalog entry as an upstream on a running MCPy proxy"
    )
    catalog_install.add_argument("id", help="Catalog entry id (e.g. filesystem, github)")
    catalog_install.add_argument("--name", default=None, help="Upstream name (defaults to catalog id)")
    catalog_install.add_argument(
        "--var",
        action="append",
        metavar="KEY=VALUE",
        help="Set a catalog variable (repeatable)",
    )
    catalog_install.add_argument("--url", default="http://127.0.0.1:8000", help="MCPy proxy base URL")
    catalog_install.add_argument("--token-env", default=None, help="Env var with bearer token")
    catalog_install.add_argument("--replace", action="store_true", help="Overwrite if upstream exists")
    catalog_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the materialised upstream config without calling the proxy",
    )
    catalog_install.set_defaults(func=cmd_catalog_install)

    # ----------------------------------------------------------------------
    # `config` — DB import/export for the active AppConfig payload
    # ----------------------------------------------------------------------

    config_cmd = subparsers.add_parser(
        "config",
        help="Manage the persisted AppConfig in the local DB",
    )
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)

    config_show = config_sub.add_parser(
        "show", help="Print the active config from the DB as JSON"
    )
    config_show.set_defaults(func=cmd_config_show)

    config_import = config_sub.add_parser(
        "import",
        help="Import a JSON config file into the DB (overwrites the active config)",
    )
    config_import.add_argument("path", help="Path to a JSON file matching AppConfig")
    config_import.add_argument(
        "--source",
        default="cli.config_import",
        help="Source label written into config_history",
    )
    config_import.set_defaults(func=cmd_config_import)

    config_export = config_sub.add_parser(
        "export",
        help="Write the active DB config out to a JSON file",
    )
    config_export.add_argument("path", help="Output path (will be overwritten)")
    config_export.set_defaults(func=cmd_config_export)

    config_history_cmd = config_sub.add_parser(
        "history", help="List recent applies from the DB history table"
    )
    config_history_cmd.add_argument(
        "--limit", type=int, default=20, help="Max rows to return (default 20)"
    )
    config_history_cmd.set_defaults(func=cmd_config_history)

    # ----------------------------------------------------------------------
    # `secrets` — DB-backed secrets management
    # ----------------------------------------------------------------------

    secrets_cmd = subparsers.add_parser(
        "secrets",
        help="Manage encrypted secrets in the local DB",
    )
    secrets_sub = secrets_cmd.add_subparsers(dest="secrets_command", required=True)

    secrets_list = secrets_sub.add_parser(
        "list", help="List secret names + metadata (values are never printed)"
    )
    secrets_list.add_argument("--json", action="store_true")
    secrets_list.set_defaults(func=cmd_secrets_list)

    secrets_set = secrets_sub.add_parser(
        "set", help="Create or rotate a secret value"
    )
    secrets_set.add_argument("name")
    secrets_set.add_argument(
        "--value",
        default=None,
        help="Secret value (default: read from MCPY_SECRET_VALUE env or stdin)",
    )
    secrets_set.add_argument("--description", default="")
    secrets_set.set_defaults(func=cmd_secrets_set)

    secrets_delete = secrets_sub.add_parser("delete", help="Delete a secret")
    secrets_delete.add_argument("name")
    secrets_delete.set_defaults(func=cmd_secrets_delete)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


# ---------------------------------------------------------------------------
# `config` subcommand implementations
# ---------------------------------------------------------------------------


def _open_local_store() -> ConfigStore:
    state_dir = (
        Path(os.getenv("MCPY_STATE_DIR")) if os.getenv("MCPY_STATE_DIR") else _default_state_dir()
    )
    fernet = load_fernet(state_dir)
    return open_store(os.getenv("MCPY_DB_URL"), fernet=fernet)


def cmd_config_show(args: argparse.Namespace) -> int:
    store = _open_local_store()
    payload = store.get_active_config()
    if payload is None:
        print("(DB has no active config)", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2))
    return 0


def cmd_config_import(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"invalid JSON in {path}: {exc}", file=sys.stderr)
        return 2
    store = _open_local_store()
    version = store.save_active_config(payload, source=args.source)
    print(f"imported {path} into DB at version {version}")
    return 0


def cmd_config_export(args: argparse.Namespace) -> int:
    store = _open_local_store()
    payload = store.get_active_config()
    if payload is None:
        print("(DB has no active config)", file=sys.stderr)
        return 1
    out = Path(args.path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} (version {store.active_version()})")
    return 0


def cmd_config_history(args: argparse.Namespace) -> int:
    store = _open_local_store()
    rows = store.list_config_history(limit=args.limit)
    print(json.dumps(rows, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# `secrets` subcommand implementations
# ---------------------------------------------------------------------------


def cmd_secrets_list(args: argparse.Namespace) -> int:
    store = _open_local_store()
    items = store.list_public_secrets()
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    if not items:
        print("(no secrets)")
        return 0
    for entry in items:
        ts = entry.get("updated_at") or 0.0
        print(
            f"{entry['name']:<32} {entry['value_preview']:<20} updated={ts:.0f} "
            f"len={entry['value_length']} {entry.get('description') or ''}".rstrip()
        )
    return 0


def cmd_secrets_set(args: argparse.Namespace) -> int:
    value: str | None = args.value
    if value is None:
        value = os.getenv("MCPY_SECRET_VALUE")
    if value is None:
        if sys.stdin.isatty():
            print(
                f"reading value for secret {args.name!r} from stdin (Ctrl-D to end):",
                file=sys.stderr,
            )
        value = sys.stdin.read().rstrip("\n")
    if not value:
        print("refusing to write empty secret value", file=sys.stderr)
        return 2
    store = _open_local_store()
    rec = store.upsert_secret(args.name, value, description=args.description)
    print(f"set {rec.name} (length={len(value)})")
    return 0


def cmd_secrets_delete(args: argparse.Namespace) -> int:
    store = _open_local_store()
    removed = store.delete_secret(args.name)
    if not removed:
        print(f"secret {args.name!r} not found", file=sys.stderr)
        return 1
    print(f"deleted {args.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
