# Intentions: data access for `kv_store` -- a generic durable key-value table. One row per key; `value`
# is an opaque jsonb blob owned by whoever owns the key's namespace. Keys are namespaced strings, e.g.
# "<domain>:<id>:<scope>" (a sync watermark, a checkpoint, a cursor). Schema: migrations/0004_kv_store.sql.
#
# WHY THIS AND NOT REDIS: persistence. Redis (utils/cache) holds derived data that may vanish -- if
# the cache is cold you recompute and get the same answer. What goes here is state you CANNOT rebuild:
# how far a job got, what you've already processed. Losing it means redoing or skipping real work.
# That's the whole distinction; if a value fails it, it belongs in the cache instead.
#
# System-wide by design (no tenant/user scoping): the KEY encodes whatever scope it needs.
# NO CONSUMER YET -- carried in ahead of need, for the first flow that needs durable progress state.

from typing import Any

from src.utils import postgres

_SET = """
    INSERT INTO kv_store (key, value)
    VALUES ($key, $value)
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
"""


async def get(key: str) -> Any | None:
    """The value stored under `key`, or None if the key doesn't exist."""
    row = await postgres.fetchrow("SELECT value FROM kv_store WHERE key = $key", {"key": key})
    return row["value"] if row else None


async def set(key: str, value: Any) -> None:
    """Upsert `value` under `key`. Idempotent -- writing the same key twice just overwrites, which is
    what a watermark or checkpoint wants."""
    await postgres.execute(_SET, {"key": key, "value": value})


async def delete(key: str) -> None:
    """Drop `key` (no-op if absent)."""
    await postgres.execute("DELETE FROM kv_store WHERE key = $key", {"key": key})
