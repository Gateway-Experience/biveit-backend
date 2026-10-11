# Intentions: data access for `events` -- a generic, append-only activity log. One row per thing that
# happened; nothing is ever updated in place, so this is the STREAM half of the table/stream duality
# (the collapsed state lives in whatever domain table the event was about).
#
# The shape is the type-ID combo applied throughout: each participant in an event is a (type, ref)
# pair -- subject (who did it), direct object (what they did it to), indirect object (what they did it
# with), grouping (what bucket it belongs to). Recording a new kind of event means inventing new type
# values, not migrating the table. `value` is the event's own payload, free-form jsonb.
# Schema: migrations/0003_events.sql.
#
# NO CONSUMER YET -- carried into the boilerplate ahead of need, because retrofitting an activity log
# after the fact means the history you wanted is exactly the history you don't have. Reporting-shaped
# reads (counts per type per day, ...) should get their own SQL here once a consumer asks for them.

from typing import Any

import pydash

from src.utils import postgres

_COLUMNS = """
    id, type, subject_type, subject_ref, direct_object_type, direct_object_ref,
    indirect_object_type, indirect_object_ref, grouping_type, grouping_ref, value, created_at
"""

# Every column a caller fills in (or filters on). id/created_at are the db's.
_EVENT_FIELDS = [
    "type",
    "subject_type",
    "subject_ref",
    "direct_object_type",
    "direct_object_ref",
    "indirect_object_type",
    "indirect_object_ref",
    "grouping_type",
    "grouping_ref",
    "value",
]


def _to_row(event: dict[str, Any]) -> dict[str, Any]:
    """[Pure] An event dict -> a FULL row (every field present, missing ones None), so a batch of
    differently-shaped events can share one INSERT statement. Unknown keys are dropped."""
    return {
        **{field: None for field in _EVENT_FIELDS},
        **pydash.pick(event, *_EVENT_FIELDS),
        "value": event.get("value") or {},
    }


_INSERT = f"INSERT INTO events {postgres.to_insert_clause(_to_row({}))}"


async def get(event_id: int) -> dict | None:
    return await postgres.fetchrow(f"SELECT {_COLUMNS} FROM events WHERE id = $event_id", {"event_id": event_id})


async def find(criteria: dict[str, Any] | None = None, *, limit: int | None = None) -> list[dict]:
    """Events whose fields EQUAL `criteria` (e.g. {"type": ..., "subject_ref": ...}), oldest-first --
    the natural order for a log. Keys outside the event fields are ignored. `limit` None means all."""
    filters = pydash.pick(criteria or {}, *_EVENT_FIELDS)
    return await postgres.fetch(
        f"SELECT {_COLUMNS} FROM events WHERE {postgres.to_where_clause(filters)} ORDER BY created_at, id LIMIT $limit",
        {**filters, "limit": limit},
    )


async def count(criteria: dict[str, Any] | None = None) -> int:
    filters = pydash.pick(criteria or {}, *_EVENT_FIELDS)
    row = await postgres.fetchrow(
        f"SELECT count(*) AS n FROM events WHERE {postgres.to_where_clause(filters)}", filters
    )
    return row["n"] if row else 0


async def insert(event: dict[str, Any]) -> dict:
    """Append one event."""
    row = await postgres.fetchrow(f"{_INSERT} RETURNING {_COLUMNS}", _to_row(event))
    assert row is not None  # RETURNING on a successful INSERT always yields the row
    return row


async def insert_many(events: list[dict[str, Any]]) -> int:
    """Append several events in ONE batched round trip, returning how many landed. For a caller
    producing a batch -- doing it one insert at a time is the thing this exists to avoid."""
    await postgres.execute_many(_INSERT, [_to_row(event) for event in events])
    return len(events)
