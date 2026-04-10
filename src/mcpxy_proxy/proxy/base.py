"""Base interfaces for upstream transports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcpxy_proxy.proxy.bridge import RequestContext


class UpstreamTransport(ABC):
    """Abstract transport interface for an upstream MCP server."""

    @abstractmethod
    async def start(self) -> None:
        """Start transport resources."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop transport resources."""

    @abstractmethod
    async def restart(self) -> None:
        """Restart transport resources."""

    @abstractmethod
    async def request(
        self,
        message: dict[str, Any],
        context: "RequestContext | None" = None,
    ) -> dict[str, Any] | None:
        """Send request and return response if available."""

    @abstractmethod
    async def send_notification(
        self,
        message: dict[str, Any],
        context: "RequestContext | None" = None,
    ) -> None:
        """Send a notification without expecting a response."""

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return health snapshot."""
