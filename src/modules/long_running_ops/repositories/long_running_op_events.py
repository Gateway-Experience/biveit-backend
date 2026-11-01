# Intentions: data access for `long_running_op_events` -- append-only, one row per thing that happened to an operation.
# Schema: migrations/0012_long_running_op_events.sql.

from uuid import UUID

from src.utils import postgres

_COLUMNS = "id, operation_id, created_at, value"


async def insert(operation_id: UUID, value: dict) -> dict:
    sql = f"""
        INSERT INTO long_running_op_events {postgres.to_insert_clause({"operation_id": operation_id, "value": value})}
        RETURNING {_COLUMNS}
    """
    inserted = await postgres.fetchrow(sql, {"operation_id": operation_id, "value": value})
    assert inserted is not None  # an insert always returns its row
    return inserted


async def find(operation_id: UUID) -> list[dict]:
    """An operation's events, oldest first."""
    sql = f"""
        SELECT {_COLUMNS} FROM long_running_op_events WHERE operation_id = $operation_id ORDER BY created_at, id
    """
    return await postgres.fetch(sql, {"operation_id": operation_id})
