# Password-reset tests: the token's one-time / expiry / can't-be-an-access-token guarantees, and the
# request side never revealing whether an email has an account. Repo, redis and email are monkeypatched.

from datetime import timedelta

import bcrypt
import jwt
import pytest

from src.config import settings
from src.constants import UserType
from src.repositories import users as users_repo
from src.services import users as user_service
from src.services.users import password_reset
from src.utils import auth, redis
from src.utils.common import drain_spawned

_SECRET = "test-jwt-secret-of-a-realistic-length-0001"
_OLD_HASH = bcrypt.hashpw(b"old-password", bcrypt.gensalt()).decode()


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", _SECRET)
    monkeypatch.setattr(settings, "env", "local")
    monkeypatch.setattr(settings, "email_from", "")


@pytest.fixture
def sent_emails(monkeypatch):
    """Captures what would've been emailed, and lets every throttle check pass."""
    sent = []

    async def fake_send(email, language, link):
        sent.append({"email": email, "language": language, "link": link})

    async def always_first(key, ttl_seconds):
        return True

    monkeypatch.setattr(password_reset, "_send_reset_email", fake_send)
    monkeypatch.setattr(redis, "set_if_absent", always_first)
    return sent


def _user(**overrides):
    return {"id": 7, "username": "alice@example.com", "user_type": UserType.USER, "language": "en", **overrides}


def _patch_account(monkeypatch, password_hash):
    """One account (id 7) whose stored hash is whatever `password_hash` currently holds."""
    state = {"password_hash": password_hash}

    async def fake_auth_by_username(username):
        return {"id": 7, "password_hash": state["password_hash"]} if username == "alice@example.com" else None

    async def fake_auth(user_id):
        return {"id": 7, "password_hash": state["password_hash"]} if user_id == 7 else None

    async def fake_get(user_id):
        return _user()

    async def fake_update(user_id, updates):
        state["password_hash"] = updates["password_hash"]
        return _user()

    monkeypatch.setattr(users_repo, "get_auth_by_username", fake_auth_by_username)
    monkeypatch.setattr(users_repo, "get_auth", fake_auth)
    monkeypatch.setattr(users_repo, "get", fake_get)
    monkeypatch.setattr(users_repo, "update", fake_update)
    return state


# --- requesting a reset ---


async def test_request_emails_a_link_for_a_password_account(monkeypatch, sent_emails):
    _patch_account(monkeypatch, _OLD_HASH)
    await user_service.request_password_reset("alice@example.com")
    await drain_spawned()
    assert len(sent_emails) == 1
    assert sent_emails[0]["link"].startswith(f"{settings.frontend_url}/reset-password?token=")


async def test_request_for_an_unknown_email_sends_nothing_and_does_not_raise(monkeypatch, sent_emails):
    _patch_account(monkeypatch, _OLD_HASH)
    await user_service.request_password_reset("nobody@example.com")
    await drain_spawned()
    assert sent_emails == []


async def test_request_for_a_google_only_account_sends_nothing(monkeypatch, sent_emails):
    _patch_account(monkeypatch, None)
    await user_service.request_password_reset("alice@example.com")
    await drain_spawned()
    assert sent_emails == []


async def test_request_is_throttled_per_address(monkeypatch, sent_emails):
    _patch_account(monkeypatch, _OLD_HASH)
    seen_keys = set()

    async def first_time_only(key, ttl_seconds):
        is_new = key not in seen_keys
        seen_keys.add(key)
        return is_new

    monkeypatch.setattr(redis, "set_if_absent", first_time_only)
    await user_service.request_password_reset("alice@example.com")
    await user_service.request_password_reset("alice@example.com")
    await drain_spawned()
    assert len(sent_emails) == 1


# --- using the link ---


async def test_reset_sets_the_new_password_and_signs_in(monkeypatch):
    state = _patch_account(monkeypatch, _OLD_HASH)
    token = password_reset.mint_reset_token(7, _OLD_HASH)
    result = await user_service.reset_password(token, "brand-new-password")
    assert result["user"]["id"] == 7
    assert result["token"]
    assert bcrypt.checkpw(b"brand-new-password", state["password_hash"].encode())


async def test_a_link_works_only_once(monkeypatch):
    _patch_account(monkeypatch, _OLD_HASH)
    token = password_reset.mint_reset_token(7, _OLD_HASH)
    await user_service.reset_password(token, "brand-new-password")
    with pytest.raises(user_service.InvalidResetToken):
        await user_service.reset_password(token, "another-password")


async def test_an_expired_link_is_rejected(monkeypatch):
    _patch_account(monkeypatch, _OLD_HASH)
    monkeypatch.setattr(password_reset, "PASSWORD_RESET_TOKEN_TTL_SECONDS", -1)
    token = password_reset.mint_reset_token(7, _OLD_HASH)
    with pytest.raises(user_service.InvalidResetToken):
        await user_service.reset_password(token, "brand-new-password")


async def test_an_access_token_is_not_a_reset_token(monkeypatch):
    _patch_account(monkeypatch, _OLD_HASH)
    access_token = auth.mint_access_token(7, "alice@example.com", UserType.USER)
    with pytest.raises(user_service.InvalidResetToken):
        await user_service.reset_password(access_token, "brand-new-password")


def test_a_reset_token_is_not_an_access_token():
    reset_token = password_reset.mint_reset_token(7, _OLD_HASH)
    assert auth._decode_subject(reset_token) is None


async def test_a_token_signed_with_the_plain_jwt_secret_is_rejected(monkeypatch):
    _patch_account(monkeypatch, _OLD_HASH)
    forged = jwt.encode(
        {
            "sub": "7",
            "pwd": password_reset._fingerprint(_OLD_HASH),
            "exp": password_reset.utc_now() + timedelta(minutes=5),
        },
        _SECRET,
        algorithm="HS256",
    )
    with pytest.raises(user_service.InvalidResetToken):
        await user_service.reset_password(forged, "brand-new-password")
