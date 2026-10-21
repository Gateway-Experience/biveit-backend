# Intentions: whether a conversation is in use right now -- what lets an idle project's container be stopped
# (services/projects.stop_idle_projects). Nothing is stored but a flag per conversation in Redis that expires
# DEVENV_IDLE_STOP_SECONDS after it was last raised; anything that shows the conversation is in use raises it
# again:
#   the project being opened, a user message      (services/projects, services/agent/conversations)
#   every call the agent makes to us -- messages, signals, dev servers, MODEL calls -- so an agent busy on
#   a long build counts as activity even with nobody watching  (controllers/agent)
#   an open chat tab, at each heartbeat of its event stream  (controllers/conversations)
# Best effort: a Redis hiccup only means a project might be stopped early or late, never an error to anyone.

from src.constants import DEVENV_IDLE_STOP_SECONDS
from src.utils import redis
from src.utils.logging import log_error


def _get_key(conversation_id: int) -> str:
    return f"devenv:active:{conversation_id}"


async def mark_active(conversation_id: int) -> None:
    """The conversation is in use: (re)start its DEVENV_IDLE_STOP_SECONDS. Never raises (see the top)."""
    try:
        await redis.set_flag(_get_key(conversation_id), DEVENV_IDLE_STOP_SECONDS)
    except Exception as error:
        log_error(error, "couldn't mark a conversation active", {"conversation_id": conversation_id})


async def is_any_active(conversation_ids: list[int]) -> bool:
    """Whether any of these conversations was in use within DEVENV_IDLE_STOP_SECONDS."""
    return any([await redis.flag_exists(_get_key(conversation_id)) for conversation_id in conversation_ids])
