"""Tests for the bundled MCP server catalog."""

import json
from pathlib import Path

import pytest

from mcpxy_proxy.discovery.catalog import (
    CATALOG_PATH,
    Catalog,
    CatalogEntry,
    CatalogVariable,
    load_catalog,
)


def test_load_bundled_catalog() -> None:
    catalog = load_catalog()
    assert catalog.version >= 1
    assert len(catalog.entries) > 5
    # Spot-check a few well-known entries ship in the catalog.
    ids = {entry.id for entry in catalog.entries}
    assert {"filesystem", "git", "github", "sqlite"} <= ids
    # Categories are derived from entries.
    assert "developer" in catalog.categories()


def test_catalog_search_is_case_insensitive_and_matches_all_parts() -> None:
    catalog = load_catalog()
    github = catalog.search("GitHub")
    assert any(e.id == "github" for e in github)
    # Multi-term matching uses AND semantics across name/description/tags.
    sqlite_sql = catalog.search("sqlite sql")
    assert all("sqlite" in e.id for e in sqlite_sql)


def test_catalog_search_by_category() -> None:
    catalog = load_catalog()
    db = catalog.search(category="database")
    assert db
    assert all(e.category == "database" for e in db)


def test_catalog_get_by_id() -> None:
    catalog = load_catalog()
    filesystem = catalog.get("filesystem")
    assert filesystem is not None
    assert filesystem.transport == "stdio"
    assert catalog.get("definitely-not-a-real-id") is None


def test_materialize_substitutes_variables_and_returns_mcpxy_config() -> None:
    entry = CatalogEntry(
        id="demo",
        name="Demo",
        description="",
        category="other",
        homepage="",
        transport="stdio",
        command="demo",
        args=("--path", "${path}", "--flag"),
        env={"DEMO_KEY": "${secret}"},
        variables=(
            CatalogVariable(name="path", description="", required=True),
            CatalogVariable(name="secret", description="", required=True, secret=True),
        ),
    )
    name, config = entry.materialize(
        name="my-demo",
        variables={"path": "/tmp/x", "secret": "s3cr3t"},
    )
    assert name == "my-demo"
    assert config == {
        "type": "stdio",
        "command": "demo",
        "args": ["--path", "/tmp/x", "--flag"],
        "env": {"DEMO_KEY": "s3cr3t"},
    }


def test_materialize_uses_defaults_when_not_required() -> None:
    entry = CatalogEntry(
        id="demo",
        name="Demo",
        description="",
        category="other",
        homepage="",
        transport="http",
        url="https://example.com/${region}/mcp",
        variables=(
            CatalogVariable(
                name="region",
                description="",
                required=False,
                default="us-east-1",
            ),
        ),
    )
    name, config = entry.materialize()
    assert name == "demo"
    assert config == {"type": "http", "url": "https://example.com/us-east-1/mcp"}


def test_materialize_rejects_missing_required_variables() -> None:
    entry = CatalogEntry(
        id="demo",
        name="Demo",
        description="",
        category="other",
        homepage="",
        transport="stdio",
        command="demo",
        args=("${missing}",),
        variables=(CatalogVariable(name="missing", description="", required=True),),
    )
    with pytest.raises(ValueError, match="missing required variable"):
        entry.materialize()


def test_bundled_catalog_parses_as_json() -> None:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert "entries" in data
    assert isinstance(data["entries"], list)


def test_catalog_from_custom_path(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "updated_at": "2026-04-08",
        "entries": [
            {
                "id": "noop",
                "name": "Noop",
                "description": "A do-nothing upstream for tests.",
                "category": "test",
                "homepage": "",
                "transport": "stdio",
                "command": "true",
            }
        ],
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    catalog = load_catalog(path)
    assert len(catalog.entries) == 1
    assert catalog.entries[0].id == "noop"
