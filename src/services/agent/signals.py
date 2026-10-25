# Intentions: signals -- timestamped markers in a conversation's log (BUILDING, IMPLEMENTATION_DONE...),
# each alone in an ASSISTANT message. They replace state we used to keep on the side (the building flag in
# redis): the log is the only record, and whatever's current is read off its tail. The agent sends a few
# itself (AGENT_SIGNALS, through POST /agent/signals); the rest we write when we make the thing happen.
# References: SIGNAL_BLOCK_TYPE / AiSignal in constants.py (the shape and the slugs).

from typing import Any

import pydash

from src.constants import SIGNAL_BLOCK_TYPE, SIGNAL_TAIL_SIZE, AiMessageRole, AiSignal
from src.dto.ai_conversations import AiMessage
from src.repositories import ai_messages as ai_messages_repo
from src.services.agent import wakeups


def to_signal_block(slug: AiSignal, description: str | None = None, meta: dict[str, Any] | None = None) -> dict:
    """[Pure] A signal as the content block we store. Empty description/meta are left out."""
    return {
        "type": SIGNAL_BLOCK_TYPE,
        "slug": slug,
        **({"description": description} if description else {}),
        **({"meta": meta} if meta else {}),
    }


def is_signal_message(message: AiMessage) -> bool:
    """[Pure] Whether a message is a signal rather than something said. Anything looking for "the last
    message" in the chat sense has to skip these."""
    return bool(message["content"]) and all(block.get("type") == SIGNAL_BLOCK_TYPE for block in message["content"])


def is_build_open(messages: list[AiMessage]) -> bool:
    """[Pure] Whether the latest BUILDING / BUILDING_DONE in `messages` (in seq order) is a BUILDING. No
    expiry here: it's for deciding whether a BUILDING_DONE is owed, and a spare one is harmless."""
    build_slugs = [
        block["slug"]
        for message in messages
        for block in message["content"]
        if block.get("type") == SIGNAL_BLOCK_TYPE and block.get("slug") in (AiSignal.BUILDING, AiSignal.BUILDING_DONE)
    ]
    return pydash.last(build_slugs) == AiSignal.BUILDING


def is_turn_open(messages: list[AiMessage]) -> bool:
    """[Pure] Whether the agent still owes an AGENT_IDLE in `messages` (in seq order): the user said
    something after the latest one (or there's none yet). A question waiting on the user counts as open
    too -- the runner holds its turn while it waits."""
    last_user_seq = pydash.last([message["seq"] for message in messages if message["role"] == AiMessageRole.USER])
    idle_seqs = [
        message["seq"]
        for message in messages
        if any(
            block.get("type") == SIGNAL_BLOCK_TYPE and block.get("slug") == AiSignal.AGENT_IDLE
            for block in message["content"]
        )
    ]
    return last_user_seq is not None and last_user_seq > (pydash.last(idle_seqs) or 0)


async def append_signal(
    conversation_id: int, slug: AiSignal, description: str | None = None, meta: dict[str, Any] | None = None
) -> AiMessage:
    """Write a signal as the conversation's next message and wake its tabs, like any new message."""
    message = await ai_messages_repo.insert(
        {
            "conversation_id": conversation_id,
            "role": AiMessageRole.ASSISTANT,
            "content": [to_signal_block(slug, description, meta)],
            "meta": {},
        }
    )
    await wakeups.publish_wakeup(conversation_id, str(message["seq"]))
    return message


async def end_open_turn(conversation_id: int, description: str) -> None:
    """Write an AGENT_IDLE if the agent's turn is still open -- for whatever ends one without the runner
    saying so (the session stopping, or dying), so the user's screen stops showing the agent as working.
    Reads only the log's tail (SIGNAL_TAIL_SIZE)."""
    tail = await ai_messages_repo.find_before(conversation_id, None, SIGNAL_TAIL_SIZE)
    if is_turn_open(tail):
        await append_signal(conversation_id, AiSignal.AGENT_IDLE, description)


async def end_open_build(conversation_id: int, description: str) -> None:
    """Write a BUILDING_DONE if a build is still open -- for whatever ends one without the agent saying so
    (the session stopping, the user signing off). Reads only the log's tail (SIGNAL_TAIL_SIZE)."""
    tail = await ai_messages_repo.find_before(conversation_id, None, SIGNAL_TAIL_SIZE)
    if is_build_open(tail):
        await append_signal(conversation_id, AiSignal.BUILDING_DONE, description)
