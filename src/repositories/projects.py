# Intentions: data access for the `projects` table -- the sites/apps a user builds, each owned by one
# user. Schema: migrations/0005_projects.sql. Every read skips soft-deleted rows (deleted_at IS NULL), and
# find() also skips the ones in Trash (trashed_at): to the rest of the app a trashed project isn't there.
# Trash has its own read (find_trashed).
#
# Writes take only real columns (_WRITABLE_COLUMNS); anything else is dropped. Deciding what a CLIENT
# may set (e.g. never user_id) is the caller's job -- this layer trusts its caller. `hash` isn't
# writable: the DB fills it in on insert, and it never changes.
#
# Every project comes back with its owner's hash as `user_hash` (joined in, not stored here), since a
# project's folder sits inside its owner's (services/devenv/workspaces).

from typing import Any, cast

import pydash

from src.dto.projects import Project
from src.utils import postgres

_COLUMNS = """
    id, hash, user_id, name, description, stage, bootstrap_data, build_approved_at, pinned_at,
    folder_id, domains, meta, created_at, updated_at, trashed_at, deleted_at
"""

# `updated_at` IS writable, and never set implicitly: it means "the user last edited this project" (the
# list sorts by it), so only user-facing edit flows pass it. System writes (meta.devenv_image...) must not,
# and neither do pinning or filing into a folder -- those organise the list, they don't edit the project.
_WRITABLE_COLUMNS = [
    "user_id",
    "name",
    "description",
    "stage",
    "bootstrap_data",
    "build_approved_at",
    "pinned_at",
    "folder_id",
    "domains",
    "meta",
    "updated_at",
    "trashed_at",
    "deleted_at",
]


def _with_user_hash(statement: str, order_by: str = "") -> str:
    """[Pure] Wrap a statement that returns project rows (SELECT, or INSERT/UPDATE ... RETURNING) so each
    row also carries its owner's hash. The statement runs as-is in a CTE, so its own WHERE never has to
    tell projects' columns apart from users' (both have id, hash, meta...)."""
    return f"""
        WITH project_rows AS ({statement})
        SELECT project_rows.*, users.hash AS user_hash FROM project_rows JOIN users ON users.id = project_rows.user_id
        {order_by}
    """


async def _find(criteria: dict[str, Any], is_in_trash: bool, order_by: str) -> list[Project]:
    """Projects matching `criteria` (column = value, ANDed) that aren't deleted, either in Trash or not."""
    where = pydash.pick(criteria, "id", *_WRITABLE_COLUMNS)
    trash_condition = "trashed_at IS NOT NULL" if is_in_trash else "trashed_at IS NULL"
    sql = _with_user_hash(
        f"""SELECT {_COLUMNS} FROM projects
            WHERE {postgres.to_where_clause(where)} AND deleted_at IS NULL AND {trash_condition}""",
        order_by,
    )
    return cast("list[Project]", await postgres.fetch(sql, where))


async def find(criteria: dict[str, Any]) -> list[Project]:
    """Live projects (not in Trash) matching `criteria`, most recently edited first. A user's list
    ({"user_id": ..}) rides idx_projects_user_updated. No pagination yet -- a user's own project list is small."""
    return await _find(criteria, False, "ORDER BY project_rows.updated_at DESC, project_rows.id DESC")


async def find_trashed(criteria: dict[str, Any]) -> list[Project]:
    """The projects in Trash matching `criteria`, most recently trashed first."""
    return await _find(criteria, True, "ORDER BY project_rows.trashed_at DESC, project_rows.id DESC")


async def has_ever_created(user_id: int) -> bool:
    """Whether the user has ever made a project, counting the ones in Trash and the ones deleted for good
    (deletes are soft, so their rows are still here)."""
    row = await postgres.fetchrow(
        "SELECT EXISTS (SELECT 1 FROM projects WHERE user_id = $user_id) AS has_created", {"user_id": user_id}
    )
    return bool(row and row["has_created"])


async def insert(data: dict[str, Any]) -> Project:
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    inserted = await postgres.fetchrow(
        _with_user_hash(f"INSERT INTO projects {postgres.to_insert_clause(row)} RETURNING {_COLUMNS}"), row
    )
    return cast("Project", inserted)


async def update(project_id: int, updates: dict[str, Any], expected: dict[str, Any] | None = None) -> Project | None:
    """Apply `updates` to a live project (in Trash or not) and return the NEW row, or None if there's no such project. Only
    the given columns change; `meta` is MERGED rather than replaced. updated_at changes only if it's
    in `updates` (see _WRITABLE_COLUMNS).

    `expected`: column = value conditions the row must ALSO meet, checked in the same statement -- a
    compare-and-set, e.g. {"stage": "bootstrap"} so only one of two racing stage changes wins. A row
    that doesn't meet them is left alone and None comes back, same as a missing one."""
    row = pydash.pick(updates, *_WRITABLE_COLUMNS)
    conditions = pydash.pick(expected or {}, *_WRITABLE_COLUMNS)
    if not row:
        raise ValueError(f"nothing writable in {list(updates)}")
    assignments = pydash.compact(
        [
            postgres.to_set_clause(pydash.omit(row, "meta")),
            "meta = meta || $meta" if "meta" in row else None,
        ]
    )
    # Their own param names (expected_<column>), so a column can be both set and checked -- stage is.
    where = " AND ".join(
        ["id = $project_id", "deleted_at IS NULL", *[f"{column} = $expected_{column}" for column in conditions]]
    )
    sql = _with_user_hash(f"UPDATE projects SET {', '.join(assignments)} WHERE {where} RETURNING {_COLUMNS}")
    params = {
        **row,
        **{f"expected_{column}": value for column, value in conditions.items()},
        "project_id": project_id,
    }
    return cast("Project | None", await postgres.fetchrow(sql, params))
