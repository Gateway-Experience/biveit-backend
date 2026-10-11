# User typing. Two kinds live here (see dto/__init__):
#  - The User INTERFACE (TypedDict) over the users-row dict the repo returns and auth/controllers
#    read by key (user["id"], user["username"], ...). Notably `password_hash` is NOT here -- the repo never
#    returns it except through the one deliberate auth lookup (users.get_auth_by_username).
#  - The request CONTRACTS (Pydantic) validating untrusted HTTP bodies.

from typing import Any, TypedDict

from pydantic import BaseModel


class User(TypedDict):
    """A users-row interface over the dict the repo returns. `password_hash` is deliberately absent --
    it's left out of every read but the auth lookup."""

    id: int
    username: str | None  # the email; None for guests
    user_type: str  # UserType -- USER | GUEST
    full_name: str | None
    profile_picture: str | None
    platform: str | None  # UserPlatform -- WEB | GOOGLE; None for guests
    platform_id: str | None  # WEB: the username; GOOGLE: Google's `sub` (not the email)
    tags: list[str]
    meta: dict[str, Any]  # app-level data, incl. FE-sent profile keys with no column of their own
    created_at: Any  # datetime
    updated_at: Any  # datetime
    deleted_at: Any  # datetime | None -- always None on anything a read returns (reads skip deleted rows)


class RegisterRequest(BaseModel):
    """POST /user/register body. `user_data` is the free-form profile blob the FE sends alongside the
    credentials. Only full_name/profile_picture become columns; anything else is kept under `meta`
    (services/users._to_profile) -- a client can never set a privileged column through it."""

    username: str
    password: str
    user_data: dict[str, Any] = {}


class AuthenticateRequest(BaseModel):
    username: str
    password: str


class PromoteRequest(BaseModel):
    """POST /user/promote body: turn the CALLING guest into a credentialed USER in place, keeping the
    same id (so whatever the guest accumulated stays attached to them). No guest_id -- which guest is
    taken from the caller's session."""

    username: str
    password: str
    user_data: dict[str, Any] = {}


class UpdateMeRequest(BaseModel):
    """PATCH /me body. Deliberately NARROW -- this is a self-service edit from the browser, so it lists
    only the fields a user may change about themselves. Anything not listed is rejected (extra="forbid")
    rather than silently dropped, so a FE sending a field it thinks is being saved finds out. Widen this
    as the profile grows; never widen it to privileged fields (user_type, tags, deleted_at)."""

    model_config = {"extra": "forbid"}

    full_name: str | None = None
    profile_picture: str | None = None
