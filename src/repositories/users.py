# Intentions: data access for the `users` table -- the app's accounts, both credentialed (USER) and
# anonymous (GUEST). Covers the auth lookups (by username, by google id), the profile reads, and the
# writes behind registration/promotion. Schema: migrations/0001_users.sql.
#
# PASSWORD HANDLING is the one thing to be careful about here: `password_hash` (bcrypt) is stored on the
# row but left out of every read's column list, so the User interface genuinely never carries it. The
# exceptions are get_auth_by_username / get_auth, which exist precisely to hand the hash to the auth
# service and are named so their call sites are greppable. Never widen a normal read to include it.
#
# Writes take only real columns (_WRITABLE_COLUMNS); anything else is dropped. Deciding which of those
# a CLIENT may set is the service's job (services/users vets FE input) -- this layer trusts its caller.

from typing import Any, cast

import asyncpg
import pydash

from src.constants import UserType
from src.dto.users import User
from src.utils import postgres

# Everything a caller may see. `password_hash` is absent on purpose -- see the module note. Only whether
# there IS one comes out (has_password), so the profile page knows to ask for the current password.
_COLUMNS = """
    id, hash, user_type, username, platform, platform_id, first_name, last_name, profile_picture, language,
    tags, meta, created_at, updated_at, deleted_at, (password_hash IS NOT NULL) AS has_password
"""

# What a caller may write. Anything outside this set is dropped.
_WRITABLE_COLUMNS = [
    "user_type",
    "username",
    "password_hash",
    "platform",
    "platform_id",
    "first_name",
    "last_name",
    "profile_picture",
    "language",
    "tags",
    "meta",
    "deleted_at",
]

# Unique constraint name (Postgres' default `<table>_<column>_key`) -> the field it guards.
_UNIQUE_CONSTRAINT_FIELDS = {
    "users_username_key": ["username"],
    "users_platform_platform_id_key": ["platform", "platform_id"],
}


class DuplicateUser(Exception):
    """A write collided with a unique constraint. Carries {"keys": [...]} naming which field(s) clashed,
    so the controller can tell "username taken" from "google account already linked" without
    re-querying. Raised instead of leaking asyncpg's UniqueViolationError past this layer."""


def _to_duplicate(error: asyncpg.UniqueViolationError) -> DuplicateUser:
    """[Pure] Translate the driver's unique-violation into our own, naming the offending field. An
    unknown constraint yields an empty list -- a vaguer error still beats a 500."""
    keys = _UNIQUE_CONSTRAINT_FIELDS.get(error.constraint_name, [])
    return DuplicateUser("duplicate key", {"keys": keys})


_GET = f"SELECT {_COLUMNS} FROM users WHERE id = $user_id AND deleted_at IS NULL"
_GET_BY_USERNAME = f"SELECT {_COLUMNS} FROM users WHERE username = $username AND deleted_at IS NULL"
_GET_BY_PLATFORM = f"""
    SELECT {_COLUMNS} FROM users WHERE platform = $platform AND platform_id = $platform_id AND deleted_at IS NULL
"""


async def get(user_id: int) -> User | None:
    return cast("User | None", await postgres.fetchrow(_GET, {"user_id": user_id}))


async def get_by_username(username: str) -> User | None:
    return cast("User | None", await postgres.fetchrow(_GET_BY_USERNAME, {"username": username}))


async def get_by_platform(platform: str, platform_id: str) -> User | None:
    """One user by their sign-in identity, e.g. (GOOGLE, <sub>)."""
    row = await postgres.fetchrow(_GET_BY_PLATFORM, {"platform": platform, "platform_id": platform_id})
    return cast("User | None", row)


_GET_AUTH_BY_USERNAME = "SELECT id, password_hash FROM users WHERE username = $username AND deleted_at IS NULL"
_GET_AUTH = "SELECT id, password_hash FROM users WHERE id = $user_id AND deleted_at IS NULL"


async def get_auth_by_username(username: str) -> dict | None:
    """The LOGIN lookup -- deliberately OUTSIDE the User interface: it selects `password_hash`, which
    User never exposes. Returns just what the auth service needs to verify a credential (id + the hash),
    or None if no such user. Keep the result inside the auth service; never let the hash travel upward."""
    return await postgres.fetchrow(_GET_AUTH_BY_USERNAME, {"username": username})


async def get_auth(user_id: int) -> dict | None:
    """get_auth_by_username's twin, by id: id + password_hash, or None. Same rule -- the hash stays inside
    the auth service."""
    return await postgres.fetchrow(_GET_AUTH, {"user_id": user_id})


async def insert(data: dict[str, Any]) -> User:
    """Create a user and return it (without the password). The caller supplies an ALREADY-HASHED
    `password_hash` if the account has one -- hashing is the auth service's job, never this layer's."""
    row = pydash.pick({"user_type": UserType.USER, **data}, *_WRITABLE_COLUMNS)
    try:
        inserted = await postgres.fetchrow(
            f"INSERT INTO users {postgres.to_insert_clause(row)} RETURNING {_COLUMNS}", row
        )
    except asyncpg.UniqueViolationError as error:
        raise _to_duplicate(error) from error
    return cast("User", inserted)


async def update(user_id: int, updates: dict[str, Any]) -> User | None:
    """Apply `updates` to a user and return the NEW row, or None if there's no such user. Only the given
    columns change, so a partial update can't blank out what it didn't mention. `meta` is MERGED into
    the existing meta rather than replacing it, for the same reason."""
    row = pydash.pick(updates, *_WRITABLE_COLUMNS)
    assignments = pydash.compact(
        [
            postgres.to_set_clause(pydash.omit(row, "meta")),
            "meta = meta || $meta" if "meta" in row else None,
            "updated_at = now()",
        ]
    )
    sql = f"UPDATE users SET {', '.join(assignments)} WHERE id = $user_id AND deleted_at IS NULL RETURNING {_COLUMNS}"
    try:
        updated = await postgres.fetchrow(sql, {**row, "user_id": user_id})
    except asyncpg.UniqueViolationError as error:
        raise _to_duplicate(error) from error
    return cast("User | None", updated)
