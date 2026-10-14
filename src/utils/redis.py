# Intentions: low-level async Redis access. Connection-module convention: connect() once at startup,
# client() for the live handle, close() at shutdown (see utils/postgres for the note). Generic
# operations only -- domain logic belongs in its own module, not here.

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import redis.asyncio as redis

from src.config import settings
from src.utils.common import with_retries

_client: redis.Redis | None = None


async def connect() -> None:
    global _client
    if _client is None:
        # socket_timeout=None: block indefinitely on reads. redis-py 8's pubsub connection otherwise
        # defaults to a 5s socket read timeout, so an IDLE subscription (our early-wake channel sits
        # quiet most of the time) raises network:TimeoutError every 5s. A blocking read on a quiet
        # channel is normal, not an error -- wait forever, let real drops surface via tcp-keepalive.
        _client = redis.from_url(settings.redis_url, socket_timeout=None)


# ci:trivial
def client() -> redis.Redis:
    assert _client is not None, "redis not connected; call connect() at startup"
    return _client


# ci:trivial
async def set_if_absent(key: str, ttl_seconds: int) -> bool:
    """SET key 1 NX EX ttl -> True if newly set, False if the key already existed. Generic op."""
    return bool(await client().set(key, 1, nx=True, ex=ttl_seconds))


# ci:trivial
async def set_flag(key: str, ttl_seconds: int) -> None:
    """SET key 1 EX ttl -- raise a boolean flag with a TTL (idempotent; a re-set just refreshes the TTL). For a
    poll-based signal: a producer raises the flag, a consumer polls flag_exists() at its convenience. Generic op."""
    await client().set(key, 1, ex=ttl_seconds)


# ci:trivial
async def set_value(key: str, value: str, ttl_seconds: int) -> None:
    """SET key value EX ttl -- store a string that expires on its own (re-setting refreshes the TTL). The
    caller owns any encoding. Generic op."""
    await client().set(key, value, ex=ttl_seconds)


# ci:trivial
async def get_value(key: str) -> str | None:
    """GET key -> the string stored there, or None if it's missing (or expired). Generic op."""
    value = await client().get(key)
    return value.decode() if isinstance(value, bytes) else value


# ci:trivial
async def flag_exists(key: str) -> bool:
    """EXISTS key -> whether the flag is currently raised. The poll side of a key-based signal. Generic op."""
    return bool(await client().exists(key))


# ci:trivial
async def delete(key: str) -> None:
    """DEL key -- clear a flag/key (no-op if absent). Generic op."""
    await client().delete(key)


# ci:trivial
async def publish(channel: str, message: str = "") -> None:
    """Fire-and-forget publish to a pub/sub channel. Dropped if no subscriber -- callers must treat
    delivery as best-effort (a missed message is not an error). Generic op."""
    await client().publish(channel, message)


@asynccontextmanager
async def subscription(channel: str) -> AsyncIterator[Callable[[float], Awaitable[str | None]]]:
    """Subscribe to `channel` and yield `receive(timeout_seconds) -> payload | None`, which waits up to the
    timeout for the next message (None on timeout). Unsubscribes/closes on exit. Generic op -- the caller
    decides what a message means.

    Best-effort, like all pub/sub: anything published while not subscribed is lost. Why a receive
    function rather than an async iterator: the natural way to time out an iterator (wait_for around
    anext) CANCELS it, which ends the generator and silently kills the subscription."""
    pubsub = client().pubsub()
    await pubsub.subscribe(channel)

    async def _receive(timeout_seconds: float) -> str | None:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout_seconds)
        if msg is None:
            return None
        data = msg["data"]
        return data.decode() if isinstance(data, bytes) else str(data)

    try:
        yield _receive
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


class LockNotAcquired(Exception):
    """lock() gave up waiting -- someone else held the key for the whole wait."""


@asynccontextmanager
async def lock(key: str, ttl_seconds: int, wait_seconds: float, poll_seconds: float = 0.2) -> AsyncIterator[None]:
    """Hold a simple mutex on `key` for the duration of the block, polling for up to `wait_seconds` to get
    it (raises LockNotAcquired otherwise). Generic op.

    Gotcha: it's not owner-checked. The TTL is only a safety net for a crashed holder, so pick one well
    above how long the block takes -- if the TTL runs out mid-block, another caller can get in, and our
    release would then delete THEIR lock."""

    async def _acquire() -> None:
        if not await set_if_absent(key, ttl_seconds):
            raise LockNotAcquired(key)

    await with_retries(
        _acquire, retries=max(1, int(wait_seconds / poll_seconds)), calculate_backoff=lambda _: poll_seconds
    )
    try:
        yield
    finally:
        await delete(key)


# ci:trivial
async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ci:trivial
async def hash_increment(key: str, field: str, amount: int = 1, ttl_seconds: int | None = None) -> int:
    """HINCRBY key field amount -> the new value, optionally (re)setting the key's TTL. One round trip via a
    pipeline when a TTL is given. Generic op -- the caller decides what the hash means."""
    if ttl_seconds is None:
        return int(await client().hincrby(key, field, amount))
    pipe = client().pipeline()
    pipe.hincrby(key, field, amount)
    pipe.expire(key, ttl_seconds)
    return int((await pipe.execute())[0])


# ci:trivial
async def hash_set(key: str, values: dict, ttl_seconds: int | None = None) -> None:
    """HSET key (multiple field/value pairs), optionally (re)setting the key's TTL. Values are stored as
    Redis strings -- the caller owns any encoding. Generic op."""
    if not values:
        return
    pipe = client().pipeline()
    pipe.hset(key, mapping=values)
    if ttl_seconds is not None:
        pipe.expire(key, ttl_seconds)
    await pipe.execute()


# ci:trivial
async def hash_get_all(key: str) -> dict[str, str]:
    """HGETALL key -> {field: value} with bytes decoded to str. Missing key -> {}. Generic op."""
    raw = await client().hgetall(key)
    return {
        (k.decode() if isinstance(k, bytes) else str(k)): (v.decode() if isinstance(v, bytes) else str(v))
        for k, v in (raw or {}).items()
    }
