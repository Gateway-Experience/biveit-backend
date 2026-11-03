# Intentions: data access for `ai_usage` -- the agents' model calls, one row each (migrations/0013_ai_usage.sql).
# Append-only: insert, and sums over a time range.

from datetime import datetime
from typing import Any

import pydash

from src.utils import postgres

_WRITABLE_COLUMNS = [
    "user_id",
    "project_id",
    "conversation_id",
    "ip_group",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
]


async def insert(data: dict[str, Any]) -> None:
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    # Columns are whitelisted above, so splicing their names in is safe.
    await postgres.execute(
        f"INSERT INTO ai_usage ({', '.join(row)}) VALUES ({', '.join(f'${column}' for column in row)})", row
    )


async def find_totals(since: datetime, user_id: int | None = None, ip_group: str | None = None) -> list[dict]:
    """Tokens used since `since` by a user or by an IP group (exactly one of them), summed per model:
    [{model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}]. Per model, since that's what
    a price applies to."""
    column, value = ("user_id", user_id) if user_id is not None else ("ip_group", ip_group)
    # `column` is one of two names fixed above, so splicing it in is safe.
    sql = f"""
        SELECT model,
               sum(input_tokens)::bigint AS input_tokens,
               sum(output_tokens)::bigint AS output_tokens,
               sum(cache_read_tokens)::bigint AS cache_read_tokens,
               sum(cache_write_tokens)::bigint AS cache_write_tokens
        FROM ai_usage
        WHERE {column} = $value AND created_at >= $since
        GROUP BY model
    """
    return await postgres.fetch(sql, {"value": value, "since": since})
