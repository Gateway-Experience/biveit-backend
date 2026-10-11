# Intentions: typed access to runtime settings (the system_configurations table), served from an
# in-memory snapshot so a read is a dict lookup rather than a db round trip.
#
# STALENESS: the snapshot refreshes LAZILY, at most every _TTL_SECONDS -- a read after the window has
# lapsed reloads first, so a setting changed in the db takes effect within 15 minutes with nothing
# scheduled and no background task. force_load() skips the wait (startup, and the admin reload
# endpoint) when you don't want to sit out the window.
#
# Consequence worth knowing: because a refresh is I/O, get() is ASYNC -- unlike a pure boot-snapshot
# cache, you must await a config read. A refresh FAILURE is swallowed here (logged, then the previous
# snapshot is served): config reads sit inside request paths, and a db hiccup shouldn't turn a working
# request into a 500 when a slightly stale value would have done.

import asyncio
import time
from typing import Any

from src.repositories import system_configurations
from src.utils.logging import log_error

_TTL_SECONDS = 900  # 15 minutes

_cache: dict[str, Any] = {}
_loaded_at: float = 0.0
# Serializes refreshes: without it, N concurrent reads arriving on an expired cache would each fire
# their own query. The first caller in reloads; the rest await it and then find the cache fresh.
_refresh_lock = asyncio.Lock()


async def force_load() -> int:
    """Refresh the snapshot NOW, regardless of the TTL, and return how many settings were loaded.
    Called at startup (so the first request is warm) and by the admin reload endpoint. Unlike the lazy
    path this does NOT swallow failures -- an explicit reload should tell its caller it failed."""
    global _cache, _loaded_at
    async with _refresh_lock:
        _cache = await system_configurations.get_all_enabled()
        _loaded_at = time.monotonic()
        return len(_cache)


async def _refresh_if_stale() -> None:
    """Reload if the snapshot has aged past the TTL. Best-effort: on failure we log and keep serving
    the old snapshot (see the module note). Re-checks freshness after taking the lock, so callers that
    queued behind a refresh don't each redo it."""
    global _cache, _loaded_at
    if time.monotonic() - _loaded_at < _TTL_SECONDS:
        return
    try:
        async with _refresh_lock:
            if time.monotonic() - _loaded_at < _TTL_SECONDS:
                return
            _cache = await system_configurations.get_all_enabled()
            _loaded_at = time.monotonic()
    except Exception as error:
        # Swallow point: this error stops here rather than bubbling, so it gets logged here.
        log_error(error, "system config refresh failed; serving the previous snapshot")


async def get(name: str, default: Any = None) -> Any:
    """The value of setting `name`, or `default` if it's absent or DISABLED. Refreshes the snapshot
    first if it's stale."""
    await _refresh_if_stale()
    return _cache.get(name, default)


async def get_int(name: str, default: int = 0) -> int:
    """get(), coerced to int. A value that won't coerce falls back to `default` rather than raising --
    a typo in an operator-edited setting shouldn't take down the caller."""
    value = await get(name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def get_all() -> dict[str, Any]:
    """The whole snapshot. For a caller that wants every setting at once (the config endpoint) rather
    than a key at a time."""
    await _refresh_if_stale()
    return dict(_cache)
