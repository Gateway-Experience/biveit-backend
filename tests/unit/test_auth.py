# Auth-dependency tests: the token round trip, and the four gates in utils/auth. No db and no HTTP --
# the user lookup is monkeypatched, so these test the AUTH decisions in isolation.

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from src.utils import auth

# HS256 wants a key of at least 32 bytes (RFC 7518); anything shorter makes PyJWT warn on every
# encode/decode. Use realistic-length secrets so the tests exercise the same shape a deployment does.
_SECRET = "test-jwt-secret-of-a-realistic-length-0001"
_OTHER_SECRET = "a-different-secret-of-realistic-length-0002"


def _creds(token: str | None):
    # What the route's _bearer dependency resolves to (None = no or malformed header).
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token is not None else None


# --- Token minting + decoding ---


def test_mint_and_decode_round_trip(monkeypatch):
    monkeypatch.setattr(auth.settings, "jwt_secret", _SECRET)
    token = auth.mint_access_token(1, "alice", "USER")
    assert auth._decode_subject(token) == 1


def test_mint_refuses_without_secret(monkeypatch):
    # Fail closed at BOTH ends: you can't mint what you'd never be able to verify.
    monkeypatch.setattr(auth.settings, "jwt_secret", "")
    with pytest.raises(RuntimeError):
        auth.mint_access_token(1, "alice", "USER")


def test_decode_rejects_foreign_signature(monkeypatch):
    monkeypatch.setattr(auth.settings, "jwt_secret", _SECRET)
    forged = jwt.encode({"sub": "whoever"}, _OTHER_SECRET, algorithm="HS256")
    assert auth._decode_subject(forged) is None


def test_decode_returns_none_without_secret(monkeypatch):
    monkeypatch.setattr(auth.settings, "jwt_secret", "")
    assert auth._decode_subject("anything") is None


# --- get_current_user: strict, 401 on anything wrong ---


async def test_get_current_user_returns_the_record(monkeypatch):
    monkeypatch.setattr(auth.settings, "jwt_secret", _SECRET)
    user = {"id": 1, "username": "alice"}

    async def fake_get(user_id):
        return user if user_id == 1 else None

    monkeypatch.setattr(auth.users, "get", fake_get)
    token = auth.mint_access_token(1, "alice", "USER")
    assert await auth.get_current_user(_creds(token), None) == user


async def test_get_current_user_401_without_credentials():
    with pytest.raises(HTTPException) as ei:
        await auth.get_current_user(_creds(None), None)
    assert ei.value.status_code == 401


async def test_get_current_user_401_when_user_is_gone(monkeypatch):
    # A structurally valid token for a user who's since been deleted must not authenticate: the token
    # proves identity, the db row is the authority on whether that identity still exists.
    monkeypatch.setattr(auth.settings, "jwt_secret", _SECRET)

    async def fake_get(user_id):
        return None

    monkeypatch.setattr(auth.users, "get", fake_get)
    token = auth.mint_access_token(1, "alice", "USER")
    with pytest.raises(HTTPException) as ei:
        await auth.get_current_user(_creds(token), None)
    assert ei.value.status_code == 401


async def test_get_current_user_falls_back_to_the_session_cookie(monkeypatch):
    monkeypatch.setattr(auth.settings, "jwt_secret", _SECRET)
    user = {"id": 1, "username": "alice"}

    async def fake_get(user_id):
        return user if user_id == 1 else None

    monkeypatch.setattr(auth.users, "get", fake_get)
    token = auth.mint_access_token(1, "alice", "USER")
    assert await auth.get_current_user(None, token) == user


# --- get_current_user_optional: lenient, None instead of raising ---


async def test_optional_user_is_none_when_anonymous():
    assert await auth.get_current_user_optional(_creds(None), None) is None


async def test_optional_user_is_none_on_bad_token(monkeypatch):
    monkeypatch.setattr(auth.settings, "jwt_secret", _SECRET)
    assert await auth.get_current_user_optional(_creds("not-a-jwt"), None) is None


# --- The super-user secret: strict and lenient flavours ---


def test_require_superuser_accepts_the_secret(monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_token", "secret")
    assert auth.require_superuser(_creds("secret")) is None


def test_require_superuser_403_on_wrong_secret(monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_token", "secret")
    with pytest.raises(HTTPException) as ei:
        auth.require_superuser(_creds("nope"))
    assert ei.value.status_code == 403


def test_require_superuser_denies_when_unconfigured(monkeypatch):
    # Fail closed: an unset admin token must deny everyone, even a caller presenting one.
    monkeypatch.setattr(auth.settings, "admin_token", "")
    with pytest.raises(HTTPException) as ei:
        auth.require_superuser(_creds("anything"))
    assert ei.value.status_code == 403


def test_maybe_superuser_reports_without_raising(monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_token", "secret")
    assert auth.get_maybe_superuser(_creds("secret")) is True
    assert auth.get_maybe_superuser(_creds("nope")) is False
    assert auth.get_maybe_superuser(_creds(None)) is False
