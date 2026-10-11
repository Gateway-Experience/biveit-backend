# Intentions: Google sign-in/sign-up from OIDC claims that Authlib has ALREADY verified (controllers/auth
# runs the redirect flow). This file never sees a raw request -- only trusted claims and the guest_id
# taken from the caller's own session.
#
# The wrinkle: the person may ALREADY have a Google-linked account from another device. Then there's
# nothing to promote -- we sign them into the existing account and the guest row is simply abandoned
# (merging the two is a product decision, not something to do implicitly). `is_new` tells the FE which.
#
# NO MERGING BY EMAIL: a Google sign-up whose email is already some account's username (password
# accounts use their email as username) FAILS rather than linking. Password signups aren't
# email-verified yet, so linking would hand a squatter's account to whoever proves the address -- or
# vice versa. Accepted for now: the real owner can't use Google until email verification lands and
# lets us resolve it properly.

from typing import Any

import pydash

from src.constants import UserPlatform, UserType
from src.repositories import users as users_repo
from src.services.users.users import issue_session


class GoogleEmailNotVerified(Exception):
    """Google vouched for the account but not for its email -- we won't link an address nobody proved
    they own."""


class GoogleEmailTaken(Exception):
    """A NEW Google account's email is already another account's username. Not merged -- see the module
    note."""


async def signin_google(claims: dict[str, Any], guest_id: int | None = None) -> dict:
    """Sign in (or sign up) with Google. Keyed on `sub` (stable per Google account; emails can change
    hands). Three outcomes:
      - a user with this sub exists -> sign into it, refreshing the picture (a guest is left alone);
      - else, `guest_id` names a live GUEST -> promote it in place;
      - else -> create a new USER.
    The username is the email, as for password accounts. It's set once at sign-up and NOT refreshed
    on later sign-ins -- if their Google address changes, the new one may already be someone else's. Raises GoogleEmailNotVerified if Google doesn't
    vouch for the email, GoogleEmailTaken if a new account's email is already a username. Only explicit
    fields are written -- nothing else from Google (tokens included) is kept."""
    if not claims.get("email_verified"):
        raise GoogleEmailNotVerified("google email not verified", {"sub": claims.get("sub")})

    existing = await users_repo.get_by_platform(UserPlatform.GOOGLE, claims["sub"])
    if existing:
        refreshed = await users_repo.update(existing["id"], {"profile_picture": claims.get("picture")})
        return {**issue_session(refreshed or existing), "is_new": False}

    # Checked up front for a clean error. A concurrent signup slipping past it still hits the unique
    # constraint (DuplicateUser) -- rare enough to leave as a generic failure.
    if await users_repo.get_by_username(claims["email"]):
        raise GoogleEmailTaken("email already registered", {"sub": claims["sub"]})

    guest = await users_repo.get(guest_id) if guest_id is not None else None
    signup = {
        "platform": UserPlatform.GOOGLE,
        "platform_id": claims["sub"],
        "full_name": claims.get("name"),
        "profile_picture": claims.get("picture"),
        "user_type": UserType.USER,
        "username": claims["email"],
    }
    if guest and guest["user_type"] == UserType.GUEST:
        promoted = await users_repo.update(guest["id"], {**signup, "tags": pydash.union(guest["tags"], ["GOOGLE"])})
        # None = the guest vanished between the read and the write; fall through and create instead.
        if promoted:
            return {**issue_session(promoted), "is_new": True}
    created = await users_repo.insert({**signup, "tags": ["GOOGLE"]})
    return {**issue_session(created), "is_new": True}
