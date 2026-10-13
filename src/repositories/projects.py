# Intentions: data access for the `projects` table -- the sites/apps a user builds, each owned by one
# user. Schema: migrations/0005_projects.sql. Every read skips soft-deleted rows (deleted_at IS NULL).
#
# Writes take only real columns (_WRITABLE_COLUMNS); anything else is dropped. Deciding what a CLIENT
# may set (e.g. never user_id) is the caller's job -- this layer trusts its caller.

from typing import Any, cast

import pydash

from src.dto.projects import Project
from src.utils import postgres

_COLUMNS = "id, user_id, name, description, current_port, meta, created_at, updated_at, deleted_at"

# `updated_at` IS writable, and never set implicitly: it means "the user last edited this project" (the
# list sorts by it), so only user-facing edit flows pass it. System writes like current_port must not.
_WRITABLE_COLUMNS = ["user_id", "name", "description", "current_port", "meta", "updated_at", "deleted_at"]


async def find(criteria: dict[str, Any]) -> list[Project]:
    """Live projects matching `criteria` (column = value, ANDed), most recently edited first. A user's
    list ({"user_id": ..}) rides idx_projects_user_updated; a session port ({"current_port": ..}) rides
    idx_projects_current_port. No pagination yet -- a user's own project list is small."""
    where = pydash.pick(criteria, "id", *_WRITABLE_COLUMNS)
    sql = f"""
        SELECT {_COLUMNS} FROM projects
        WHERE {postgres.to_where_clause(where)} AND deleted_at IS NULL
        ORDER BY updated_at DESC, id DESC
    """
    return cast("list[Project]", await postgres.fetch(sql, where))


async def insert(data: dict[str, Any]) -> Project:
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    inserted = await postgres.fetchrow(
        f"INSERT INTO projects {postgres.to_insert_clause(row)} RETURNING {_COLUMNS}", row
    )
    return cast("Project", inserted)


async def update(project_id: int, updates: dict[str, Any]) -> Project | None:
    """Apply `updates` to a live project and return the NEW row, or None if there's no such project. Only
    the given columns change; `meta` is MERGED rather than replaced. updated_at changes only if it's
    in `updates` (see _WRITABLE_COLUMNS)."""
    row = pydash.pick(updates, *_WRITABLE_COLUMNS)
    if not row:
        raise ValueError(f"nothing writable in {list(updates)}")
    assignments = pydash.compact(
        [
            postgres.to_set_clause(pydash.omit(row, "meta")),
            "meta = meta || $meta" if "meta" in row else None,
        ]
    )
    sql = f"UPDATE projects SET {', '.join(assignments)} WHERE id = $project_id AND deleted_at IS NULL RETURNING {_COLUMNS}"
    return cast("Project | None", await postgres.fetchrow(sql, {**row, "project_id": project_id}))
