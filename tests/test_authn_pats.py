"""Tests for personal access token CRUD and verification."""

from __future__ import annotations

from cryptography.fernet import Fernet

from mcpxy_proxy.authn.users import mint_pat, verify_pat
from mcpxy_proxy.storage.config_store import open_store


def _make_store(tmp_path):
    fernet = Fernet(Fernet.generate_key())
    return open_store(
        f"sqlite:///{tmp_path / 'test.db'}",
        fernet=fernet,
        state_dir=str(tmp_path),
    )


def test_mint_and_verify_pat(tmp_path):
    store = _make_store(tmp_path)
    user = store.create_user(
        email="dev@x.com", provider="local", role="member", activated=True,
    )
    record, plaintext = mint_pat(store, user_id=user.id, name="test token")
    assert plaintext.startswith("mcpxy_pat_")
    assert record.token_prefix == plaintext[:8]

    # Verify succeeds
    owner = verify_pat(store, plaintext)
    assert owner is not None
    assert owner.id == user.id


def test_verify_wrong_plaintext(tmp_path):
    store = _make_store(tmp_path)
    user = store.create_user(
        email="dev@x.com", provider="local", role="member", activated=True,
    )
    mint_pat(store, user_id=user.id, name="tok")
    assert verify_pat(store, "mcpxy_pat_totally_wrong_token") is None


def test_verify_revoked_pat(tmp_path):
    store = _make_store(tmp_path)
    user = store.create_user(
        email="dev@x.com", provider="local", role="member", activated=True,
    )
    record, plaintext = mint_pat(store, user_id=user.id, name="tok")
    store.revoke_pat(record.id, user.id)
    assert verify_pat(store, plaintext) is None


def test_verify_disabled_user_pat(tmp_path):
    store = _make_store(tmp_path)
    user = store.create_user(
        email="dev@x.com", provider="local", role="member", activated=True,
    )
    _record, plaintext = mint_pat(store, user_id=user.id, name="tok")
    store.disable_user(user.id)
    assert verify_pat(store, plaintext) is None


def test_list_pats_for_user(tmp_path):
    store = _make_store(tmp_path)
    user = store.create_user(
        email="dev@x.com", provider="local", role="member", activated=True,
    )
    mint_pat(store, user_id=user.id, name="tok1")
    mint_pat(store, user_id=user.id, name="tok2")
    pats = store.list_pats_for_user(user.id)
    assert len(pats) == 2


def test_pat_not_a_pat(tmp_path):
    store = _make_store(tmp_path)
    assert verify_pat(store, "some-random-bearer-token") is None


def test_revoke_all_pats(tmp_path):
    store = _make_store(tmp_path)
    user = store.create_user(
        email="dev@x.com", provider="local", role="member", activated=True,
    )
    mint_pat(store, user_id=user.id, name="a")
    mint_pat(store, user_id=user.id, name="b")
    count = store.revoke_all_pats_for_user(user.id)
    assert count == 2
    assert len(store.list_pats_for_user(user.id)) == 0
