# Intentions: what the agents spend on the model, and the daily limits on it.
#
# Metering: every model call goes through our proxy (model_proxy), so that's where it's counted -- each answer's
# usage (read off the stream as it passes through) becomes one ai_usage row: the tokens, the model, who it's
# charged to (the conversation's user and project), and the IP group it's counted against (the IP of the
# conversation's latest user message -- calls come from the project's container, which isn't the user's address).
#
# Limits: a user may spend AI_DAILY_LIMIT_USD_PER_USER a day (UTC), and so may an IP group (an IPv4 address, or an
# IPv6 /64 -- one customer's block) -- the second catches one person rotating accounts. Spend is always summed
# from the rows and priced at read time (AI_MODEL_PRICES), so nothing is stored as a counter and a price change
# applies at once. Checked twice:
#   - when the user sends a message (check_daily_limits): over either limit, it's refused -- before a turn starts,
#     so a turn is never cut off halfway through editing the site. A turn already going finishes.
#   - on every model call (check_hard_limits): past AI_HARD_LIMIT_FACTOR times a limit, the proxy refuses it -- a
#     backstop for a turn that runs away, which should practically never trigger.
# The limits, the hard-limit factor and the prices are all system config (ConfigName), with the constants as
# defaults.

import ipaddress
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pydash

from src.constants import (
    AI_DAILY_LIMIT_USD_PER_IP,
    AI_DAILY_LIMIT_USD_PER_USER,
    AI_HARD_LIMIT_FACTOR,
    AI_MODEL_PRICES,
    AI_UNKNOWN_MODEL_PRICE,
    AiMessageRole,
    ConfigName,
)
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import ai_usage as ai_usage_repo
from src.services import system_config
from src.utils.common import spawn

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
# Our token fields as Anthropic's usage objects name them.
_USAGE_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_write_tokens": "cache_creation_input_tokens",
}


class AiLimitReached(Exception):
    """A daily AI limit is used up. `scope` is which one (user | ip), `resets_at` when it frees up (the next
    00:00 UTC)."""

    def __init__(self, scope: str, resets_at: datetime):
        super().__init__(f"daily AI limit reached ({scope}), resets at {resets_at.isoformat()}")
        self.scope = scope
        self.resets_at = resets_at


def to_ip_group(ip: str | None) -> str | None:
    """[Pure] The group an IP is counted in: an IPv4 address as is, an IPv6 one as its /64 (a customer usually gets
    a whole /64 and can hop around in it). None for nothing, or something that isn't an IP."""
    try:
        address = ipaddress.ip_address(ip or "")
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        # An IPv4 client seen through an IPv6 socket (::ffff:1.2.3.4) is still that IPv4 address.
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{address}/64", strict=False))
    return str(address)


def read_usage(body: bytes) -> dict | None:
    """[Pure] A model answer's usage -- {model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}
    -- from its whole body, streamed (server-sent events) or not (one JSON). Streamed, message_start carries the
    model and the input side, and message_delta the final counts, which win. None when there's none (an error,
    a cut-off stream before message_start)."""
    text = body.decode(errors="replace")
    stream_events = [
        event
        for line in text.splitlines()
        if line.startswith("data:")
        for event in [_parse_json(line.removeprefix("data:").strip())]
        if isinstance(event, dict)
    ]
    if stream_events:
        start = pydash.find(stream_events, {"type": "message_start"}) or {}
        deltas = [event.get("usage") or {} for event in stream_events if event.get("type") == "message_delta"]
        model = pydash.get(start, "message.model")
        usage = pydash.assign({}, pydash.get(start, "message.usage") or {}, *deltas)
    else:
        whole = _parse_json(text)
        model = pydash.get(whole, "model")
        usage = pydash.get(whole, "usage") or {}
    if not model or not usage:
        return None
    return {"model": model, **{ours: int(usage.get(theirs) or 0) for ours, theirs in _USAGE_FIELDS.items()}}


def to_cost_usd(totals: list[dict], prices: dict[str, dict], unknown_model_price: dict) -> float:
    """[Pure] What token totals per model (ai_usage_repo.find_totals' rows) cost, at `prices` (USD per million
    tokens, AI_MODEL_PRICES' shape): each model at the longest price key its name starts with, else
    `unknown_model_price` (AI_UNKNOWN_MODEL_PRICE's shape)."""
    return sum(
        (row[field] or 0) * price[field.removesuffix("_tokens")] / 1_000_000
        for row in totals
        for price in [_find_price(row["model"], prices, unknown_model_price)]
        for field in _TOKEN_FIELDS
    )


