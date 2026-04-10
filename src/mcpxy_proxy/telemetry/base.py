"""Telemetry sink interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TelemetrySink(ABC):
    """Interface for telemetry sinks."""

    @abstractmethod
    async def start(self) -> None:
        """Start sink."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop sink."""

    @abstractmethod
    async def emit(self, event: dict[str, Any]) -> None:
        """Emit a telemetry event."""

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return sink health."""
