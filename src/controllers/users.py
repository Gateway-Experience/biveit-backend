# Intentions: the user + password-auth API.
#   GET   /me                    (auth)   the current user
#   PATCH /me                    (auth)   self-service profile edit
#   POST  /me/password           (auth)   change my password (or set a first one, for Google-only accounts)
#   GET   /me/journeys           (auth)   where I am in each of the FE's journeys (e.g. first-time intros)
#   POST  /me/journeys/{journey}/steps (auth) log a step I took in one
#   PUT   /me/avatar             (auth)   upload my profile picture (multipart `file`)
#   DELETE /me/avatar            (auth)   back to no profile picture
#   GET   /avatars/{hash}/{file} (public) an uploaded profile picture
#   POST  /user/register         (public) sign up with username + password
#   POST  /user/authenticate     (public) log in
#   POST  /user/register-guest   (public) mint an anonymous guest account
#   POST  /user/promote          (guest)  the calling guest -> credentialed user, in place
#   POST  /user/forgot-password  (public) email a password-reset link
#   POST  /user/reset-password   (public) set a new password from that link, and log in
# Google login lives in controllers/auth (a redirect flow, not a JSON endpoint).
# Protocol-only: parse, authenticate, delegate to services/users, map outcomes to HTTP. The guest
# lifecycle these endpoints expose is explained in services/users. Journeys are plain CRUD, so they go
# straight to the repo (repositories/user_journey_steps).
#
# Every endpoint that signs someone in also sets the session cookie (utils/auth.set_access_cookie), so
# all login methods land in the same session. The token is still returned in the body for the FE's
# current localStorage model -- drop it once the FE runs purely on the cookie.

import pydash
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse

from src.constants import AVATAR_MAX_BYTES, UserJourney, UserType
from src.dto.users import (
    AddJourneyStepRequest,
    AuthenticateRequest,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    PromoteRequest,
    RegisterRequest,
    ResetPasswordRequest,
    UpdateMeRequest,
    User,
    UserJourneyStep,
)
from src.repositories import user_journey_steps as user_journey_steps_repo
from src.services import users as user_service
from src.utils.auth import clear_signed_in_cookie, get_current_user, set_access_cookie, set_signed_in_cookie
from src.utils.logging import log_http_error

router = APIRouter()


@router.get("/me")
async def get_me(response: Response, user: User = Depends(get_current_user)) -> User:
    """The authenticated caller's own record, returned BARE (not wrapped in an envelope) -- the FE
    stores the response as the user directly. No service hop: the auth dependency already loaded it.
    Also refreshes the signed-in marker (utils/auth.set_signed_in_cookie), so sessions from before it
    existed get it on their next visit."""
    if user["user_type"] == UserType.GUEST:
        clear_signed_in_cookie(response)
    else:
        set_signed_in_cookie(response)
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


@router.post("/me/password", status_code=204)
async def change_password(
    body: ChangePasswordRequest, request: Request, user: User = Depends(get_current_user)
) -> None:
    """Change the caller's password. 400 WRONG_PASSWORD if the current one doesn't match. The session
    stays as it is -- the caller is the one who just changed it."""
    try:
        await user_service.change_password(user["id"], body.current_password, body.new_password)
    except user_service.WrongPassword as error:
        raise HTTPException(status_code=400, detail="WRONG_PASSWORD") from error
    except user_service.UserNotFound as error:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/me/journeys")
async def get_my_journeys(request: Request, user: User = Depends(get_current_user)) -> dict[str, UserJourneyStep]:
    """Where the caller is in each journey they've started: {journey: its latest step}. A journey that's
    missing hasn't been started."""
    try:
        return pydash.key_by(await user_journey_steps_repo.find_latest(user["id"]), "journey")
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/me/journeys/{journey}/steps", status_code=201)
async def add_journey_step(
    journey: UserJourney, body: AddJourneyStepRequest, request: Request, user: User = Depends(get_current_user)
) -> UserJourneyStep:
    """The caller took a step in one of the FE's journeys (e.g. step "finished" once they're
    through it, or skipped it); returns the step. An unknown journey is a 422."""
    try:
        return await user_journey_steps_repo.insert({"user_id": user["id"], "journey": journey, **body.model_dump()})
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.put("/me/avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...), user: User = Depends(get_current_user)) -> User:
    """Make the uploaded image the caller's profile picture; returns the user, bare like GET /me. 400
    INVALID_AVATAR for anything but a PNG / JPEG / GIF / WebP within the size limit."""
    try:
        # One byte over the limit is enough to know it's too big, without reading a huge file whole.
        return await user_service.save_avatar(user, await file.read(AVATAR_MAX_BYTES + 1))
    except user_service.InvalidAvatar as error:
        raise HTTPException(status_code=400, detail="INVALID_AVATAR") from error
    except user_service.UserNotFound as error:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.delete("/me/avatar")
async def delete_avatar(request: Request, user: User = Depends(get_current_user)) -> User:
    try:
        return await user_service.remove_avatar(user)
    except user_service.UserNotFound as error:
        raise HTTPException(status_code=404, detail="USER_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/avatars/{user_hash}/{file_name}")
async def get_avatar(user_hash: str, file_name: str, request: Request) -> FileResponse:
    """Public, like a Google picture URL: an <img> has to load it, and the random hash + file name make
    it unguessable. A file never changes (a new upload gets a new name), so it's cacheable for good.
    nosniff + sandbox: it was checked to be an image on the way in, and must never run as anything else."""
    try:
        path, mime = user_service.resolve_avatar_file(user_hash, file_name)
    except user_service.AvatarNotFound as error:
        raise HTTPException(status_code=404, detail="AVATAR_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
    return FileResponse(
        path,
        media_type=mime,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _with_names(body: RegisterRequest | PromoteRequest) -> dict:
    """[Pure] The signup's profile data, with the names (top-level, validated fields) folded in."""
    return {**body.user_data, "first_name": body.first_name, "last_name": body.last_name}


@router.post("/user/register")
async def register(body: RegisterRequest, request: Request, response: Response) -> dict:
    """Sign up with a username and password; returns the user and a token."""
    try:
        result = await user_service.register(body.username, body.password, _with_names(body))
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
    set_access_cookie(response, result["token"], is_guest=True)
    return result


@router.post("/user/promote")
async def promote(
    body: PromoteRequest, request: Request, response: Response, user: User = Depends(get_current_user)
) -> dict:
    """Promote the CALLING guest to a credentialed user, keeping their id and everything attached to it.
    Which guest comes from the session, never the body -- ids are guessable."""
    try:
        result = await user_service.promote_guest(user, body.username, body.password, _with_names(body))
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


@router.post("/user/forgot-password", status_code=204)
async def forgot_password(body: ForgotPasswordRequest, request: Request) -> None:
    """Email a reset link if the address has a password account. Always 204, account or not, so this
    can't be used to find out who has one."""
    try:
        await user_service.request_password_reset(body.email)
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/user/reset-password")
async def reset_password(body: ResetPasswordRequest, request: Request, response: Response) -> dict:
    """Set a new password from an emailed link, then sign the user in like any other login."""
    try:
        result = await user_service.reset_password(body.token, body.password)
    except user_service.InvalidResetToken as error:
        raise HTTPException(status_code=400, detail="INVALID_RESET_TOKEN") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
    set_access_cookie(response, result["token"])
    return result
