# Intentions: the core of user accounts -- password signup/login, the guest lifecycle, profile edits,
# and the {user, token} response every sign-in path ends with. Google sign-in lives in google.py.
#
# CLIENT INPUT never picks columns: FE-sent profile data goes through to_profile, an allowlist. Without
# it a signup body could set platform_id (squatting someone's Google identity), user_type, tags, etc.
#
# PASSWORD HASHING goes through asyncio.to_thread: bcrypt is deliberately slow and CPU-bound, so a
# direct call would block the event loop -- every other in-flight request stalls for the duration of
# one login. Don't remove the thread hop.

import asyncio
import hmac
from typing import Any

import bcrypt
import pydash

from src.config import settings
from src.constants import UserPlatform, UserType
from src.dto.users import User
from src.repositories import users as users_repo
from src.repositories.users import DuplicateUser
from src.utils.auth import mint_access_token

__all__ = [
    "DuplicateUser",
    "InvalidCredentials",
    "NotAGuest",
    "UserNotFound",
    "WrongPassword",
    "authenticate",
    "change_password",
    "issue_session",
    "promote_guest",
    "register",
    "register_guest",
    "to_profile",
    "update",
]

# The profile fields a client may set about itself (mirrors dto.users.UpdateMeRequest). Everything else
# a client sends is kept under `meta`, never written to a column.
_CLIENT_PROFILE_FIELDS = ["first_name", "last_name", "profile_picture"]


class InvalidCredentials(Exception):
    """Login failed -- unknown username, or wrong password. Deliberately undifferentiated (no user
    enumeration). 401."""


class UserNotFound(Exception):
    """A user the caller named doesn't exist -- a master-password lookup that matched nothing, or a
    user deleted mid-request. 404."""


class WrongPassword(Exception):
    """A password change gave the wrong current password. 400 -- the caller is signed in, so unlike a
    failed login it's fine to say exactly what was wrong."""


class NotAGuest(Exception):
    """A promotion was attempted on an account that's already a USER. 409."""


def issue_session(user: User) -> dict:
    """[Pure-ish] The standard sign-in response: the user plus a freshly minted token. Every sign-in path
    (here and in google.py) ends here, so the shape the FE sees can't drift between them."""
    return {"user": user, "token": mint_access_token(user["id"], user.get("username"), user["user_type"])}


def to_profile(user_data: dict[str, Any] | None) -> dict[str, Any]:
    """[Pure] FE-sent profile data -> what we'll write: the allowlisted fields as columns, anything else
    kept (not dropped) under `meta`. Never lets a client choose a privileged column."""
    extras = pydash.omit(user_data or {}, *_CLIENT_PROFILE_FIELDS)
    return {**pydash.pick(user_data or {}, *_CLIENT_PROFILE_FIELDS), **({"meta": extras} if extras else {})}


async def hash_password(password: str) -> str:
    hashed = await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt())
    return hashed.decode()


async def _check_password(password: str, hashed: str) -> bool:
    """Constant-time compare against a stored hash, off the event loop."""
    return await asyncio.to_thread(bcrypt.checkpw, password.encode(), hashed.encode())


def _to_web_identity(username: str) -> dict[str, Any]:
    """[Pure] The columns that make an account a WEB (email + password) login. platform_id mirrors the
    username (see migrations/0001_users.sql), so they're always written together, here."""
    return {"username": username, "platform": UserPlatform.WEB, "platform_id": username}


async def register(username: str, password: str, user_data: dict[str, Any] | None = None) -> dict:
    """Create a credentialed USER and sign them in. Raises DuplicateUser if the username is taken."""
    user = await users_repo.insert(
        {
            **to_profile(user_data),
            **_to_web_identity(username),
            "password_hash": await hash_password(password),
            "user_type": UserType.USER,
        }
    )
    return issue_session(user)


