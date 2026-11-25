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


# What every sum here returns, so it can be priced: the token sums, and when (UTC) they were used -- a price can depend
# on the hour (services/agent/usage, peak hours). Grouped by those two as well, so one group is at most an hour of a
# weekday. Each query below splices it in, with _PRICING_GROUP. Qualified, as some queries join tables that have a
# created_at of their own.
_PRICED_COLUMNS = """
    extract(isodow FROM ai_usage.created_at AT TIME ZONE 'UTC')::int AS utc_weekday,
    extract(hour FROM ai_usage.created_at AT TIME ZONE 'UTC')::int AS utc_hour,
    sum(ai_usage.input_tokens)::bigint AS input_tokens,
    sum(ai_usage.output_tokens)::bigint AS output_tokens,
    sum(ai_usage.cache_read_tokens)::bigint AS cache_read_tokens,
    sum(ai_usage.cache_write_tokens)::bigint AS cache_write_tokens
"""
_PRICING_GROUP = "utc_weekday, utc_hour"


async def insert(data: dict[str, Any]) -> None:
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    # Columns are whitelisted above, so splicing their names in is safe.
    await postgres.execute(
        f"INSERT INTO ai_usage ({', '.join(row)}) VALUES ({', '.join(f'${column}' for column in row)})", row
    )


async def find_totals(since: datetime, user_id: int | None = None, ip_group: str | None = None) -> list[dict]:
    """Tokens used since `since` by a user or by an IP group (exactly one of them), summed per model:
    [{model, utc_weekday, utc_hour, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}]. Per model
    and hour of the week, since that's what a price applies to (_PRICED_COLUMNS)."""
    column, value = ("user_id", user_id) if user_id is not None else ("ip_group", ip_group)
    # `column` is one of two names fixed above, so splicing it in is safe.
    sql = f"""
        SELECT model,
               {_PRICED_COLUMNS}
        FROM ai_usage
        WHERE {column} = $value AND created_at >= $since
        GROUP BY model, {_PRICING_GROUP}
    """
    return await postgres.fetch(sql, {"value": value, "since": since})


async def find_user_totals() -> list[dict]:
    """All tokens ever used, summed per user, model and hour of the week: [{user_id, model, utc_weekday, utc_hour,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}]."""
    sql = f"""
        SELECT user_id, model,
               {_PRICED_COLUMNS}
        FROM ai_usage
        GROUP BY user_id, model, {_PRICING_GROUP}
    """
    return await postgres.fetch(sql)


async def find_project_totals(since: datetime, until: datetime) -> list[dict]:
    """Tokens used in [since, until), summed per project, model and hour of the week, with each project's name and
    owner (deleted ones too -- the spend happened): [{project_id, project_name, user_id, username, first_name,
    last_name, model, utc_weekday, utc_hour, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}]."""
    sql = f"""
        SELECT ai_usage.project_id, projects.name AS project_name, ai_usage.user_id,
               users.username, users.first_name, users.last_name, ai_usage.model,
               {_PRICED_COLUMNS}
        FROM ai_usage
        LEFT JOIN projects ON projects.id = ai_usage.project_id
        LEFT JOIN users ON users.id = ai_usage.user_id
        WHERE ai_usage.created_at >= $since AND ai_usage.created_at < $until
        GROUP BY ai_usage.project_id, projects.name, ai_usage.user_id, users.username, users.first_name,
                 users.last_name, ai_usage.model, {_PRICING_GROUP}
    """
    return await postgres.fetch(sql, {"since": since, "until": until})


async def find_bucket_totals(project_id: int, since: datetime, until: datetime, bucket: str) -> list[dict]:
    """One project's tokens in [since, until), summed per `bucket` (hour | day, in UTC) and model:
    [{bucket_start, model, utc_weekday, utc_hour, input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens}] -- per hour of the week as well, for pricing."""
    if bucket not in ("hour", "day"):
        raise ValueError(f"unknown bucket {bucket}")
    # `bucket` is checked above, so splicing it in is safe.
    sql = f"""
        SELECT date_trunc('{bucket}', created_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS bucket_start, model,
               {_PRICED_COLUMNS}
        FROM ai_usage
        WHERE project_id = $project_id AND created_at >= $since AND created_at < $until
        GROUP BY bucket_start, model, {_PRICING_GROUP}
    """
    return await postgres.fetch(sql, {"project_id": project_id, "since": since, "until": until})
