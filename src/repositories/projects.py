# Intentions: data access for the `projects` table -- the sites/apps a user builds, each owned by one
# user. Schema: migrations/0005_projects.sql. Every read skips soft-deleted rows (deleted_at IS NULL).
#
# Writes take only real columns (_WRITABLE_COLUMNS); anything else is dropped. Deciding what a CLIENT
# may set (e.g. never user_id) is the caller's job -- this layer trusts its caller.

from typing import Any, cast

import pydash

from src.dto.projects import Project
from src.utils import postgres

_COLUMNS = "id, user_id, name, description, meta, created_at, updated_at, deleted_at"

_WRITABLE_COLUMNS = ["user_id", "name", "description", "meta", "deleted_at"]

# Rides idx_projects_user_updated (partial on deleted_at IS NULL), so it's an index scan per user.
_FIND_BY_USER = f"""
    SELECT {_COLUMNS} FROM projects
    WHERE user_id = $user_id AND deleted_at IS NULL
    ORDER BY updated_at DESC, id DESC
"""


async def find(user_id: int) -> list[Project]:
    """A user's live projects, most recently edited first. No pagination yet -- a user's own project
    list is small; add a limit/cursor when that stops being true."""
    return cast("list[Project]", await postgres.fetch(_FIND_BY_USER, {"user_id": user_id}))


async def insert(data: dict[str, Any]) -> Project:
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    inserted = await postgres.fetchrow(
        f"INSERT INTO projects {postgres.to_insert_clause(row)} RETURNING {_COLUMNS}", row
    )
    return cast("Project", inserted)
