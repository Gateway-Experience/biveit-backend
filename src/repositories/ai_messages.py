# Intentions: data access for the `ai_messages` table -- the append-only log of each conversation.
# Schema: migrations/0007_ai_messages.sql. Insert-only on purpose: messages are never updated.

import random
from typing import Any, cast

import asyncpg
import pydash

from src.constants import AI_MESSAGE_SEQ_ATTEMPTS
from src.dto.ai_conversations import AiMessage
from src.utils import postgres
from src.utils.common import with_retries

_COLUMNS = "id, conversation_id, seq, role, content, model, stop_reason, usage, meta, created_at"

# `seq` is absent on purpose: insert assigns it (see below), callers never pick it.
_WRITABLE_COLUMNS = ["conversation_id", "role", "content", "model", "stop_reason", "usage", "meta"]


async def find(conversation_id: int, after_seq: int = 0, limit: int | None = None) -> list[AiMessage]:
    """A conversation's messages in order, optionally only those after `after_seq` and at most `limit` of
    them (None = all) -- the read for a client catching up from what it has. Rides the
    (conversation_id, seq) unique index."""
    sql = f"""
        SELECT {_COLUMNS} FROM ai_messages
        WHERE conversation_id = $conversation_id AND seq > $after_seq
        ORDER BY seq
        LIMIT $limit
    """
    params = {"conversation_id": conversation_id, "after_seq": after_seq, "limit": limit}
    return cast("list[AiMessage]", await postgres.fetch(sql, params))


# The page is the `limit` messages before the cursor (newest_before), pulled back to the start of the turn
# its first message is in, so it never begins between the agent asking and the user answering: the
# answer would come without its question. A turn starts at a user message that isn't an answer, or at the
# first message after any user message -- i.e. the questions (plus any signals/text the agent added after
# them) and their answer always land on the same page. $before_seq NULL = no cursor (the newest page).
# LIMIT NULL = no limit.
_FIND_BEFORE = f"""
    WITH newest_before AS (
        SELECT seq FROM ai_messages
        WHERE conversation_id = $conversation_id AND ($before_seq::int IS NULL OR seq < $before_seq::int)
        ORDER BY seq DESC
        LIMIT $limit
    ),
    turn_starts AS (
        SELECT seq FROM (
            SELECT seq, role, content, lag(role) OVER (ORDER BY seq) AS previous_role
            FROM ai_messages
            WHERE conversation_id = $conversation_id AND seq <= (SELECT min(seq) FROM newest_before)
        ) earlier
        WHERE (role = 'user' AND NOT content @> '[{{"type": "tool_result"}}]')
           OR previous_role = 'user'
           OR previous_role IS NULL
    )
    SELECT {_COLUMNS} FROM ai_messages
    WHERE conversation_id = $conversation_id
      AND seq >= coalesce((SELECT max(seq) FROM turn_starts), (SELECT min(seq) FROM newest_before))
      AND ($before_seq::int IS NULL OR seq < $before_seq::int)
    ORDER BY seq
"""


async def find_before(conversation_id: int, before_seq: int | None, limit: int) -> list[AiMessage]:
    """One page of history going BACKWARDS, for scrolling up: the `limit` messages before `before_seq`
    (or the newest `limit` when it's None), in seq order.

    A page never starts between the agent asking and the user answering: it reaches back to the start
    of the turn, so whoever holds the page can always show an answer with its question. Such a page can
    come back a little longer than `limit`."""
    params = {"conversation_id": conversation_id, "before_seq": before_seq, "limit": limit}
    return cast("list[AiMessage]", await postgres.fetch(_FIND_BEFORE, params))


async def insert(data: dict[str, Any]) -> AiMessage:
    """Append a message to its conversation as the next seq, and return it.

    seq is computed in the same statement (max + 1). Two writers racing on one conversation can compute
    the same seq; UNIQUE (conversation_id, seq) rejects the loser, which simply tries again."""
    row = pydash.pick(data, *_WRITABLE_COLUMNS)
    # Columns are whitelisted above, so splicing their names in is safe.
    sql = f"""
        INSERT INTO ai_messages ({", ".join(row)}, seq)
        VALUES ({", ".join(f"${column}" for column in row)},
                (SELECT coalesce(max(seq), 0) + 1 FROM ai_messages WHERE conversation_id = $conversation_id))
        RETURNING {_COLUMNS}
    """
    inserted = await with_retries(
        lambda: postgres.fetchrow(sql, row),
        retries=AI_MESSAGE_SEQ_ATTEMPTS,
        calculate_backoff=lambda attempt: random.uniform(0, 0.01 * (attempt + 1)),
        should_retry=lambda error, _: isinstance(error, asyncpg.UniqueViolationError),
    )
    return cast("AiMessage", inserted)


# Everything the user typed to describe their project: the starting form's description (their very first
# prompt, kept on the project) and the text of their chat messages. Answers to the agent's questions
# (tool_result blocks) are picks from its own options, so they're left out. One row per project -- its
# earliest match. No text index yet: this scans the user's own messages, fine while those stay small.
_FIND_PROMPT_MATCHES = """
    WITH matches AS (
        SELECT projects.id AS project_id, projects.bootstrap_data->>'description' AS matched_text, 0 AS seq
        FROM projects
        WHERE projects.user_id = $user_id AND projects.deleted_at IS NULL
          AND projects.bootstrap_data->>'description' ILIKE $pattern ESCAPE '\\'
        UNION ALL
        SELECT conversations.project_id, block->>'text', messages.seq
        FROM ai_conversations conversations
        JOIN projects ON projects.id = conversations.project_id AND projects.deleted_at IS NULL
        JOIN ai_messages messages ON messages.conversation_id = conversations.id AND messages.role = 'user'
        CROSS JOIN LATERAL jsonb_array_elements(messages.content) block
        WHERE conversations.user_id = $user_id AND conversations.deleted_at IS NULL
          AND block->>'type' = 'text' AND block->>'text' ILIKE $pattern ESCAPE '\\'
    )
    SELECT DISTINCT ON (project_id) project_id, matched_text FROM matches ORDER BY project_id, seq
"""


async def find_prompt_matches(user_id: int, text: str) -> list[dict]:
    """The user's projects whose prompts contain `text` (case-insensitive, taken literally -- % and _
    match themselves), as [{project_id, matched_text}] where matched_text is the whole prompt it was
    found in."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return await postgres.fetch(_FIND_PROMPT_MATCHES, {"user_id": user_id, "pattern": f"%{escaped}%"})
