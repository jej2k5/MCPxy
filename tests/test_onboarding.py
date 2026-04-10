"""Tests for the first-run onboarding wizard.

Covers the whole backend surface:

- ``ConfigStore.ensure_onboarding_row`` + state transitions
- ``AuthConfig.token`` precedence in ``resolve_admin_token``
- Auth bypass on the onboarding endpoints while inactive vs. active
- ``/admin/api/onboarding/set_admin_token`` happy path + rejection
- ``/admin/api/onboarding/add_upstream`` (optional step)
- ``/admin/api/onboarding/test_database`` + ``set_database`` (hot-swap
  + restart-fallback) — lets the wizard pick SQLite / Postgres / MySQL
  from the UI instead of requiring env vars.
- ``/admin/api/onboarding/finish`` (must come after set_admin_token)
- The "onboarding_required" 503 middleware on every *other* admin path
- 410 Gone after finish
- TTL expiry behaviour
- Loopback-only gating + override via MCPXY_ONBOARDING_ALLOWED_CLIENTS
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from mcpxy_proxy.config import AppConfig, AuthConfig, resolve_admin_token
from mcpxy_proxy.plugins.registry import PluginRegistry
from mcpxy_proxy.proxy.bridge import ProxyBridge
from mcpxy_proxy.proxy.manager import UpstreamManager
from mcpxy_proxy.secrets import SecretsManager
from mcpxy_proxy.server import AppState, create_app
from mcpxy_proxy.storage.config_store import ConfigStore, open_store
from mcpxy_proxy.telemetry.noop_sink import NoopTelemetrySink
from mcpxy_proxy.telemetry.pipeline import TelemetryPipeline
