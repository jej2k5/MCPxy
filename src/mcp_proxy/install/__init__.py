"""Install helpers for wiring MCPy into MCP-aware client apps."""

from mcp_proxy.install.clients import (
    ClaudeCodeAdapter,
    ClaudeDesktopAdapter,
    ChatGPTAdapter,
    ClientAdapter,
    InstallOptions,
    get_adapter,
    list_clients,
)

__all__ = [
    "ClientAdapter",
    "ClaudeDesktopAdapter",
    "ClaudeCodeAdapter",
    "ChatGPTAdapter",
    "InstallOptions",
    "get_adapter",
    "list_clients",
]
