"""Install helpers for wiring MCPxy into MCP-aware client apps."""

from mcpxy_proxy.install.clients import (
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
