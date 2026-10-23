# User typing. Two kinds live here (see dto/__init__):
#  - The User INTERFACE (TypedDict) over the users-row dict the repo returns and auth/controllers
#    read by key (user["id"], user["username"], ...). Notably `password_hash` is NOT here -- the repo never
#    returns it except through the one deliberate auth lookup (users.get_auth_by_username).
#  - The request CONTRACTS (Pydantic) validating untrusted HTTP bodies.

from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field, StringConstraints, field_validator

from src.constants import MIN_PASSWORD_LENGTH, USER_NAME_MAX_LENGTH, UserLanguage


class User(TypedDict):
    """A users-row interface over the dict the repo returns. `password_hash` is deliberately absent --
    it's left out of every read but the auth lookup."""

    id: int
    hash: str  # random public handle; names the user's storage folder (see 0001_users.sql)
    username: str | None  # the email; None for guests
    user_type: str  # UserType -- USER | GUEST
    first_name: str | None  # None for guests (and accounts from before names were asked for)
    last_name: str | None  # can be empty: not everyone has one
    profile_picture: str | None
    language: str  # UserLanguage -- the FE's language picker
    platform: str | None  # UserPlatform -- WEB | GOOGLE; None for guests
    platform_id: str | None  # WEB: the username; GOOGLE: Google's `sub` (not the email)
    role: str | None  # UserRole -- None for a normal user; set by hand only (see 0001_users.sql)
    tags: list[str]
    meta: dict[str, Any]  # app-level data, incl. FE-sent profile keys with no column of their own
    created_at: Any  # datetime
    updated_at: Any  # datetime
    deleted_at: Any  # datetime | None -- always None on anything a read returns (reads skip deleted rows)
    has_password: bool  # derived from password_hash (which itself never leaves the repo); False for Google-only


# Signup asks for both names; last_name may be blank (not everyone has one). Whitespace is trimmed first.
_FirstName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=USER_NAME_MAX_LENGTH)]
_LastName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=USER_NAME_MAX_LENGTH)]


class RegisterRequest(BaseModel):
    """POST /user/register body. `user_data` is the free-form profile blob the FE sends alongside the
    credentials. Only the profile columns (services/users.to_profile) are written as such; anything else
    is kept under `meta` -- a client can never set a privileged column through it."""

    username: str
    password: str
    first_name: _FirstName
    last_name: _LastName = ""
    user_data: dict[str, Any] = {}


class AuthenticateRequest(BaseModel):
    username: str
    password: str


class ForgotPasswordRequest(BaseModel):
    """POST /user/forgot-password body. `email` is the username."""

    email: str


class ResetPasswordRequest(BaseModel):
    """POST /user/reset-password body: the token from the emailed link, and the new password."""

    token: str
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)


class PromoteRequest(BaseModel):
    """POST /user/promote body: turn the CALLING guest into a credentialed USER in place, keeping the
    same id (so whatever the guest accumulated stays attached to them). No guest_id -- which guest is
    taken from the caller's session."""

    username: str
    password: str
    first_name: _FirstName
    last_name: _LastName = ""
    user_data: dict[str, Any] = {}


class ChangePasswordRequest(BaseModel):
    """POST /me/password body. `current_password` is required for an account that has a password, and
    ignored for one that doesn't yet (Google-only) -- that's how such an account sets its first one."""

    current_password: str | None = None
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH)


class UpdateMeRequest(BaseModel):
    """PATCH /me body. Deliberately NARROW -- this is a self-service edit from the browser, so it lists
    only the fields a user may change about themselves. Anything not listed is rejected (extra="forbid")
    rather than silently dropped, so a FE sending a field it thinks is being saved finds out. Widen this
    as the profile grows; never widen it to privileged fields (user_type, tags, deleted_at)."""

    model_config = {"extra": "forbid"}

    first_name: _FirstName | None = None
    last_name: _LastName | None = None
    profile_picture: str | None = None
    # Sent by the FE's language picker whenever the user switches language.
    language: UserLanguage | None = None

    @field_validator("first_name", "last_name", "language")
    @classmethod
    def _refuse_null(cls, value: Any) -> Any:
        """Leaving a field out keeps it as is; an explicit null is refused (422) -- a user always has a
        language and a first name, and "no last name" is an empty one. (Validators only run on values
        actually sent, so the None defaults are unaffected.)"""
        if value is None:
            raise ValueError("can't be null")
        return value
