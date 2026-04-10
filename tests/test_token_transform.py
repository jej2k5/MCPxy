"""Tests for the token transformation policy.

Covers config validation, ConfigStore CRUD for token mappings,
and the HttpUpstreamTransport transform logic.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from mcpxy_proxy.config import (
    HttpUpstreamConfig,
    TokenTransformConfig,
)
from mcpxy_proxy.proxy.bridge import RequestContext
from mcpxy_proxy.proxy.http import HttpUpstreamTransport
from mcpxy_proxy.storage.config_store import open_store


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


def test_token_transform_default_strategy():
    cfg = TokenTransformConfig()
    assert cfg.strategy == "static"


def test_token_transform_passthrough():
    cfg = TokenTransformConfig(strategy="passthrough")
    assert cfg.strategy == "passthrough"


def test_token_transform_map_with_fallback():
    cfg = TokenTransformConfig(strategy="map", fallback_on_missing_map="static")
    assert cfg.fallback_on_missing_map == "static"


def test_http_upstream_with_transform():
    cfg = HttpUpstreamConfig(
        type="http",
        url="http://upstream:9000/mcp",
        token_transform=TokenTransformConfig(strategy="passthrough"),
    )
    assert cfg.token_transform is not None
    assert cfg.token_transform.strategy == "passthrough"


def test_http_upstream_without_transform():
    cfg = HttpUpstreamConfig(type="http", url="http://upstream:9000/mcp")
    assert cfg.token_transform is None


# ---------------------------------------------------------------------------
# Token mapping CRUD tests
# ---------------------------------------------------------------------------


def _make_store(tmp_path):
    fernet = Fernet(Fernet.generate_key())
    return open_store(
        f"sqlite:///{tmp_path / 'test.db'}",
        fernet=fernet,
        state_dir=str(tmp_path),
    )


def test_upsert_and_get_token_mapping(tmp_path):
    store = _make_store(tmp_path)
    store.create_user(email="dev@x.com", provider="local", role="member", activated=True)
    user = store.get_user_by_email("dev@x.com")

    mapping = store.upsert_token_mapping(
        upstream="github",
        user_id=user.id,
        upstream_token="ghp_abc123",
        description="GitHub PAT for dev",
    )
    assert mapping.upstream == "github"
    assert mapping.user_id == user.id
    assert mapping.upstream_token == "ghp_abc123"

    fetched = store.get_token_mapping(upstream="github", user_id=user.id)
    assert fetched is not None
    assert fetched.upstream_token == "ghp_abc123"


def test_upsert_overwrites_existing(tmp_path):
    store = _make_store(tmp_path)
    store.create_user(email="dev@x.com", provider="local", role="member", activated=True)
    user = store.get_user_by_email("dev@x.com")

    store.upsert_token_mapping(upstream="gh", user_id=user.id, upstream_token="old")
    store.upsert_token_mapping(upstream="gh", user_id=user.id, upstream_token="new")

    fetched = store.get_token_mapping(upstream="gh", user_id=user.id)
    assert fetched.upstream_token == "new"


def test_list_token_mappings_filtered(tmp_path):
    store = _make_store(tmp_path)
    u = store.create_user(email="dev@x.com", provider="local", activated=True)
    store.upsert_token_mapping(upstream="a", user_id=u.id, upstream_token="tok-a")
    store.upsert_token_mapping(upstream="b", user_id=u.id, upstream_token="tok-b")

    all_mappings = store.list_token_mappings()
    assert len(all_mappings) == 2

    filtered = store.list_token_mappings(upstream="a")
    assert len(filtered) == 1
    assert filtered[0].upstream == "a"


def test_delete_token_mapping(tmp_path):
    store = _make_store(tmp_path)
    u = store.create_user(email="dev@x.com", provider="local", activated=True)
    m = store.upsert_token_mapping(upstream="x", user_id=u.id, upstream_token="t")
    assert store.delete_token_mapping(m.id) is True
    assert store.get_token_mapping(upstream="x", user_id=u.id) is None
    assert store.delete_token_mapping(999) is False


def test_token_mapping_public_dict_redacts_token(tmp_path):
    store = _make_store(tmp_path)
    u = store.create_user(email="dev@x.com", provider="local", activated=True)
    m = store.upsert_token_mapping(
        upstream="x", user_id=u.id, upstream_token="secret-value"
    )
    pub = m.to_public_dict()
    assert "upstream_token" not in pub
    assert "token_preview" in pub


def test_get_missing_mapping_returns_none(tmp_path):
    store = _make_store(tmp_path)
    assert store.get_token_mapping(upstream="x", user_id=999) is None


# ---------------------------------------------------------------------------
# Transport-level transform logic tests
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, data):
        self._data = data
        self.content = json.dumps(data).encode() if data else b""

    def json(self):
        return self._data


def test_transform_static_returns_none():
    transport = HttpUpstreamTransport("test", {
        "url": "http://upstream/mcp",
        "token_transform": {"strategy": "static"},
    })
    ctx = RequestContext(user_id=1, email="a@x.com", incoming_bearer="my-token")
    assert transport._resolve_transform_headers(ctx) is None


def test_transform_passthrough_forwards_bearer():
    transport = HttpUpstreamTransport("test", {
        "url": "http://upstream/mcp",
        "token_transform": {"strategy": "passthrough"},
    })
    ctx = RequestContext(user_id=1, email="a@x.com", incoming_bearer="client-tok")
    headers = transport._resolve_transform_headers(ctx)
    assert headers == {"Authorization": "Bearer client-tok"}


def test_transform_passthrough_no_bearer():
    transport = HttpUpstreamTransport("test", {
        "url": "http://upstream/mcp",
        "token_transform": {"strategy": "passthrough"},
    })
    ctx = RequestContext(user_id=1, email="a@x.com", incoming_bearer=None)
    assert transport._resolve_transform_headers(ctx) is None


def test_transform_header_inject():
    transport = HttpUpstreamTransport("test", {
        "url": "http://upstream/mcp",
        "token_transform": {"strategy": "header_inject", "inject_header": "X-User"},
    })
    ctx = RequestContext(user_id=1, email="a@x.com", incoming_bearer="tok")
    headers = transport._resolve_transform_headers(ctx)
    assert headers == {"X-User": "a@x.com"}


def test_transform_map_with_match(tmp_path):
    store = _make_store(tmp_path)
    u = store.create_user(email="dev@x.com", provider="local", activated=True)
    store.upsert_token_mapping(upstream="test", user_id=u.id, upstream_token="upstream-secret")

    transport = HttpUpstreamTransport("test", {
        "url": "http://upstream/mcp",
        "token_transform": {"strategy": "map"},
        "_config_store": store,
    })
    ctx = RequestContext(user_id=u.id, email="dev@x.com", incoming_bearer="proxy-tok")
    headers = transport._resolve_transform_headers(ctx)
    assert headers == {"Authorization": "Bearer upstream-secret"}


def test_transform_map_missing_denies(tmp_path):
    store = _make_store(tmp_path)
    transport = HttpUpstreamTransport("test", {
        "url": "http://upstream/mcp",
        "token_transform": {"strategy": "map", "fallback_on_missing_map": "deny"},
        "_config_store": store,
    })
    ctx = RequestContext(user_id=999, email="nobody@x.com", incoming_bearer="tok")
    headers = transport._resolve_transform_headers(ctx)
    assert headers == {}  # empty dict signals deny


def test_transform_map_missing_fallback_static(tmp_path):
    store = _make_store(tmp_path)
    transport = HttpUpstreamTransport("test", {
        "url": "http://upstream/mcp",
        "token_transform": {"strategy": "map", "fallback_on_missing_map": "static"},
        "_config_store": store,
    })
    ctx = RequestContext(user_id=999, email="nobody@x.com", incoming_bearer="tok")
    headers = transport._resolve_transform_headers(ctx)
    assert headers is None  # None = use static auth


def test_transform_none_config():
    """No token_transform configured → always returns None."""
    transport = HttpUpstreamTransport("test", {"url": "http://upstream/mcp"})
    ctx = RequestContext(user_id=1, email="a@x.com", incoming_bearer="tok")
    assert transport._resolve_transform_headers(ctx) is None
