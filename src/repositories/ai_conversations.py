# Intentions: data access for the `ai_conversations` table -- a project's chat threads with the agent.
# Schema: migrations/0006_ai_conversations.sql. Every read skips soft-deleted rows.

from datetime import datetime
from typing import Any, cast

import pydash

from src.dto.ai_conversations import AiConversation
from src.utils import postgres

_COLUMNS = "id, project_id, user_id, title, meta, created_at, updated_at, deleted_at"

_WRITABLE_COLUMNS = ["project_id", "user_id", "title", "meta", "deleted_at"]


async def get(conversation_id: int) -> AiConversation | None:
    sql = f"SELECT {_COLUMNS} FROM ai_conversations WHERE id = $conversation_id AND deleted_at IS NULL"
    return cast("AiConversation | None", await postgres.fetchrow(sql, {"conversation_id": conversation_id}))


async def find(criteria: dict[str, Any]) -> list[AiConversation]:
    """Live conversations matching `criteria` (column = value, ANDed), newest first."""
    where = pydash.pick(criteria, *_WRITABLE_COLUMNS)
    sql = f"""
        SELECT {_COLUMNS} FROM ai_conversations
        WHERE {postgres.to_where_clause(where)} AND deleted_at IS NULL
        ORDER BY created_at DESC, id DESC
    """
    return cast("list[AiConversation]", await postgres.fetch(sql, where))


async def insert(data: dict[str, Any]) -> AiConversation:
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    inserted = await postgres.fetchrow(
        f"INSERT INTO ai_conversations {postgres.to_insert_clause(row)} RETURNING {_COLUMNS}", row
    )
    return cast("AiConversation", inserted)


async def claim_meta_key(conversation_id: int, key: str, stale_before: datetime | None = None) -> bool:
    """Set meta[key] to the current time unless the conversation already has it, in ONE statement -- so
    of several callers racing for the same key exactly one gets True. A one-time marker ("this has
    been done"); delete_meta_key gives it back. With `stale_before`, a marker set before that time can be
    claimed again too -- still by exactly one of the racers, since the first one moves it to now."""
    sql = """
        UPDATE ai_conversations SET meta = meta || jsonb_build_object($key::text, now())
        WHERE id = $conversation_id AND deleted_at IS NULL
          AND (NOT meta ? $key::text OR (meta ->> $key::text)::timestamptz < $stale_before::timestamptz)
        RETURNING id
    """
    params = {"conversation_id": conversation_id, "key": key, "stale_before": stale_before}
    return await postgres.fetchrow(sql, params) is not None


async def delete_meta_key(conversation_id: int, key: str) -> None:
    sql = "UPDATE ai_conversations SET meta = meta - $key::text WHERE id = $conversation_id"
    await postgres.execute(sql, {"conversation_id": conversation_id, "key": key})
