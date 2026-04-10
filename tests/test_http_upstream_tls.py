"""Outbound mTLS for HTTP upstream transports.

Covers the per-upstream ``tls`` config block (``verify``,
``client_cert``, ``client_key``, ``client_key_password``) and how
:class:`HttpUpstreamTransport` threads it into ``httpx.AsyncClient``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from mcpxy_proxy.config import (
    AppConfig,
    HttpUpstreamConfig,
    HttpUpstreamTlsConfig,
    _apply_expansions,
    load_config,
    redact_secrets,
)
from mcpxy_proxy.proxy.http import HttpUpstreamTransport


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_http_upstream_tls_defaults() -> None:
    tls = HttpUpstreamTlsConfig()
    assert tls.verify is True
    assert tls.client_cert is None
    assert tls.client_key is None
    assert tls.client_key_password is None


def test_http_upstream_config_accepts_tls_block() -> None:
    cfg = HttpUpstreamConfig(
        type="http",
        url="https://upstream.example/mcp",
        tls={
            "verify": "/etc/mcpxy/upstream-ca.pem",
            "client_cert": "/etc/mcpxy/client.pem",
            "client_key": "/etc/mcpxy/client.key",
        },
    )
    assert cfg.tls is not None
    assert cfg.tls.verify == "/etc/mcpxy/upstream-ca.pem"
    assert cfg.tls.client_cert == "/etc/mcpxy/client.pem"
    assert cfg.tls.client_key == "/etc/mcpxy/client.key"


def test_http_upstream_tls_client_key_requires_client_cert() -> None:
    with pytest.raises(ValidationError):
        HttpUpstreamTlsConfig(client_key="/etc/mcpxy/client.key")


def test_http_upstream_tls_password_requires_client_key() -> None:
    with pytest.raises(ValidationError):
        HttpUpstreamTlsConfig(
            client_cert="/etc/mcpxy/client.pem",
            client_key_password="hunter2",
        )


def test_http_upstream_tls_allows_verify_bool_and_path() -> None:
    assert HttpUpstreamTlsConfig(verify=False).verify is False
    assert HttpUpstreamTlsConfig(verify="/etc/ca.pem").verify == "/etc/ca.pem"


# ---------------------------------------------------------------------------
# Expansion + redaction
# ---------------------------------------------------------------------------


def test_http_upstream_tls_client_key_password_env_expansion(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("UPSTREAM_KEY_PW", "from-env")
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "upstreams": {
                    "remote": {
                        "type": "http",
                        "url": "https://upstream.example/mcp",
                        "tls": {
                            "client_cert": "/etc/mcpxy/client.pem",
                            "client_key": "/etc/mcpxy/client.key",
                            "client_key_password": "${env:UPSTREAM_KEY_PW}",
                        },
                    }
                }
            }
        )
    )
    cfg = load_config(config_path)
    upstream = cfg.upstreams["remote"]
    assert upstream.tls.client_key_password == "from-env"  # type: ignore[union-attr]


def test_http_upstream_tls_client_key_password_secret_expansion() -> None:
    payload = {
        "upstreams": {
            "remote": {
                "type": "http",
                "url": "https://upstream.example/mcp",
                "tls": {
                    "client_cert": "/etc/mcpxy/client.pem",
                    "client_key": "/etc/mcpxy/client.key",
                    "client_key_password": "${secret:UPSTREAM_KEY_PW}",
                },
            }
        }
    }

    def resolver(name: str) -> str | None:
        return "stub-pw" if name == "UPSTREAM_KEY_PW" else None

    expanded = _apply_expansions(payload, secrets=resolver)
    cfg = AppConfig.model_validate(expanded)
    assert cfg.upstreams["remote"].tls.client_key_password == "stub-pw"  # type: ignore[union-attr]


def test_redact_secrets_masks_upstream_client_key_password() -> None:
    payload = {
        "upstreams": {
            "remote": {
                "type": "http",
                "url": "https://upstream.example/mcp",
                "tls": {
                    "client_cert": "/etc/mcpxy/client.pem",
                    "client_key": "/etc/mcpxy/client.key",
                    "client_key_password": "hunter2",
                },
            }
        }
    }
    redacted = redact_secrets(payload)
    tls = redacted["upstreams"]["remote"]["tls"]
    assert tls["client_key_password"] == "***REDACTED***"
    # Non-secret fields stay visible.
    assert tls["client_cert"] == "/etc/mcpxy/client.pem"
    assert tls["client_key"] == "/etc/mcpxy/client.key"
    # Original payload is untouched.
    assert payload["upstreams"]["remote"]["tls"]["client_key_password"] == "hunter2"


# ---------------------------------------------------------------------------
# Transport integration: httpx.AsyncClient kwargs
# ---------------------------------------------------------------------------


class _AsyncClientSpy:
    """Stub ``httpx.AsyncClient`` that records the kwargs it was built with.

    We only care about the ``verify`` / ``cert`` kwargs, but keep a few
    no-op methods so the transport's lifecycle (``start`` / ``stop``)
    runs to completion without blowing up.
    """

    last_kwargs: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _AsyncClientSpy.last_kwargs = dict(kwargs)

    async def aclose(self) -> None:
        return None


@pytest.fixture()
def httpx_client_spy(monkeypatch: pytest.MonkeyPatch) -> type[_AsyncClientSpy]:
    _AsyncClientSpy.last_kwargs = {}
    monkeypatch.setattr("mcpxy_proxy.proxy.http.httpx.AsyncClient", _AsyncClientSpy)
    return _AsyncClientSpy


def _write(path: Path, content: str = "stub") -> str:
    path.write_text(content)
    return str(path)


@pytest.mark.asyncio
async def test_transport_no_tls_passes_no_verify_or_cert(
    httpx_client_spy: type[_AsyncClientSpy],
) -> None:
    transport = HttpUpstreamTransport(
        "t",
        {"type": "http", "url": "https://upstream.example/mcp"},
    )
    await transport.start()
    await transport.stop()
    assert "verify" not in _AsyncClientSpy.last_kwargs
    assert "cert" not in _AsyncClientSpy.last_kwargs


@pytest.mark.asyncio
async def test_transport_tls_default_verify_true_passes_no_kwarg(
    httpx_client_spy: type[_AsyncClientSpy],
) -> None:
    # verify=True is httpx's default; we shouldn't bother passing it
    # explicitly, so the kwarg should be absent.
    transport = HttpUpstreamTransport(
        "t",
        {
            "type": "http",
            "url": "https://upstream.example/mcp",
            "tls": {},
        },
    )
    await transport.start()
    await transport.stop()
    assert "verify" not in _AsyncClientSpy.last_kwargs
    assert "cert" not in _AsyncClientSpy.last_kwargs


@pytest.mark.asyncio
async def test_transport_tls_verify_false_forwarded(
    httpx_client_spy: type[_AsyncClientSpy],
) -> None:
    transport = HttpUpstreamTransport(
        "t",
        {
            "type": "http",
            "url": "https://upstream.example/mcp",
            "tls": {"verify": False},
        },
    )
    await transport.start()
    await transport.stop()
    assert _AsyncClientSpy.last_kwargs["verify"] is False


@pytest.mark.asyncio
async def test_transport_tls_verify_ca_bundle_forwarded(
    httpx_client_spy: type[_AsyncClientSpy],
    tmp_path: Path,
) -> None:
    ca = _write(tmp_path / "ca.pem", "-----BEGIN CERTIFICATE-----\nstub\n")
    transport = HttpUpstreamTransport(
        "t",
        {
            "type": "http",
            "url": "https://upstream.example/mcp",
            "tls": {"verify": ca},
        },
    )
    await transport.start()
    await transport.stop()
    assert _AsyncClientSpy.last_kwargs["verify"] == ca


@pytest.mark.asyncio
async def test_transport_tls_verify_ca_bundle_missing_fails_fast(
    httpx_client_spy: type[_AsyncClientSpy],
) -> None:
    transport = HttpUpstreamTransport(
        "t",
        {
            "type": "http",
            "url": "https://upstream.example/mcp",
            "tls": {"verify": "/no/such/ca.pem"},
        },
    )
    with pytest.raises(RuntimeError, match="CA bundle not found"):
        await transport.start()


@pytest.mark.asyncio
async def test_transport_mtls_cert_key_tuple_forwarded(
    httpx_client_spy: type[_AsyncClientSpy],
    tmp_path: Path,
) -> None:
    cert = _write(tmp_path / "client.pem")
    key = _write(tmp_path / "client.key")
    transport = HttpUpstreamTransport(
        "t",
        {
            "type": "http",
            "url": "https://upstream.example/mcp",
            "tls": {"client_cert": cert, "client_key": key},
        },
    )
    await transport.start()
    await transport.stop()
    assert _AsyncClientSpy.last_kwargs["cert"] == (cert, key)


@pytest.mark.asyncio
async def test_transport_mtls_cert_key_password_forwarded(
    httpx_client_spy: type[_AsyncClientSpy],
    tmp_path: Path,
) -> None:
    cert = _write(tmp_path / "client.pem")
    key = _write(tmp_path / "client.key")
    transport = HttpUpstreamTransport(
        "t",
        {
            "type": "http",
            "url": "https://upstream.example/mcp",
            "tls": {
                "client_cert": cert,
                "client_key": key,
                "client_key_password": "hunter2",
            },
        },
    )
    await transport.start()
    await transport.stop()
    assert _AsyncClientSpy.last_kwargs["cert"] == (cert, key, "hunter2")


@pytest.mark.asyncio
async def test_transport_mtls_combined_cert_only(
    httpx_client_spy: type[_AsyncClientSpy],
    tmp_path: Path,
) -> None:
    cert = _write(tmp_path / "combined.pem")
    transport = HttpUpstreamTransport(
        "t",
        {
            "type": "http",
            "url": "https://upstream.example/mcp",
            "tls": {"client_cert": cert},
        },
    )
    await transport.start()
    await transport.stop()
    assert _AsyncClientSpy.last_kwargs["cert"] == cert


@pytest.mark.asyncio
async def test_transport_mtls_cert_missing_fails_fast(
    httpx_client_spy: type[_AsyncClientSpy],
) -> None:
    transport = HttpUpstreamTransport(
        "t",
        {
            "type": "http",
            "url": "https://upstream.example/mcp",
            "tls": {"client_cert": "/no/such/cert.pem"},
        },
    )
    with pytest.raises(RuntimeError, match="client_cert not found"):
        await transport.start()


@pytest.mark.asyncio
async def test_transport_mtls_key_missing_fails_fast(
    httpx_client_spy: type[_AsyncClientSpy],
    tmp_path: Path,
) -> None:
    cert = _write(tmp_path / "client.pem")
    transport = HttpUpstreamTransport(
        "t",
        {
            "type": "http",
            "url": "https://upstream.example/mcp",
            "tls": {"client_cert": cert, "client_key": "/no/such/client.key"},
        },
    )
    with pytest.raises(RuntimeError, match="client_key not found"):
        await transport.start()


@pytest.mark.asyncio
async def test_transport_health_reports_tls(
    httpx_client_spy: type[_AsyncClientSpy],
    tmp_path: Path,
) -> None:
    cert = _write(tmp_path / "client.pem")
    key = _write(tmp_path / "client.key")
    transport = HttpUpstreamTransport(
        "t",
        {
            "type": "http",
            "url": "https://upstream.example/mcp",
            "tls": {"client_cert": cert, "client_key": key, "verify": False},
        },
    )
    await transport.start()
    try:
        h = transport.health()
        assert h["tls"] == {"verify": False, "mtls": True}
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_transport_health_tls_none_when_unset(
    httpx_client_spy: type[_AsyncClientSpy],
) -> None:
    transport = HttpUpstreamTransport(
        "t",
        {"type": "http", "url": "https://upstream.example/mcp"},
    )
    await transport.start()
    try:
        assert transport.health()["tls"] is None
    finally:
        await transport.stop()
