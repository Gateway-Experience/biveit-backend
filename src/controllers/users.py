# Intentions: the user + password-auth API.
#   GET   /me                    (auth)   the current user
#   PATCH /me                    (auth)   self-service profile edit
#   POST  /user/register         (public) sign up with username + password
#   POST  /user/authenticate     (public) log in
#   POST  /user/register-guest   (public) mint an anonymous guest account
#   POST  /user/promote          (guest)  the calling guest -> credentialed user, in place
# Google login lives in controllers/auth (a redirect flow, not a JSON endpoint).
# Protocol-only: parse, authenticate, delegate to services/users, map outcomes to HTTP. The guest
# lifecycle these endpoints expose is explained in services/users.
#
# Every endpoint that signs someone in also sets the session cookie (utils/auth.set_access_cookie), so
# all login methods land in the same session. The token is still returned in the body for the FE's
# current localStorage model -- drop it once the FE runs purely on the cookie.

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from src.dto.users import AuthenticateRequest, PromoteRequest, RegisterRequest, UpdateMeRequest, User
from src.services import users as user_service
from src.utils.auth import get_current_user, set_access_cookie
from src.utils.logging import log_http_error

router = APIRouter()


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)) -> User:
    """The authenticated caller's own record, returned BARE (not wrapped in an envelope) -- the FE
    stores the response as the user directly. No service hop: the auth dependency already loaded it."""
    return user


@router.patch("/me")
async def update_me(body: UpdateMeRequest, request: Request, user: User = Depends(get_current_user)) -> User:
    """Update the caller's own profile. The body model lists exactly which fields are self-editable and
    rejects anything else; exclude_unset means an omitted field is left alone rather than nulled."""
    try:
        # Bare, like GET /me -- the FE swaps the response straight into its user state.
        return await user_service.update(user["id"], body.model_dump(exclude_unset=True))
    except user_service.UserNotFound as error:
        # The user authenticated a moment ago, so this means they were deleted mid-request.
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/user/register")
async def register(body: RegisterRequest, request: Request, response: Response) -> dict:
    """Sign up with a username and password; returns the user and a token."""
    try:
        result = await user_service.register(body.username, body.password, body.user_data)
    except user_service.DuplicateUser as error:
        raise HTTPException(status_code=400, detail="DUPLICATE_USERNAME") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
    set_access_cookie(response, result["token"])
    return result


@router.post("/user/authenticate")
async def authenticate(body: AuthenticateRequest, request: Request, response: Response) -> dict:
    """Log in. A wrong password and an unknown username give the same 401 (no user enumeration)."""
    try:
        result = await user_service.authenticate(body.username, body.password)
    except user_service.InvalidCredentials as error:
        raise HTTPException(status_code=401, detail="INVALID_CREDENTIALS") from error
    except user_service.UserNotFound as error:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
    set_access_cookie(response, result["token"])
    return result


@router.post("/user/register-guest")
async def register_guest(request: Request, response: Response, body: dict | None = None) -> dict:
    """Mint an anonymous guest account. The body is free-form profile data, and optional -- the FE
    calls this with nothing at all on a first visit. The guest gets the session cookie too: that's how
    a later Google login (a full-page redirect, so no Authorization header) knows whom to promote."""
    try:
        result = await user_service.register_guest(body or {})
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
    set_access_cookie(response, result["token"])
    return result


@router.post("/user/promote")
async def promote(
    body: PromoteRequest, request: Request, response: Response, user: User = Depends(get_current_user)
) -> dict:
    """Promote the CALLING guest to a credentialed user, keeping their id and everything attached to it.
    Which guest comes from the session, never the body -- ids are guessable."""
    try:
        result = await user_service.promote_guest(user, body.username, body.password, body.user_data)
    except user_service.NotAGuest as error:
        raise HTTPException(status_code=409, detail="NOT_A_GUEST") from error
    except user_service.UserNotFound as error:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND") from error
    except user_service.DuplicateUser as error:
        raise HTTPException(status_code=400, detail="DUPLICATE_USERNAME") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
    set_access_cookie(response, result["token"])
    return result
