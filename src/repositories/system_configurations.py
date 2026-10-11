# Intentions: data access for `system_configurations` -- runtime, operator-tunable settings (name ->
# value) that change behaviour without a redeploy. Read-mostly; writes happen out-of-band (a console,
# a script), not through the app's own endpoints. Schema: migrations/0002_system_configurations.sql.
#
# TWO INDEPENDENT GATES on a row, which are easy to conflate:
#   - `status` (ENABLED/DISABLED) -- whether the value is IN EFFECT. A disabled row is treated as
#     absent, so callers fall back to their code default.
#   - `is_private` -- who may READ it. Private settings are served only to a super user; the public
#     ones are what the browser is allowed to see.
# They're orthogonal: a row can be in effect but invisible to the FE (a secret threshold), or visible
# but disabled. Every read here applies `status`; VISIBILITY is the caller's flag to pass, since only
# the controller knows who's asking.

from typing import Any

import pydash

from src.constants import ConfigStatus
from src.utils import postgres

_COLUMNS = "id, name, value, status, is_private, meta, deleted, created_at, updated_at"

# Columns update() may touch. `name` is the lookup key, and the rest are bookkeeping.
_UPDATABLE_COLUMNS = ["value", "status", "is_private", "meta"]

# The filter fragment every read carries: only rows that are in effect. `$include_private` widens the
# read to private rows (super user) -- when false, only public ones come back.
_IN_EFFECT = "status = $status AND NOT deleted AND ($include_private OR NOT is_private)"


async def get_all_enabled() -> dict[str, Any]:
    """Every ENABLED setting (private or not) as {name: value} -- what the service's cache is built
    from. DISABLED rows are excluded, so an absent key means "use your default"."""
    rows = await postgres.fetch(
        f"SELECT name, value FROM system_configurations WHERE {_IN_EFFECT}",
        {"status": ConfigStatus.ENABLED, "include_private": True},
    )
    return {row["name"]: row["value"] for row in rows}


async def find(*, include_private: bool) -> list[dict]:
    """Every enabled setting the caller may see -- private ones only when `include_private`."""
    return await postgres.fetch(
        f"SELECT {_COLUMNS} FROM system_configurations WHERE {_IN_EFFECT} ORDER BY name",
        {"status": ConfigStatus.ENABLED, "include_private": include_private},
    )


async def get(name: str, *, include_private: bool) -> dict | None:
    """One enabled setting by name, or None -- a private one looks absent unless `include_private`."""
    return await postgres.fetchrow(
        f"SELECT {_COLUMNS} FROM system_configurations WHERE name = $name AND {_IN_EFFECT}",
        {"name": name, "status": ConfigStatus.ENABLED, "include_private": include_private},
    )


_INSERT = f"""
    INSERT INTO system_configurations (name, value, is_private, meta)
    VALUES ($name, $value, $is_private, $meta)
    RETURNING {_COLUMNS}
"""


async def insert(name: str, value: Any, *, is_private: bool = True, meta: dict | None = None) -> dict:
    """Create a setting. Defaults to PRIVATE: a new setting shouldn't become browser-visible just
    because whoever added it didn't think about visibility -- opt in to public explicitly."""
    row = await postgres.fetchrow(_INSERT, {"name": name, "value": value, "is_private": is_private, "meta": meta or {}})
    assert row is not None  # RETURNING on a successful INSERT always yields the row
    return row


async def update(name: str, updates: dict[str, Any]) -> dict | None:
    """Apply `updates` to the setting called `name`, returning the new row (or None if absent). Matches
    regardless of status, so a DISABLED row can be re-enabled through here. Keys outside
    _UPDATABLE_COLUMNS are ignored."""
    row = pydash.pick(updates, *_UPDATABLE_COLUMNS)
    assignments = ", ".join(pydash.compact([postgres.to_set_clause(row), "updated_at = now()"]))
    return await postgres.fetchrow(
        f"UPDATE system_configurations SET {assignments} WHERE name = $name AND NOT deleted RETURNING {_COLUMNS}",
        {**row, "name": name},
    )
