# Intentions: the agent's "building" flag -- it's in the middle of a big change (the first build, a
# redesign...) and the preview may be half-done meanwhile, so the FE shows a building state. Small
# tweaks don't set it.
#
# Only the agent clears it (its `building` tool, active: false), except that it also goes when the
# session is stopped (sessions.stop_session), and on its own after BUILDING_TTL_SECONDS, so one the agent
# forgot can't stick. Progress updates the agent sends the user meanwhile leave it alone. Setting it again
# restarts the hour -- a long build should do that.
#
# Kept in redis as building:<conversation_id> = {"since", "until", "summary"}: it's short-lived and
# expiring is exactly what redis keys do. `until` is when the key expires, for the FE: the expiry itself
# wakes nobody, so a tab open that long goes by it. Every change wakes the conversation's tabs (`devenv` hint) to re-read it.

import json
from datetime import UTC, datetime, timedelta

import pydash

from src.constants import BUILDING_TTL_SECONDS
from src.services.agent.wakeups import publish_devenv_changed
from src.utils import redis


def _get_key(conversation_id: int) -> str:
    return f"building:{conversation_id}"


async def set_building(conversation_id: int, summary: str | None = None) -> None:
    """Turn the flag on (or refresh it) with an optional one-line summary of what's being built.
    `since` is when this build started, so refreshing keeps the original start."""
    current = await get_building(conversation_id)
    now = datetime.now(UTC)
    building = {
        "since": current["since"] if current else now.isoformat(),
        "until": (now + timedelta(seconds=BUILDING_TTL_SECONDS)).isoformat(),
        # A refresh without a summary keeps the one already there.
        "summary": summary if summary is not None else pydash.get(current, "summary"),
    }
    await redis.set_value(_get_key(conversation_id), json.dumps(building), BUILDING_TTL_SECONDS)
    await publish_devenv_changed(conversation_id)


async def clear_building(conversation_id: int) -> None:
    """Turn the flag off. Idempotent."""
    await redis.delete(_get_key(conversation_id))
    await publish_devenv_changed(conversation_id)


async def get_building(conversation_id: int) -> dict | None:
    """{since, until, summary} while the agent is building, else None."""
    stored = await redis.get_value(_get_key(conversation_id))
    return json.loads(stored) if stored else None
