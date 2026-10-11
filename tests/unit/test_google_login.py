# Google login redirect-flow tests (controllers/auth). The two Google round trips are mocked
# (authorize_redirect / the code exchange) and so is the users service; what's under test is the
# controller: session hand-off between the legs, open-redirect protection, guest resolution, and that
# every failure lands on the FE's /login?error= page rather than a 500. The state and access_denied
# cases run Authlib's REAL checks -- those need no network.

import pytest
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import settings
from src.constants import ACCESS_TOKEN_COOKIE, UserType
from src.controllers import auth as auth_controller
from src.services import users as user_service
from src.utils.auth import get_current_user_optional
from src.utils.oauth import oauth

_FRONTEND = "http://fe.test"
_CLAIMS = {"sub": "1234567890", "email": "a@b.c", "email_verified": True, "name": "Alice"}


@pytest.fixture
def configure(monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", _FRONTEND)
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(settings, "session_secret", "session-secret-of-a-realistic-length-01")


@pytest.fixture
def google(monkeypatch, configure):
    """Mocks both Google round trips and the service; records what the service was called with."""
    calls = {}

    async def fake_authorize_redirect(request, redirect_uri):
        # Authlib would stash state/nonce in the session here; the callback mock below doesn't check it.
        return RedirectResponse("https://accounts.google.com/o/oauth2/auth", status_code=302)

    async def fake_authorize_access_token(request):
        return {"userinfo": _CLAIMS}

    async def fake_signin_google(claims, guest_id=None):
        calls["claims"] = claims
        calls["guest_id"] = guest_id
        return {"user": {"id": 1}, "token": "minted-token", "is_new": True}

    monkeypatch.setattr(oauth.google, "authorize_redirect", fake_authorize_redirect)
    monkeypatch.setattr(oauth.google, "authorize_access_token", fake_authorize_access_token)
    monkeypatch.setattr(user_service, "signin_google", fake_signin_google)
    return calls


def _client(caller=None) -> TestClient:
    # A fresh app so SessionMiddleware picks up the patched secret. `caller` overrides who's logged in.
    app = create_app()
    app.dependency_overrides[get_current_user_optional] = lambda: caller
    return TestClient(app, follow_redirects=False)


def _error_location(code: str) -> str:
    return f"{_FRONTEND}/login?error={code}"


# --- _to_safe_next: the open-redirect gate ---


@pytest.mark.parametrize("next_path", ["/projects/42", "/", "/a?b=c#d"])
def test_safe_next_keeps_plain_paths(next_path):
    assert auth_controller._to_safe_next(next_path) == next_path


@pytest.mark.parametrize(
    "next_path",
    [None, "", "https://evil.com", "//evil.com", "/\\evil.com", "/\t/evil.com", "evil.com", "javascript:alert(1)"],
)
def test_safe_next_rejects_anything_else(next_path):
    assert auth_controller._to_safe_next(next_path) == "/"


# --- the happy path ---


def test_full_flow_signs_in_and_lands_on_next(google):
    client = _client()
    login = client.get("/api/auth/google/login", params={"next": "/projects/42"})
    assert login.status_code == 302
    assert login.headers["location"].startswith("https://accounts.google.com/")

    # Extra query params on the callback must not reach the service -- only Authlib's verified claims.
    callback = client.get("/api/auth/google/callback", params={"code": "c", "state": "s", "email": "evil@x.com"})
    assert callback.status_code == 302
    assert callback.headers["location"] == f"{_FRONTEND}/projects/42"
    assert google["claims"] == _CLAIMS
    assert callback.cookies.get(ACCESS_TOKEN_COOKIE) == "minted-token"
    assert "httponly" in callback.headers["set-cookie"].lower()


def test_foreign_next_falls_back_to_root(google):
    client = _client()
    client.get("/api/auth/google/login", params={"next": "https://evil.com/phish"})
    callback = client.get("/api/auth/google/callback", params={"code": "c", "state": "s"})
    assert callback.headers["location"] == f"{_FRONTEND}/"


# --- guest resolution ---


def test_guest_id_comes_from_the_session(google):
    client = _client(caller={"id": 5, "user_type": UserType.GUEST})
    client.get("/api/auth/google/login")
    client.get("/api/auth/google/callback", params={"code": "c", "state": "s"})
    assert google["guest_id"] == 5


def test_guest_id_query_param_is_ignored(google):
    client = _client()
    client.get("/api/auth/google/login", params={"guest_id": 99})
    client.get("/api/auth/google/callback", params={"code": "c", "state": "s"})
    assert google["guest_id"] is None


def test_a_real_user_is_not_passed_as_guest(google):
    client = _client(caller={"id": 5, "user_type": UserType.USER})
    client.get("/api/auth/google/login")
    client.get("/api/auth/google/callback", params={"code": "c", "state": "s"})
    assert google["guest_id"] is None


# --- failures always redirect to the FE ---


def test_disabled_when_unconfigured(monkeypatch, configure):
    monkeypatch.setattr(settings, "google_client_id", "")
    client = _client()
    assert client.get("/api/auth/google/login").headers["location"] == _error_location("GOOGLE_LOGIN_DISABLED")
    callback = client.get("/api/auth/google/callback", params={"code": "c", "state": "s"})
    assert callback.headers["location"] == _error_location("GOOGLE_LOGIN_DISABLED")


def test_missing_state_is_rejected(configure):
    # Real Authlib: a callback with no login leg before it has no state in the session.
    callback = _client().get("/api/auth/google/callback", params={"code": "c", "state": "forged"})
    assert callback.headers["location"] == _error_location("INVALID_STATE")


def test_user_cancelling_on_google(configure):
    callback = _client().get("/api/auth/google/callback", params={"error": "access_denied"})
    assert callback.headers["location"] == _error_location("ACCESS_DENIED")


def test_unverified_email(monkeypatch, google):
    async def raise_unverified(claims, guest_id=None):
        raise user_service.GoogleEmailNotVerified("nope")

    monkeypatch.setattr(user_service, "signin_google", raise_unverified)
    client = _client()
    client.get("/api/auth/google/login")
    callback = client.get("/api/auth/google/callback", params={"code": "c", "state": "s"})
    assert callback.headers["location"] == _error_location("EMAIL_NOT_VERIFIED")
    assert ACCESS_TOKEN_COOKIE not in callback.cookies


def test_registered_email(monkeypatch, google):
    async def raise_taken(claims, guest_id=None):
        raise user_service.GoogleEmailTaken("taken")

    monkeypatch.setattr(user_service, "signin_google", raise_taken)
    client = _client()
    client.get("/api/auth/google/login")
    callback = client.get("/api/auth/google/callback", params={"code": "c", "state": "s"})
    assert callback.headers["location"] == _error_location("EMAIL_TAKEN")
    assert ACCESS_TOKEN_COOKIE not in callback.cookies


def test_unexpected_failure_still_redirects(monkeypatch, google):
    async def explode(claims, guest_id=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(user_service, "signin_google", explode)
    client = _client()
    client.get("/api/auth/google/login")
    callback = client.get("/api/auth/google/callback", params={"code": "c", "state": "s"})
    assert callback.status_code == 302
    assert callback.headers["location"] == _error_location("INTERNAL_ERROR")


def test_logout_clears_the_cookie(configure):
    response = _client().post("/api/auth/logout")
    assert response.status_code == 200
    assert f'{ACCESS_TOKEN_COOKIE}=""' in response.headers["set-cookie"]
