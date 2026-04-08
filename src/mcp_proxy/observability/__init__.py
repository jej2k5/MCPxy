"""Observability primitives: traffic capture and route discovery."""

from mcp_proxy.observability.discovery import RouteDiscoverer
from mcp_proxy.observability.traffic import TrafficRecord, TrafficRecorder

__all__ = ["TrafficRecord", "TrafficRecorder", "RouteDiscoverer"]
