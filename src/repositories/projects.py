# Intentions: data access for the `projects` table -- the sites/apps a user builds, each owned by one
# user. Schema: migrations/0005_projects.sql. Every read skips soft-deleted rows (deleted_at IS NULL).
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
    id, hash, user_id, name, description, stage, bootstrap_data, implementation_done_at, pinned_at,
    folder_id, meta, created_at, updated_at, deleted_at
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
    "implementation_done_at",
    "pinned_at",
    "folder_id",
    "meta",
    "updated_at",
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


async def find(criteria: dict[str, Any]) -> list[Project]:
    """Live projects matching `criteria` (column = value, ANDed), most recently edited first. A user's
    list ({"user_id": ..}) rides idx_projects_user_updated. No pagination yet -- a user's own project list is small."""
    where = pydash.pick(criteria, "id", *_WRITABLE_COLUMNS)
    sql = _with_user_hash(
        f"SELECT {_COLUMNS} FROM projects WHERE {postgres.to_where_clause(where)} AND deleted_at IS NULL",
        "ORDER BY project_rows.updated_at DESC, project_rows.id DESC",
    )
    return cast("list[Project]", await postgres.fetch(sql, where))


async def insert(data: dict[str, Any]) -> Project:
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    inserted = await postgres.fetchrow(
        _with_user_hash(f"INSERT INTO projects {postgres.to_insert_clause(row)} RETURNING {_COLUMNS}"), row
    )
    return cast("Project", inserted)


async def update(project_id: int, updates: dict[str, Any], expected: dict[str, Any] | None = None) -> Project | None:
    """Apply `updates` to a live project and return the NEW row, or None if there's no such project. Only
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
