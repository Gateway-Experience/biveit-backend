# Intentions: a conversation's wake-up channel on redis -- how its open tabs (the SSE stream,
# conversations.watch_messages) hear that something changed. A wake-up carries no data: it's a cue to
# re-read. Its payload says what to re-read: DEVENV_WAKEUP for the dev environment status, anything else
# (a message's seq) for the message log. Best effort, like all pub/sub -- a lost one only delays a tab
# until its next periodic check or reconnect.
#
# Its own file so everything that changes what tabs show can wake them (conversations, signals,
# devenvs, stages) without importing each other.

from src.constants import DEVENV_WAKEUP
from src.utils import redis
from src.utils.logging import log_error


def get_wakeup_channel(conversation_id: int) -> str:
    return f"ai_conversation:{conversation_id}"


async def publish_wakeup(conversation_id: int, payload: str) -> None:
    """Wake the conversation's tabs with `payload`. Failures are logged and swallowed -- whatever changed
    already happened, and the tabs catch up on their own."""
    try:
        await redis.publish(get_wakeup_channel(conversation_id), payload)
    except Exception as error:
        log_error(error, "conversation wake-up publish failed", {"conversation_id": conversation_id})


async def publish_devenv_changed(conversation_id: int) -> None:
    """Tell the conversation's open tabs its dev environment (or the project's stage) changed, so they
    re-read its status."""
    await publish_wakeup(conversation_id, DEVENV_WAKEUP)
