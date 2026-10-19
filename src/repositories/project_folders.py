# Intentions: data access for the `project_folders` table -- the (nestable) folders a user groups their
# projects into. Schema: migrations/0008_project_folders.sql. Every read skips soft-deleted rows
# (deleted_at IS NULL).
#
# Writes take only real columns (_WRITABLE_COLUMNS); anything else is dropped. Deciding what a CLIENT
# may set (never user_id) is the caller's job.

from typing import Any, cast

import asyncpg
import pydash

from src.dto.projects import ProjectFolder
from src.utils import postgres

_COLUMNS = "id, user_id, parent_id, name, created_at, updated_at, deleted_at"

_WRITABLE_COLUMNS = ["user_id", "parent_id", "name", "deleted_at"]


class DuplicateFolderName(Exception):
    """The parent already has a live folder by that name (case-insensitive, uq_project_folders_parent_name).
    Raised instead of leaking asyncpg's UniqueViolationError past this layer."""


async def find(criteria: dict[str, Any]) -> list[ProjectFolder]:
    """Live folders matching `criteria` (column = value, ANDed), oldest first -- so a user's folders keep
    the order they made them in. A user's list ({"user_id": ..}) rides uq_project_folders_parent_name."""
    where = pydash.pick(criteria, "id", *_WRITABLE_COLUMNS)
    sql = f"""
        SELECT {_COLUMNS} FROM project_folders WHERE {postgres.to_where_clause(where)} AND deleted_at IS NULL
        ORDER BY created_at, id
    """
    return cast("list[ProjectFolder]", await postgres.fetch(sql, where))


async def insert(data: dict[str, Any]) -> ProjectFolder:
    """Create a folder and return it. Raises DuplicateFolderName if its parent already has that name."""
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    try:
        inserted = await postgres.fetchrow(
            f"INSERT INTO project_folders {postgres.to_insert_clause(row)} RETURNING {_COLUMNS}", row
        )
    except asyncpg.UniqueViolationError as error:
        raise DuplicateFolderName(row.get("name")) from error
    return cast("ProjectFolder", inserted)


async def update(folder_id: int, updates: dict[str, Any]) -> ProjectFolder | None:
    """Apply `updates` to a live folder and return the NEW row, or None if there's no such folder. Raises
    DuplicateFolderName if a rename or a move lands on a name already taken in that parent."""
    row = pydash.pick(updates, *_WRITABLE_COLUMNS)
    if not row:
        raise ValueError(f"nothing writable in {list(updates)}")
    sql = f"""
        UPDATE project_folders SET {postgres.to_set_clause(row)}, updated_at = now()
        WHERE id = $folder_id AND deleted_at IS NULL RETURNING {_COLUMNS}
    """
    try:
        updated = await postgres.fetchrow(sql, {**row, "folder_id": folder_id})
    except asyncpg.UniqueViolationError as error:
        raise DuplicateFolderName(row.get("name")) from error
    return cast("ProjectFolder | None", updated)


async def delete(folder_id: int) -> list[int]:
    """Soft-delete a live folder and everything under it, all with the same deleted_at (so a hand restore
    can find the batch), and take every project filed in any of them back to the root (folder_id NULL;
    updated_at untouched -- unfiling isn't editing). One statement, so it's all or nothing. Returns the
    folder ids deleted -- empty if there's no such live folder.
    The walk joins on (user_id, parent_id), riding uq_project_folders_parent_name; UNION (not UNION ALL)
    so even a bad row looping back on itself can't make it recurse forever."""
    sql = """
        WITH RECURSIVE subtree AS (
            SELECT id, user_id FROM project_folders WHERE id = $folder_id AND deleted_at IS NULL
            UNION
            SELECT child.id, child.user_id FROM project_folders child
            JOIN subtree ON child.user_id = subtree.user_id AND child.parent_id = subtree.id
            WHERE child.deleted_at IS NULL
        ),
        deleted AS (
            UPDATE project_folders SET deleted_at = now(), updated_at = now()
            WHERE id IN (SELECT id FROM subtree) RETURNING id
        ),
        unfiled AS (
            UPDATE projects SET folder_id = NULL WHERE folder_id IN (SELECT id FROM deleted)
        )
        SELECT id FROM deleted
    """
    return pydash.map_(await postgres.fetch(sql, {"folder_id": folder_id}), "id")
