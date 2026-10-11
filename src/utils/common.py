# Intentions: small shared helpers used across the codebase. The coercion/formatting helpers are pure;
# spawn + with_retries are generic async control-flow helpers (they touch the event loop, so not pure).

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from src.constants import Charset
from src.utils.logging import log_error

# Strong references to in-flight fire-and-forget tasks (see spawn). asyncio keeps only a WEAK ref to a
# task, so a bare create_task with no ref can be garbage-collected mid-run and silently vanish; holding
# the ref here keeps it alive. Self-pruning: each task's done-callback discards itself, so the set only
# ever holds CURRENTLY-running spawned tasks (naturally small).
_background_tasks: set[asyncio.Task] = set()


# ci:trivial
def spawn(coro: Awaitable[Any]) -> asyncio.Task:
    """Fire-and-forget: run `coro` in the background and return immediately (don't await it). Keeps a
    strong ref until the task finishes (asyncio's weak-ref-to-tasks gotcha) and, on completion, removes
    that ref -- so there's no leak whether the task SUCCEEDS, RAISES, or is CANCELLED (done-callbacks
    fire on any terminal state). A task that raised has its exception LOGGED here (via log_error) rather
    than silently swallowed -- so a fire-and-forget error is never lost. Returns the Task (usually
    ignored)."""
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_if_failed)
    return task


async def drain_spawned() -> None:
    """Await all currently-outstanding spawn()ed tasks to completion (a snapshot -- tasks spawned AFTER
    this call aren't awaited). Exceptions are already logged by the done-callback, so a failed task
    doesn't raise here. For graceful shutdown / test teardown where lingering background work must
    finish before proceeding."""
    pending = [t for t in _background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _log_if_failed(task: asyncio.Task) -> None:
    # Surface a fire-and-forget failure: a cancelled task is expected (no log); a task that RAISED has
    # its exception logged (and marked retrieved, so asyncio doesn't also warn "exception never
    # retrieved"). This is a swallow point -- the error stops here, so it gets logged here.
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log_error(exc, meta={"source": "spawn"})


# ci:trivial
def generate_code(charset: str = Charset.NUMERIC, length: int = 6) -> str:
    """[Pure-ish] A random code of `length` characters drawn from `charset`. Uses `secrets`, not
    `random`, because these end up as usernames/tokens -- a predictable sequence there is a real
    weakness, and the cost of the stronger generator is nil at these sizes."""
    return "".join(secrets.choice(charset) for _ in range(length))


# ci:trivial
def to_utc(dt: datetime) -> datetime:
    """[Pure] Coerce a datetime to UTC. A naive datetime is ASSUMED to already be UTC (the storage
    convention: everything in the db is UTC), an aware one is converted."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


# ci:trivial
def utc_now() -> datetime:
    """[Pure-ish] Now, as an aware UTC datetime. Use this instead of datetime.now() so timestamps are
    never accidentally local-time."""
    return datetime.now(UTC)


# ci:trivial
def exponential_backoff(base: float, *, rate: float = 2.0, cap: float | None = None) -> Callable[[int], float]:
    """[Pure] Build a backoff schedule for with_retries: attempt -> base * rate**attempt (0-based
    attempt), optionally capped at `cap` seconds. rate=2 is plain doubling; a `cap` bounds a single wait
    (e.g. 3600 for a 1h ceiling). base=0 -> always 0 (immediate retry)."""
    return lambda attempt: min(base * (rate**attempt), cap) if cap is not None else base * (rate**attempt)


async def with_retries[T](
    payload: Callable[[], Awaitable[T]],
    retries: int = 4,
    *,
    calculate_backoff: Callable[[int], float] | None = None,
    should_retry: Callable[[Exception, int], bool] | None = None,
) -> T:
    """Call the async thunk `payload`, retrying up to `retries` total attempts. The last failure is
    re-raised (caller decides what to do).

    `calculate_backoff(attempt) -> seconds`: how long to wait BEFORE the next attempt, given the 0-based
    attempt index that just failed (0 = the first try failed). The CALLER owns the backoff policy
    entirely (constant, exponential, capped, jittered -- whatever). None -> no wait (retry immediately).
    Use the `exponential_backoff(...)` factory above for the common case.

    `should_retry(exc, attempt)`: decides whether a given failure is retryable. Return False to re-raise
    IMMEDIATELY without consuming the remaining attempts -- e.g. retry a transient 503 but give up on a
    400 at once. Default (None) retries on ANY exception. Note: the last attempt always re-raises
    regardless, so should_retry is only consulted when a retry could still happen."""
    if retries < 1:
        raise ValueError("retries must be >= 1")

    for attempt in range(retries):
        try:
            return await payload()
        except Exception as exc:
            if attempt == retries - 1 or (should_retry is not None and not should_retry(exc, attempt)):
                raise
            wait = calculate_backoff(attempt) if calculate_backoff is not None else 0
            if wait:
                await asyncio.sleep(wait)

    raise AssertionError("unreachable")  # for type-checkers; loop always returns or raises
