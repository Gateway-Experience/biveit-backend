# Intentions: data access for `subscriptions` -- our copy of each purchase at the payment provider (plans and domains,
# recurring; the one-time purchase, lifetime), plus the ones an admin set by hand (provider 'admin'). Schema:
# migrations/0011_subscriptions.sql (deleted_at: soft-deleted rows are left out of every read unless asked for).

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pydash

from src.utils import postgres

_COLUMNS = """
    id, provider, provider_object, provider_ref, billing, user_id, target_type, target_id, product, plan,
    product_data, status, renews_at, ends_at, trial_ends_at, custom_data, snapshot, created_at, updated_at, deleted_at
"""
_WRITABLE_COLUMNS = [
    "provider", "provider_object", "provider_ref", "billing", "user_id", "target_type", "target_id", "product", "plan",
    "product_data", "status", "renews_at", "ends_at", "trial_ends_at", "custom_data", "snapshot", "created_at",
]  # fmt: skip
# What a row is -- whose, for what, which object at the provider -- never changes once it's there.
_FIXED_COLUMNS = {"provider", "provider_object", "provider_ref", "billing", "user_id", "target_type", "target_id"}


async def find(criteria: dict[str, Any], include_deleted: bool = False) -> list[dict]:
    """Subscriptions matching `criteria` (column = value, ANDed), oldest first. A target's ({"target_type": ..,
    "target_id": ..}) rides idx_subscriptions_target."""
    where = pydash.pick(criteria, "id", *_WRITABLE_COLUMNS)
    not_deleted = "" if include_deleted else " AND deleted_at IS NULL"
    sql = f"SELECT {_COLUMNS} FROM subscriptions WHERE {postgres.to_where_clause(where)}{not_deleted} ORDER BY created_at, id"
    return await postgres.fetch(sql, where)


async def find_by_targets(target_type: str, target_ids: list[int]) -> list[dict]:
    """The not-deleted subscriptions of any of these targets, oldest first. Rides idx_subscriptions_target."""
    if not target_ids:
        return []
    sql = f"""
        SELECT {_COLUMNS} FROM subscriptions
        WHERE target_type = $target_type AND target_id = ANY($target_ids) AND deleted_at IS NULL
        ORDER BY created_at, id
    """
    return await postgres.fetch(sql, {"target_type": target_type, "target_ids": target_ids})


async def find_stale(statuses: Sequence[str], updated_before: datetime, providers: Sequence[str]) -> list[dict]:
    """Not-deleted subscriptions from one of `providers`, in one of `statuses`, last written before `updated_before`,
    oldest write first."""
    sql = f"""
        SELECT {_COLUMNS} FROM subscriptions
        WHERE status = ANY($statuses) AND updated_at < $updated_before AND provider = ANY($providers)
            AND deleted_at IS NULL
        ORDER BY updated_at
    """
    return await postgres.fetch(
        sql, {"statuses": list(statuses), "updated_before": updated_before, "providers": list(providers)}
    )


async def find_by_checkout_ids(checkout_ids: list[str]) -> list[dict]:
    """Subscriptions started by any of these checkouts (custom_data.checkout_id)."""
    if not checkout_ids:
        return []
    sql = f"SELECT {_COLUMNS} FROM subscriptions WHERE custom_data->>'checkout_id' = ANY($checkout_ids)"
    return await postgres.fetch(sql, {"checkout_ids": checkout_ids})


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


async def insert(row: dict[str, Any]) -> dict:
    """Insert a subscription we made ourselves (an admin's), not one a provider told us about (upsert)."""
    values = pydash.pick(row, *_WRITABLE_COLUMNS)
    inserted = await postgres.fetchrow(
        f"INSERT INTO subscriptions {postgres.to_insert_clause(values)} RETURNING {_COLUMNS}", values
    )
    assert inserted is not None  # an insert always returns its row
    return inserted


async def update_one(subscription_id: int, changes: dict[str, Any]) -> dict | None:
    """Overwrite some of a subscription's columns. None if there's no such row."""
    values = pydash.pick(changes, *_WRITABLE_COLUMNS)
    sql = f"""
        UPDATE subscriptions SET {postgres.to_set_clause(values)}, updated_at = now()
        WHERE id = $subscription_id RETURNING {_COLUMNS}
    """
    return await postgres.fetchrow(sql, {**values, "subscription_id": subscription_id})


async def delete(subscription_id: int) -> None:
    """Soft-delete a subscription: it's kept, with deleted_at set, and every read leaves it out from then on."""
    await postgres.execute(
        "UPDATE subscriptions SET deleted_at = now(), updated_at = now() WHERE id = $subscription_id AND deleted_at IS NULL",
        {"subscription_id": subscription_id},
    )
