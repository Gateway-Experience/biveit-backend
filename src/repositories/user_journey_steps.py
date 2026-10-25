# Intentions: data access for the `user_journey_steps` table -- the steps users take through the FE's
# journeys (the flows it walks a user through step by step). Schema: migrations/0009_user_journey_steps.sql.
# Append-only in spirit: a step is inserted, and only ever soft-deleted (going back), never edited. Every
# read skips soft-deleted rows.

from typing import Any, cast

import pydash

from src.dto.users import UserJourneyStep
from src.utils import postgres

_COLUMNS = "id, user_id, journey, step, data, created_at, deleted_at"

_WRITABLE_COLUMNS = ["user_id", "journey", "step", "data"]


async def insert(data: dict[str, Any]) -> UserJourneyStep:
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    inserted = await postgres.fetchrow(
        f"INSERT INTO user_journey_steps {postgres.to_insert_clause(row)} RETURNING {_COLUMNS}", row
    )
    return cast("UserJourneyStep", inserted)


async def find_latest(user_id: int) -> list[UserJourneyStep]:
    """The user's latest live step in each journey they've started -- where they are now in each. Rides
    idx_user_journey_steps_user_journey."""
    sql = f"""
        SELECT DISTINCT ON (journey) {_COLUMNS} FROM user_journey_steps
        WHERE user_id = $user_id AND deleted_at IS NULL
        ORDER BY journey, created_at DESC, id DESC
    """
    return cast("list[UserJourneyStep]", await postgres.fetch(sql, {"user_id": user_id}))
