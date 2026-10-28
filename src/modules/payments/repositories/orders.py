# Intentions: data access for `orders` -- our copy of each one-off order at the payment provider (the one-time
# purchase). Schema: migrations/0012_orders.sql.

from typing import Any

import pydash

from src.utils import postgres

_COLUMNS = """
    id, provider, provider_ref, user_id, target_type, target_id, product, status, total_usd, custom_data,
    snapshot, created_at, updated_at
"""
_WRITABLE_COLUMNS = [
    "provider", "provider_ref", "user_id", "target_type", "target_id", "product", "status", "total_usd",
    "custom_data", "snapshot", "created_at",
]  # fmt: skip


async def find(criteria: dict[str, Any]) -> list[dict]:
    """Orders matching `criteria` (column = value, ANDed), oldest first."""
    where = pydash.pick(criteria, "id", *_WRITABLE_COLUMNS)
    sql = f"SELECT {_COLUMNS} FROM orders WHERE {postgres.to_where_clause(where)} ORDER BY created_at, id"
    return await postgres.fetch(sql, where)


async def upsert(row: dict[str, Any]) -> dict:
    """Insert the order, or overwrite its status / total / snapshot with what the provider says now (keyed by
    provider + provider_ref). Who and what it was for never change."""
    values = pydash.pick(row, *_WRITABLE_COLUMNS)
    sql = f"""
        INSERT INTO orders {postgres.to_insert_clause(values)}
        ON CONFLICT (provider, provider_ref) DO UPDATE SET status = EXCLUDED.status,
            total_usd = EXCLUDED.total_usd, snapshot = EXCLUDED.snapshot, updated_at = now()
        RETURNING {_COLUMNS}
    """
    upserted = await postgres.fetchrow(sql, values)
    assert upserted is not None  # an upsert always returns its row
    return upserted
