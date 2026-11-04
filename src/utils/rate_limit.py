# Intentions: keep calls to an upstream API under its requests-per-second cap, so a burst of our own (a dozen
# lookups fired at once by one search, say) queues up for a moment instead of getting us a 429. Process-local --
# fine because the backend runs as one process. It spaces out call STARTS; caps on hourly totals are the caller's
# problem (caching, mostly).

import asyncio
import time

# Per key: the monotonic time the next call may start. Mutable module state on purpose -- it IS the limiter.
_next_start_at: dict[str, float] = {}


async def wait_for_turn(key: str, per_second: float) -> None:
    """Wait until a call under `key` may start, so calls under it start at most `per_second` a second. Each caller
    reserves the next free slot before its first await -- asyncio can't interleave another caller in between, so no
    two get the same slot -- then sleeps until it comes. `key`: one per rate-limited account (e.g. "namecom:live")."""
    now = time.monotonic()
    start_at = max(now, _next_start_at.get(key, now))
    _next_start_at[key] = start_at + 1 / per_second
    await asyncio.sleep(start_at - now)
