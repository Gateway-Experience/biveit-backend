# Intentions: data access for the `project_folders` table -- the folders a user groups their projects
# into. Schema: migrations/0008_project_folders.sql. Every read skips soft-deleted rows (deleted_at IS NULL).
#
# Writes take only real columns (_WRITABLE_COLUMNS); anything else is dropped. Deciding what a CLIENT
# may set (never user_id) is the caller's job.

from typing import Any, cast

import pydash

from src.dto.projects import ProjectFolder
from src.utils import postgres

_COLUMNS = "id, user_id, name, created_at, updated_at, deleted_at"

_WRITABLE_COLUMNS = ["user_id", "name", "deleted_at"]


async def find(criteria: dict[str, Any]) -> list[ProjectFolder]:
    """Live folders matching `criteria` (column = value, ANDed), oldest first -- so a user's folders keep
    the order they made them in. A user's list ({"user_id": ..}) rides idx_project_folders_user."""
    where = pydash.pick(criteria, "id", *_WRITABLE_COLUMNS)
    sql = f"""
        SELECT {_COLUMNS} FROM project_folders WHERE {postgres.to_where_clause(where)} AND deleted_at IS NULL
        ORDER BY created_at, id
    """
    return cast("list[ProjectFolder]", await postgres.fetch(sql, where))


async def insert(data: dict[str, Any]) -> ProjectFolder:
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    inserted = await postgres.fetchrow(
        f"INSERT INTO project_folders {postgres.to_insert_clause(row)} RETURNING {_COLUMNS}", row
    )
    return cast("ProjectFolder", inserted)


async def update(folder_id: int, updates: dict[str, Any]) -> ProjectFolder | None:
    """Apply `updates` to a live folder and return the NEW row, or None if there's no such folder."""
    row = pydash.pick(updates, *_WRITABLE_COLUMNS)
    if not row:
        raise ValueError(f"nothing writable in {list(updates)}")
    sql = f"""
        UPDATE project_folders SET {postgres.to_set_clause(row)}, updated_at = now()
        WHERE id = $folder_id AND deleted_at IS NULL RETURNING {_COLUMNS}
    """
    return cast("ProjectFolder | None", await postgres.fetchrow(sql, {**row, "folder_id": folder_id}))
