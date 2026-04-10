"""Tests for the user management CRUD in ConfigStore and authn.users."""

from __future__ import annotations

from cryptography.fernet import Fernet

from mcp_proxy.storage.config_store import open_store


def _make_store(tmp_path):
    fernet = Fernet(Fernet.generate_key())
    return open_store(
        f"sqlite:///{tmp_path / 'test.db'}",
        fernet=fernet,
        state_dir=str(tmp_path),
    )


def test_create_and_get_user(tmp_path):
    store = _make_store(tmp_path)
    user = store.create_user(
        email="alice@example.com",
        name="Alice",
        provider="local",
        role="admin",
        password_hash="$2b$12$fakehash",
        activated=True,
    )
    assert user.id > 0
    assert user.email == "alice@example.com"
    assert user.role == "admin"
    assert user.activated_at is not None

    fetched = store.get_user(user.id)
    assert fetched is not None
    assert fetched.email == "alice@example.com"


def test_get_user_by_email(tmp_path):
    store = _make_store(tmp_path)
    store.create_user(email="bob@example.com", provider="local", role="member")
    found = store.get_user_by_email("bob@example.com")
    assert found is not None
    assert found.email == "bob@example.com"
    assert store.get_user_by_email("nobody@example.com") is None


def test_get_user_by_provider_subject(tmp_path):
    store = _make_store(tmp_path)
    store.create_user(
        email="carol@example.com",
        provider="google",
        provider_subject="google-12345",
    )
    found = store.get_user_by_provider_subject("google", "google-12345")
    assert found is not None
    assert found.email == "carol@example.com"
    assert store.get_user_by_provider_subject("google", "wrong") is None


def test_list_users_excludes_disabled(tmp_path):
    store = _make_store(tmp_path)
    u1 = store.create_user(email="a@x.com", provider="local")
    store.create_user(email="b@x.com", provider="local")
    store.disable_user(u1.id)
    active = store.list_users()
    assert len(active) == 1
    assert active[0].email == "b@x.com"
    all_users = store.list_users(include_disabled=True)
    assert len(all_users) == 2


def test_count_admins(tmp_path):
    store = _make_store(tmp_path)
    assert store.count_admins() == 0
    store.create_user(email="a@x.com", provider="local", role="admin")
    assert store.count_admins() == 1
    u2 = store.create_user(email="b@x.com", provider="local", role="admin")
    assert store.count_admins() == 2
    store.disable_user(u2.id)
    assert store.count_admins() == 1


def test_update_user_role(tmp_path):
    store = _make_store(tmp_path)
    user = store.create_user(email="d@x.com", provider="local", role="member")
    store.update_user_role(user.id, "admin")
    assert store.get_user(user.id).role == "admin"


def test_delete_user(tmp_path):
    store = _make_store(tmp_path)
    user = store.create_user(email="e@x.com", provider="local")
    assert store.delete_user(user.id) is True
    assert store.get_user(user.id) is None
    assert store.delete_user(999) is False


def test_user_to_public_dict_has_no_password(tmp_path):
    store = _make_store(tmp_path)
    user = store.create_user(
        email="f@x.com", provider="local", password_hash="secret"
    )
    d = user.to_public_dict()
    assert "password_hash" not in d
    assert d["email"] == "f@x.com"


def test_password_hash_round_trip(tmp_path):
    from authy import hash_password, verify_password

    pw = "correct-horse-battery-staple"
    hashed = hash_password(pw)
    assert verify_password(pw, hashed) is True
    assert verify_password("wrong", hashed) is False
