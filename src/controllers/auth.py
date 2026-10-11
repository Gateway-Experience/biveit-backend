# Intentions: Google login as a server-side OIDC redirect flow (authorization code + PKCE), and logout.
#   GET  /auth/google/login     (public) full-page nav from the FE's button; bounces to Google
#   GET  /auth/google/callback  (public) Google sends the browser back here; we sign them in and
#                                         bounce to the FE with the session cookie set
#   POST /auth/logout           (public) clears the session cookie
#
# Why a redirect flow: the old endpoints took a Google profile the FE decoded WITHOUT verifying, so
# anyone could log in as any email. Here the BE swaps the code for tokens itself (with the client
# secret) and Authlib verifies the ID token, so the claims we act on are Google's, not the browser's.
#
# Between the two legs, request.session (a signed cookie, SessionMiddleware) carries Authlib's
# state/nonce/PKCE verifier plus our own `next` and guest_id. The guest_id comes from the caller's
# session at /login -- never from a query param.
#
# NEVER A 500 PAGE: the callback runs as a top-level browser navigation, so every failure redirects to
# FRONTEND_URL/login?error=<GoogleLoginError> instead. Expected failures (user cancelled, stale or
# forged state, unverified or already-registered email) aren't logged as errors; anything else is.
# References: src/utils/oauth.py (client registration), src/services/users.signin_google.

from urllib.parse import urlencode

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse

from src.config import settings
from src.constants import GoogleLoginError, UserType
from src.dto.users import User
from src.services import users as user_service
from src.utils.auth import clear_access_cookie, get_current_user_optional, set_access_cookie
from src.utils.logging import log_http_error
from src.utils.oauth import oauth

router = APIRouter()

# request.session key holding what /login hands to /callback (next + guest_id).
_LOGIN_STATE_KEY = "google_login"

# Authlib's OAuthError.error values we treat as expected outcomes rather than faults.
_EXPECTED_OAUTH_ERRORS = {
    "access_denied": GoogleLoginError.ACCESS_DENIED,
    "mismatching_state": GoogleLoginError.INVALID_STATE,
}


def _to_safe_next(next_path: str | None) -> str:
    """[Pure] The post-login FE path, or "/" if `next_path` isn't a plain same-origin path. Blocks open
    redirects: absolute URLs, protocol-relative `//evil.com`, backslashes (`/\\evil.com`, which browsers
    read as `//`), and whitespace/control chars (browsers strip tabs/newlines, so `/\\t/evil.com` would
    become `//evil.com`)."""
    if not next_path:
        return "/"
    is_plain_path = (
        next_path.startswith("/")
        and not next_path.startswith("//")
        and "\\" not in next_path
        and not any(char.isspace() or ord(char) < 0x20 for char in next_path)
    )
    return next_path if is_plain_path else "/"


def _redirect_to_frontend_error(code: GoogleLoginError) -> RedirectResponse:
    return RedirectResponse(f"{settings.frontend_url}/login?{urlencode({'error': code})}", status_code=302)


@router.get("/auth/google/login")
async def login_google(
    request: Request,
    next_path: str | None = Query(None, alias="next"),
    user: User | None = Depends(get_current_user_optional),
) -> Response:
    """Start Google login: remember where to land and which guest (if the caller is one) to promote,
    then redirect to Google's consent screen."""
    if not settings.is_google_login_enabled():
        return _redirect_to_frontend_error(GoogleLoginError.DISABLED)
    try:
        request.session[_LOGIN_STATE_KEY] = {
            "next": _to_safe_next(next_path),
            "guest_id": user["id"] if user and user["user_type"] == UserType.GUEST else None,
        }
        # redirect_uri passed explicitly: it must match the registered URI byte for byte, and deriving it
        # from the request breaks behind a TLS-terminating proxy (http vs https).
        return await oauth.google.authorize_redirect(request, settings.google_redirect_uri)
    except Exception as error:
        log_http_error(error, request)
        return _redirect_to_frontend_error(GoogleLoginError.INTERNAL_ERROR)


@router.get("/auth/google/callback")
async def google_callback(request: Request) -> Response:
    """Finish Google login: Authlib checks state, exchanges the code, and verifies the ID token; we then
    sign the person in (see services/users.signin_google) and send them to the FE with the session
    cookie. Only Authlib-verified claims are used -- nothing from this request's query reaches the db."""
    # The kill switch must hold here too: with no session secret, session contents can't be trusted.
    if not settings.is_google_login_enabled():
        return _redirect_to_frontend_error(GoogleLoginError.DISABLED)
    login_state = request.session.pop(_LOGIN_STATE_KEY, None) or {}
    try:
        token = await oauth.google.authorize_access_token(request)
        result = await user_service.signin_google(token["userinfo"], login_state.get("guest_id"))
    except OAuthError as error:
        expected_code = _EXPECTED_OAUTH_ERRORS.get(error.error)
        if expected_code is None:
            log_http_error(error, request)
        return _redirect_to_frontend_error(expected_code or GoogleLoginError.GOOGLE_AUTH_FAILED)
    except user_service.GoogleEmailNotVerified:
        return _redirect_to_frontend_error(GoogleLoginError.EMAIL_NOT_VERIFIED)
    except user_service.GoogleEmailTaken:
        return _redirect_to_frontend_error(GoogleLoginError.EMAIL_TAKEN)
    except Exception as error:
        log_http_error(error, request)
        return _redirect_to_frontend_error(GoogleLoginError.INTERNAL_ERROR)

    # `next` was vetted at /login; re-vet anyway, since the session is the only thing standing between
    # a tampered value and an open redirect.
    response = RedirectResponse(f"{settings.frontend_url}{_to_safe_next(login_state.get('next'))}", status_code=302)
    set_access_cookie(response, result["token"])
    return response


@router.post("/auth/logout")
async def logout(response: Response) -> dict:
    """Drop the session cookie. The JWT itself stays valid until it expires (it's stateless) -- this
    ends the browser's session, it doesn't revoke the token."""
    clear_access_cookie(response)
    return {"ok": True}
