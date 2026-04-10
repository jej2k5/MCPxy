"""Tests for the invite lifecycle."""

from __future__ import annotations

from unittest.mock import MagicMock

from cryptography.fernet import Fernet

from mcp_proxy.authn.users import accept_invite, invite_user
from mcp_proxy.storage.config_store import open_store


def _make_store(tmp_path):
    fernet = Fernet(Fernet.generate_key())
    return open_store(
        f"sqlite:///{tmp_path / 'test.db'}",
        fernet=fernet,
        state_dir=str(tmp_path),
    )


def _mock_manager():
    from authy import hash_password

    mgr = MagicMock()
    mgr.hash_password = hash_password
    return mgr


def test_invite_and_accept(tmp_path):
    store = _make_store(tmp_path)
    manager = _mock_manager()
    record, plaintext = invite_user(
        store, email="new@x.com", role="member", invited_by_id=None,
    )
    assert record.email == "new@x.com"
    assert len(plaintext) > 20

    user = accept_invite(
        store,
        token_plaintext=plaintext,
        password="securepassword123",
        name="New User",
        manager=manager,
    )
    assert user is not None
    assert user.email == "new@x.com"
    assert user.role == "member"
    assert user.activated_at is not None


def test_accept_with_wrong_token(tmp_path):
    store = _make_store(tmp_path)
    manager = _mock_manager()
    invite_user(store, email="new@x.com", role="member")

    user = accept_invite(
        store,
        token_plaintext="totally-wrong-token",
        password="secure123",
        manager=manager,
    )
    assert user is None


def test_double_accept_fails(tmp_path):
    store = _make_store(tmp_path)
    manager = _mock_manager()
    _record, plaintext = invite_user(store, email="new@x.com", role="member")

    user1 = accept_invite(
        store, token_plaintext=plaintext, password="secure123", manager=manager,
    )
    assert user1 is not None

    # Second accept should fail (invite already consumed)
    user2 = accept_invite(
        store, token_plaintext=plaintext, password="secure123", manager=manager,
    )
    assert user2 is None


def test_list_invites(tmp_path):
    store = _make_store(tmp_path)
    invite_user(store, email="a@x.com", role="member")
    invite_user(store, email="b@x.com", role="admin")
    invites = store.list_invites()
    assert len(invites) == 2
