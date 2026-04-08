"""MCP server discovery: catalog, import from clients, runtime registration."""

from mcp_proxy.discovery.catalog import CATALOG_PATH, Catalog, CatalogEntry, load_catalog
from mcp_proxy.discovery.importers import (
    ClientImporter,
    DiscoveredUpstream,
    IMPORTERS,
    discover_all,
    get_importer,
)
from mcp_proxy.discovery.registration import (
    FileDropWatcher,
    RegistrationService,
)

__all__ = [
    "CATALOG_PATH",
    "Catalog",
    "CatalogEntry",
    "load_catalog",
    "ClientImporter",
    "DiscoveredUpstream",
    "IMPORTERS",
    "discover_all",
    "get_importer",
    "FileDropWatcher",
    "RegistrationService",
]