async def register_guest(user_data: dict[str, Any] | None = None) -> dict:
    """Mint an anonymous GUEST and sign them in -- no username, no password. The token is a real one,
    so a guest is an ordinary authenticated caller everywhere downstream; only user_type says
    otherwise."""
    user = await users_repo.insert({**to_profile(user_data), "user_type": UserType.GUEST})
    return issue_session(user)


async def promote_guest(guest: User, username: str, password: str, user_data: dict[str, Any] | None = None) -> dict:
    """Turn the caller's own GUEST account into a credentialed USER in place -- same row, so everything
    already attached to them survives. Raises NotAGuest if they're already a USER, UserNotFound if the
    row vanished mid-request, DuplicateUser if the username is taken."""
    if guest["user_type"] != UserType.GUEST:
        raise NotAGuest("already a user", {"user_id": guest["id"]})
    user = await users_repo.update(
        guest["id"],
        {
            **to_profile(user_data),
            **_to_web_identity(username),
            "password_hash": await hash_password(password),
            "user_type": UserType.USER,
        },
    )
    if user is None:
        raise UserNotFound("no such guest", {"guest_id": guest["id"]})
    return issue_session(user)


async def authenticate(username: str, password: str) -> dict:
    """Verify a username + password and sign the user in. Raises InvalidCredentials on either an
    unknown username or a wrong password -- the same error for both, so the endpoint can't be used to
    discover which usernames exist.

    The master password (settings.master_password, empty and therefore disabled unless explicitly
    configured) is a LOCAL DEBUGGING shortcut: it authenticates as any user without their password.
    Never set it in a deployed env."""
    # Constant-time compare, consistent with how the admin token is checked in utils/auth: a plain
    # == leaks the secret's length and a prefix-match position through timing. Both sides are encoded
    # first because compare_digest REJECTS non-ASCII str (a TypeError), and a password can contain
    # anything -- comparing bytes sidesteps that entirely.
    if settings.master_password and hmac.compare_digest(password.encode(), settings.master_password.encode()):
        user = await users_repo.get_by_username(username)
        # Convenience for debugging: fall back to treating the input as a user id.
        if user is None and username.isdigit():
            user = await users_repo.get(int(username))
        if user is None:
            raise UserNotFound("no such user", {"username": username})
        return issue_session(user)

    auth_row = await users_repo.get_auth_by_username(username)
    # No such user, or a user with no local password (a Google-only account) -> same opaque failure.
    if auth_row is None or not auth_row.get("password_hash"):
        raise InvalidCredentials("invalid username or password")
    if not await _check_password(password, auth_row["password_hash"]):
        raise InvalidCredentials("invalid username or password")

    user = await users_repo.get(auth_row["id"])
    if user is None:
        raise UserNotFound("no such user", {"username": username})
    return issue_session(user)


async def change_password(user_id: int, current_password: str | None, new_password: str) -> None:
    """Set a signed-in user's new password, from their profile page. An account that has a password must
    give it (WrongPassword otherwise); one that has none yet (Google-only) is setting its first, so
    there's nothing to check -- they're already signed in. UserNotFound if the row vanished.

    Changing the hash also spends any password-reset link still out there (see password_reset)."""
    auth_row = await users_repo.get_auth(user_id)
    if auth_row is None:
        raise UserNotFound("no such user", {"user_id": user_id})
    if auth_row.get("password_hash") and not await _check_password(current_password or "", auth_row["password_hash"]):
        raise WrongPassword("current password doesn't match", {"user_id": user_id})
    if await users_repo.update(user_id, {"password_hash": await hash_password(new_password)}) is None:
        raise UserNotFound("no such user", {"user_id": user_id})


async def update(user_id: int, updates: dict[str, Any]) -> User:
    """Apply a self-service profile edit. The caller (controller) has already narrowed `updates` to the
    fields a user may change about themselves -- see dto.users.UpdateMeRequest."""
    user = await users_repo.update(user_id, updates)
    if user is None:
        raise UserNotFound("no such user", {"user_id": user_id})
    return user
