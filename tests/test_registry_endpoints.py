"""Tests for the new registration/discovery/catalog admin API endpoints."""

import json
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mcp_proxy.config import AppConfig
from mcp_proxy.proxy.bridge import ProxyBridge
from mcp_proxy.proxy.manager import PluginRegistry, UpstreamManager
from mcp_proxy.server import AppState, create_app
from mcp_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcp_proxy.telemetry.pipeline import TelemetryPipeline


HEADERS = {"Authorization": "Bearer secret"}


def _client() -> TestClient:
    config = AppConfig.model_validate(
        {
            "auth": {"token_env": "MCP_PROXY_TOKEN"},
            "admin": {
                "enabled": True,
                "mount_name": "__admin__",
                "require_token": True,
                "allowed_clients": ["testclient"],
            },
            "upstreams": {"seed": {"type": "http", "url": "http://seed"}},
        }
    )
    os.environ["MCP_PROXY_TOKEN"] = "secret"
    reg = PluginRegistry()
    manager = UpstreamManager(config.upstreams, reg)
    bridge = ProxyBridge(manager)
    telemetry = TelemetryPipeline(NoopTelemetrySink())
    raw = config.model_dump()
    # Disable file-drop watcher for tests so we don't touch ~/.mcpy.
    raw["registration"] = {"file_drop_enabled": False}
    state = AppState(config, raw, manager, bridge, telemetry, reg)
    return TestClient(create_app(state))


