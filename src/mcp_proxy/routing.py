"""Routing logic for selecting target upstream."""

from __future__ import annotations

from typing import Any

from mcp_proxy.config import AppConfig


class RouteResult(dict):
    """Route result object with selected upstream and cleaned message."""


def resolve_upstream(
    message: dict[str, Any],
    config: AppConfig,
    path_name: str | None,
    header_name: str | None,
) -> tuple[str | None, dict[str, Any]]:
    """Resolve upstream with precedence and strip in-band routing param."""
    cleaned = dict(message)
    params = cleaned.get("params")
    in_band: str | None = None
    if isinstance(params, dict) and "mcp_upstream" in params:
        in_band = params.get("mcp_upstream")
        params = dict(params)
        params.pop("mcp_upstream", None)
        cleaned["params"] = params

    upstream = path_name or header_name or in_band or config.default_upstream
    if upstream and upstream in config.upstreams:
        return upstream, cleaned
    return None, cleaned