async def meter_model_stream(conversation_id: int, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Pass a model answer's chunks straight through, then record its usage (ai_usage) once it's done -- or cut
    off: whatever usage came through is still counted. The recording is spawned, so a closed connection or a db
    hiccup never holds up or breaks the answer."""
    # Kept whole to read the usage at the end: one answer is at most a few MB, and its usage events sit at both
    # ends of it.
    seen: list[bytes] = []
    try:
        async for chunk in chunks:
            seen.append(chunk)  # mutation on purpose: the answer streams through, we keep a copy as it goes
            yield chunk
    finally:
        usage = read_usage(b"".join(seen))
        if usage:
            spawn(record_usage(conversation_id, usage))


async def record_usage(conversation_id: int, usage: dict) -> None:
    """One ai_usage row for a model call made in `conversation_id`, charged to its user and project, counted
    against the IP group of its latest user message. [I/O]"""
    conversation = await ai_conversations_repo.get(conversation_id)
    if conversation is None:
        return
    latest_user_message = await ai_messages_repo.get_latest(conversation_id, AiMessageRole.USER)
    await ai_usage_repo.insert(
        {
            **usage,
            "user_id": conversation["user_id"],
            "project_id": conversation["project_id"],
            "conversation_id": conversation_id,
            "ip_group": to_ip_group(pydash.get(latest_user_message, "meta.client_ip")),
        }
    )


async def check_daily_limits(user_id: int, ip_group: str | None) -> None:
    """Raises AiLimitReached when the user, or their IP group, has used up a daily limit. For the start of a
    turn. [I/O: reads]"""
    await _check_limits(user_id, ip_group, 1)


async def check_hard_limits(conversation_id: int) -> None:
    """Raises AiLimitReached when the conversation's user, or its IP group, is past AI_HARD_LIMIT_FACTOR times a
    daily limit. For every model call: the backstop against a runaway turn. [I/O: reads]"""
    conversation = await ai_conversations_repo.get(conversation_id)
    if conversation is None:
        return
    latest_user_message = await ai_messages_repo.get_latest(conversation_id, AiMessageRole.USER)
    ip_group = to_ip_group(pydash.get(latest_user_message, "meta.client_ip"))
    factor = await _get_number(ConfigName.AI_HARD_LIMIT_FACTOR, AI_HARD_LIMIT_FACTOR)
    await _check_limits(conversation["user_id"], ip_group, factor)


async def _check_limits(user_id: int, ip_group: str | None, factor: float) -> None:
    """Raises AiLimitReached for the first of the user's / the IP group's daily limits (times `factor`) that
    today's spend has reached."""
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    prices = await system_config.get(ConfigName.AI_MODEL_PRICES, AI_MODEL_PRICES)
    unknown_model_price = await system_config.get(ConfigName.AI_UNKNOWN_MODEL_PRICE, AI_UNKNOWN_MODEL_PRICE)
    user_limit = await _get_number(ConfigName.AI_DAILY_LIMIT_USD_PER_USER, AI_DAILY_LIMIT_USD_PER_USER)
    user_spend = to_cost_usd(await ai_usage_repo.find_totals(day_start, user_id=user_id), prices, unknown_model_price)
    if user_spend >= user_limit * factor:
        raise AiLimitReached("user", day_start + timedelta(days=1))
    if ip_group is None:
        return
    ip_limit = await _get_number(ConfigName.AI_DAILY_LIMIT_USD_PER_IP, AI_DAILY_LIMIT_USD_PER_IP)
    ip_spend = to_cost_usd(await ai_usage_repo.find_totals(day_start, ip_group=ip_group), prices, unknown_model_price)
    if ip_spend >= ip_limit * factor:
        raise AiLimitReached("ip", day_start + timedelta(days=1))


async def _get_number(name: ConfigName, default: float) -> float:
    """A number from system config (a limit, the hard-limit factor), or `default` when it's absent or not a number
    -- a typo in the config shouldn't lift the limit or take messages down."""
    try:
        return float(await system_config.get(name, default))
    except (TypeError, ValueError):
        return default


def _find_price(model: str, prices: dict[str, dict], unknown_model_price: dict) -> dict:
    # The longest match, not the first: prices from system config come back from jsonb, which doesn't keep key order.
    matching_keys = [key for key in prices if model.lower().startswith(key)]
    return prices[pydash.max_by(matching_keys, len)] if matching_keys else unknown_model_price


def _parse_json(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None
