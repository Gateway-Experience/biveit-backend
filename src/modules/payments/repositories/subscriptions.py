# Intentions: data access for `subscriptions` -- our copy of each purchase at the payment provider (plans and domains,
# recurring; the one-time purchase, lifetime). Schema: migrations/0011_subscriptions.sql.

from typing import Any

import pydash

from src.utils import postgres

_COLUMNS = """
    id, provider, provider_object, provider_ref, billing, user_id, target_type, target_id, product, plan,
    product_data, status, renews_at, ends_at, trial_ends_at, custom_data, snapshot, created_at, updated_at
"""
_WRITABLE_COLUMNS = [
    "provider", "provider_object", "provider_ref", "billing", "user_id", "target_type", "target_id", "product", "plan",
    "product_data", "status", "renews_at", "ends_at", "trial_ends_at", "custom_data", "snapshot", "created_at",
]  # fmt: skip
# What a row is -- whose, for what, which object at the provider -- never changes once it's there.
_FIXED_COLUMNS = {"provider", "provider_object", "provider_ref", "billing", "user_id", "target_type", "target_id"}


async def find(criteria: dict[str, Any]) -> list[dict]:
    """Subscriptions matching `criteria` (column = value, ANDed), oldest first. A target's ({"target_type": ..,
    "target_id": ..}) rides idx_subscriptions_target."""
    where = pydash.pick(criteria, "id", *_WRITABLE_COLUMNS)
    sql = f"SELECT {_COLUMNS} FROM subscriptions WHERE {postgres.to_where_clause(where)} ORDER BY created_at, id"
    return await postgres.fetch(sql, where)


async def upsert(row: dict[str, Any]) -> dict:
    """Insert the subscription, or overwrite ours with what the provider says now (keyed by provider +
    provider_object + provider_ref). custom_data is kept from the first time it came (later webhooks may not repeat
    it), and so is what the row is (_FIXED_COLUMNS)."""
    values = pydash.pick(row, *_WRITABLE_COLUMNS)
    refreshed = [column for column in values if column not in _FIXED_COLUMNS]
    assignments = ", ".join(
        f"{column} = "
        + (
            "CASE WHEN subscriptions.custom_data = '{}'::jsonb THEN EXCLUDED.custom_data ELSE subscriptions.custom_data END"
            if column == "custom_data"
            else f"EXCLUDED.{column}"
        )
        for column in refreshed
    )
    sql = f"""
        INSERT INTO subscriptions {postgres.to_insert_clause(values)}
        ON CONFLICT (provider, provider_object, provider_ref) DO UPDATE SET {assignments}, updated_at = now()
        RETURNING {_COLUMNS}
    """
    upserted = await postgres.fetchrow(sql, values)
    assert upserted is not None  # an upsert always returns its row
    return upserted
