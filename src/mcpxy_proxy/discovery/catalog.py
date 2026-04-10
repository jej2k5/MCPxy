"""Bundled MCP server catalog.

MCPxy ships a curated JSON catalog of well-known MCP servers so the
dashboard's Browse page can offer one-click installation without
requiring the user to know how each server is distributed. The catalog
is loaded at startup and served through ``/admin/api/catalog``.

Entries are rendered into concrete upstream configs by
:func:`materialize`, which substitutes user-supplied environment
variables into the ``args`` and ``env`` fields.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "mcp_catalog.json"
_VAR_RE = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass(frozen=True)
class CatalogVariable:
    """A user-supplied variable required by a catalog entry."""

    name: str
    description: str
    required: bool = False
    default: str | None = None
    secret: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogVariable":
        return cls(
            name=str(data["name"]),
            description=str(data.get("description", "")),
            required=bool(data.get("required", False)),
            default=data.get("default"),
            secret=bool(data.get("secret", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "required": self.required,
            "secret": self.secret,
        }
        if self.default is not None:
            out["default"] = self.default
        return out


@dataclass(frozen=True)
class CatalogEntry:
    """A single MCP server available in the bundled catalog."""

    id: str
    name: str
    description: str
    category: str
    homepage: str
    transport: str  # "stdio" or "http"
    install_hint: str = ""
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    variables: tuple[CatalogVariable, ...] = ()
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogEntry":
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            description=str(data.get("description", "")),
            category=str(data.get("category", "other")),
            homepage=str(data.get("homepage", "")),
            transport=str(data["transport"]),
            install_hint=str(data.get("install_hint", "")),
            command=data.get("command"),
            args=tuple(str(a) for a in data.get("args", []) or ()),
            url=data.get("url"),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            variables=tuple(CatalogVariable.from_dict(v) for v in (data.get("variables") or [])),
            tags=tuple(str(t) for t in (data.get("tags") or [])),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "homepage": self.homepage,
            "transport": self.transport,
            "install_hint": self.install_hint,
            "tags": list(self.tags),
            "variables": [v.to_dict() for v in self.variables],
        }
        if self.command is not None:
            out["command"] = self.command
        if self.args:
            out["args"] = list(self.args)
        if self.url is not None:
            out["url"] = self.url
        if self.env:
            out["env"] = dict(self.env)
        return out

    def matches(self, query: str) -> bool:
        q = query.strip().lower()
        if not q:
            return True
        hay = " ".join(
            [self.id, self.name, self.description, self.category, *self.tags]
        ).lower()
        return all(part in hay for part in q.split())

    def materialize(
        self,
        *,
        name: str | None = None,
        variables: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Render this entry into a concrete ``(upstream_name, config)`` pair.

        Missing required variables raise ``ValueError`` and any ``${VAR}``
        placeholders in ``args``/``env``/``url`` are substituted.
        """
        supplied = dict(variables or {})
        effective: dict[str, str] = {}
        missing: list[str] = []
        for var in self.variables:
            if var.name in supplied and supplied[var.name] != "":
                effective[var.name] = supplied[var.name]
            elif var.default is not None:
                effective[var.name] = var.default
            elif var.required:
                missing.append(var.name)
        if missing:
            raise ValueError(
                f"missing required variable(s) for catalog entry '{self.id}': "
                + ", ".join(missing)
            )

        def sub(value: str) -> str:
            return _VAR_RE.sub(lambda m: effective.get(m.group(1), ""), value)

        upstream_name = name or self.id
        if self.transport == "stdio":
            if not self.command:
                raise ValueError(f"catalog entry '{self.id}' is stdio but has no command")
            config: dict[str, Any] = {
                "type": "stdio",
                "command": sub(self.command),
                "args": [sub(a) for a in self.args],
            }
            if self.env:
                config["env"] = {k: sub(v) for k, v in self.env.items()}
        elif self.transport == "http":
            if not self.url:
                raise ValueError(f"catalog entry '{self.id}' is http but has no url")
            config = {"type": "http", "url": sub(self.url)}
        else:
            raise ValueError(f"unknown transport '{self.transport}' in catalog entry '{self.id}'")
        return upstream_name, config


@dataclass
class Catalog:
    """In-memory view of the bundled MCP server catalog."""

    version: int
    updated_at: str
    entries: list[CatalogEntry]

    def search(self, query: str = "", category: str | None = None) -> list[CatalogEntry]:
        results = [e for e in self.entries if e.matches(query)]
        if category:
            results = [e for e in results if e.category == category]
        return results

    def get(self, entry_id: str) -> CatalogEntry | None:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def categories(self) -> list[str]:
        return sorted({e.category for e in self.entries})

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "entries": [e.to_dict() for e in self.entries],
            "categories": self.categories(),
        }


def load_catalog(path: Path | str | None = None) -> Catalog:
    """Load and parse the bundled MCP catalog from ``path`` (defaults to the shipped copy)."""
    target = Path(path) if path is not None else CATALOG_PATH
    data = json.loads(target.read_text(encoding="utf-8"))
    return Catalog(
        version=int(data.get("version", 1)),
        updated_at=str(data.get("updated_at", "")),
        entries=[CatalogEntry.from_dict(e) for e in data.get("entries", [])],
    )


def iter_catalog(entries: Iterable[CatalogEntry]) -> Iterable[dict[str, Any]]:
    for entry in entries:
        yield entry.to_dict()