def test_register_upstream_adds_live_config() -> None:
    client = _client()
    res = client.post(
        "/admin/api/upstreams",
        headers=HEADERS,
        json={
            "name": "new",
            "config": {"type": "http", "url": "http://new.example"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["applied"] is True

    listed = client.get("/admin/api/upstreams/registered", headers=HEADERS)
    assert listed.status_code == 200
    assert "new" in listed.json()["upstreams"]


def test_register_requires_name_and_config() -> None:
    client = _client()
    res = client.post(
        "/admin/api/upstreams",
        headers=HEADERS,
        json={"config": {"type": "http", "url": "http://x"}},
    )
    assert res.status_code == 400


def test_register_rejects_duplicate_without_replace() -> None:
    client = _client()
    res = client.post(
        "/admin/api/upstreams",
        headers=HEADERS,
        json={"name": "seed", "config": {"type": "http", "url": "http://other"}},
    )
    assert res.status_code == 400


def test_register_replace_overwrites() -> None:
    client = _client()
    res = client.post(
        "/admin/api/upstreams",
        headers=HEADERS,
        json={
            "name": "seed",
            "config": {"type": "http", "url": "http://other"},
            "replace": True,
        },
    )
    assert res.status_code == 200
    assert res.json()["applied"] is True


def test_unregister_upstream_removes_it() -> None:
    client = _client()
    res = client.delete("/admin/api/upstreams/seed", headers=HEADERS)
    assert res.status_code == 200
    listed = client.get("/admin/api/upstreams/registered", headers=HEADERS)
    assert "seed" not in listed.json()["upstreams"]


def test_unregister_unknown_upstream_404() -> None:
    client = _client()
    res = client.delete("/admin/api/upstreams/ghost", headers=HEADERS)
    assert res.status_code == 404


def test_register_endpoints_require_auth() -> None:
    client = _client()
    assert client.get("/admin/api/upstreams/registered").status_code == 401
    assert client.post("/admin/api/upstreams", json={}).status_code == 401
    assert client.delete("/admin/api/upstreams/seed").status_code == 401


def test_discovery_clients_endpoint_is_stable() -> None:
    client = _client()
    # Point every importer at a non-existent path so the endpoint returns
    # a deterministic "not detected" result regardless of host state.
    missing = [Path("/definitely/not/here.json")]
    with patch(
        "mcp_proxy.discovery.importers.ClaudeDesktopImporter.candidate_paths",
        return_value=missing,
    ), patch(
        "mcp_proxy.discovery.importers.ClaudeCodeImporter.candidate_paths",
        return_value=missing,
    ), patch(
        "mcp_proxy.discovery.importers.CursorImporter.candidate_paths",
        return_value=missing,
    ), patch(
        "mcp_proxy.discovery.importers.WindsurfImporter.candidate_paths",
        return_value=missing,
    ), patch(
        "mcp_proxy.discovery.importers.ContinueImporter.candidate_paths",
        return_value=missing,
    ):
        res = client.get("/admin/api/discovery/clients", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert len(body["clients"]) == 5
    assert all(c["detected"] is False for c in body["clients"])


def test_discovery_import_adds_selected_upstreams(tmp_path: Path) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "imported-http": {"url": "https://fromclient.example/mcp"},
                    "imported-stdio": {"command": "true", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    client = _client()
    with patch(
        "mcp_proxy.discovery.importers.ClaudeDesktopImporter.candidate_paths",
        return_value=[config_path],
    ):
        res = client.post(
            "/admin/api/discovery/import",
            headers=HEADERS,
            json={
                "client": "claude-desktop",
                "upstreams": ["imported-http"],
            },
        )
    assert res.status_code == 200
    body = res.json()
    assert body["applied"] is True
    assert body["imported"] == ["imported-http"]

    listed = client.get("/admin/api/upstreams/registered", headers=HEADERS)
    assert "imported-http" in listed.json()["upstreams"]


def test_discovery_import_unknown_client_404() -> None:
    client = _client()
    res = client.post(
        "/admin/api/discovery/import",
        headers=HEADERS,
        json={"client": "nonsense", "upstreams": []},
    )
    assert res.status_code == 404


def test_discovery_import_missing_upstream_404(tmp_path: Path) -> None:
    config_path = tmp_path / "claude_desktop_config.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    client = _client()
    with patch(
        "mcp_proxy.discovery.importers.ClaudeDesktopImporter.candidate_paths",
        return_value=[config_path],
    ):
        res = client.post(
            "/admin/api/discovery/import",
            headers=HEADERS,
            json={"client": "claude-desktop", "upstreams": ["ghost"]},
        )
    assert res.status_code == 404


def test_catalog_endpoint_returns_entries() -> None:
    client = _client()
    res = client.get("/admin/api/catalog", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["version"] >= 1
    assert len(body["entries"]) > 0
    assert "developer" in body["categories"]


def test_catalog_endpoint_supports_search_and_category() -> None:
    client = _client()
    res = client.get(
        "/admin/api/catalog?q=git&category=developer",
        headers=HEADERS,
    )
    assert res.status_code == 200
    body = res.json()
    ids = {e["id"] for e in body["entries"]}
    assert {"git", "github"} <= ids


def test_catalog_install_materialises_entry() -> None:
    client = _client()
    res = client.post(
        "/admin/api/catalog/install",
        headers=HEADERS,
        json={
            "id": "filesystem",
            "name": "fs-test",
            "variables": {"allowed_path": "/tmp"},
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["applied"] is True
    assert body["installed"]["id"] == "filesystem"

    listed = client.get("/admin/api/upstreams/registered", headers=HEADERS)
    upstreams = listed.json()["upstreams"]
    assert "fs-test" in upstreams
    fs = upstreams["fs-test"]
    assert fs["type"] == "stdio"
    assert fs["command"] == "npx"
    assert "/tmp" in fs["args"]


def test_catalog_install_rejects_missing_required_variables() -> None:
    client = _client()
    res = client.post(
        "/admin/api/catalog/install",
        headers=HEADERS,
        json={"id": "github", "variables": {}},
    )
    assert res.status_code == 400


def test_catalog_install_unknown_entry_404() -> None:
    client = _client()
    res = client.post(
        "/admin/api/catalog/install",
        headers=HEADERS,
        json={"id": "does-not-exist"},
    )
    assert res.status_code == 404


def test_catalog_endpoints_require_auth() -> None:
    client = _client()
    assert client.get("/admin/api/catalog").status_code == 401
    assert client.post("/admin/api/catalog/install", json={}).status_code == 401
    assert client.get("/admin/api/discovery/clients").status_code == 401
    assert client.post("/admin/api/discovery/import", json={}).status_code == 401
