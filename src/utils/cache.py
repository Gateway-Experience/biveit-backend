# Intentions: a tiny JSON cache over Redis -- read-through via with_cache(), plus the get/set/clear
# primitives for callers that need to invalidate. For EXPENSIVE READS whose staleness is acceptable for a
# while (an upstream API call too slow to repeat per row on every page load).
#
# NOT a store. Everything here is derived data with a TTL -- if the cache is empty, cold, or wrong, the
# caller recomputes and gets the same answer. Never cache something you can't rebuild from its source, and
# never read a cached value to make a decision the source could contradict.
#
# Values go through json.dumps, so only JSON-shaped data (dict/list/str/int/bool/None) round-trips --
# a datetime or Decimal raises on write. Callers cache shaped, already-serializable results.
# References: utils/redis (the client).

import json

from src.utils import redis
from src.utils.logging import log_error


# ci:trivial
async def get_cache(key: str):
    """The cached value, or None if absent/expired. None is indistinguishable from a cached null -- that's
    why with_cache refuses to store None (see below)."""
    cached = await redis.client().get(key)
    if cached is None:
        return None
    # bytes today (the client doesn't set decode_responses), but json.loads takes either -- so this doesn't
    # break if that ever changes.
    return json.loads(cached)


# ci:trivial
async def set_cache(key: str, value, expires_in: int) -> None:
    """Store `value` as JSON under `key` for `expires_in` SECONDS."""
    await redis.client().setex(key, expires_in, json.dumps(value).encode("utf-8"))


# ci:trivial
async def clear_cache(key: str) -> None:
    """Drop `key` -- the invalidation hook. Call this from any flow that makes a cached value wrong, so the
    next read recomputes instead of serving a stale answer until the TTL lapses.

    NEVER RAISES. Invalidation is appended to flows whose real work is already done (a connect that
    committed, a subscribe that reached Amazon), so a redis hiccup must not turn a succeeded operation into
    a 500. The cost of failing quietly is bounded: the value expires on its own TTL anyway."""
    try:
        await redis.client().delete(key)
    except Exception as e:
        log_error(e, f"cache invalidation failed for {key}")


# ci:trivial
async def with_cache(key: str, expires_in: int, func):
    """Read-through cache: return the cached value for `key`, else await `func()`, store, and return it.

    `func` is a zero-arg async callable, so the expensive work is only STARTED on a miss (passing an
    already-created coroutine would run it either way).

    A None result is NOT cached -- None is how get_cache reports a miss, so storing it would be
    indistinguishable from an empty cache and would just be recomputed anyway. It also means a failed
    computation that returns None retries next time rather than being pinned for the whole TTL."""
    cached = await get_cache(key)
    if cached is not None:
        return cached

    result = await func()
    if result is not None:
        await set_cache(key, result, expires_in)
    return result
