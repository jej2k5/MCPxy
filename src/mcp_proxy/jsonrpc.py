"""JSON-RPC helpers and errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class JsonRpcError(Exception):
    """Represent a JSON-RPC error payload."""

    code: int
    message: str
    data: Any | None = None
    request_id: Any | None = None

    def to_response(self) -> dict[str, Any]:
        """Return the JSON-RPC error response."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "error": {"code": self.code, "message": self.message},
            "id": self.request_id,
        }
        if self.data is not None:
            payload["error"]["data"] = self.data
        return payload


def is_notification(message: dict[str, Any]) -> bool:
    """Return True if message is a JSON-RPC notification."""
    return "id" not in message
