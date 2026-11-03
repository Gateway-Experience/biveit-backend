# The daily AI limits (services/agent/usage): grouping IPs, reading an answer's usage off the stream, pricing it,
# refusing a new message once a limit's used up (answers still go through), and the model proxy metering every
# answer and refusing calls past the hard limit. Repos and the model endpoint are faked.

import asyncio
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from src.app import create_listener_app
from src.config import settings
from src.constants import AI_MODEL_PRICES, AI_UNKNOWN_MODEL_PRICE, AiMessageRole
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import ai_usage as ai_usage_repo
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services import system_config
from src.services.agent import conversations, tokens, usage

_CONVERSATION = {"id": 5, "project_id": 3, "user_id": 7}
_USER_MESSAGE = {"id": 1, "seq": 1, "role": "user", "meta": {"client_ip": "203.0.113.9"}}
# A day's spend on deepseek-flash: 10M fresh input tokens = $1.40.
_FLASH_SPEND = {"model": "deepseek-flash", "input_tokens": 10_000_000, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}  # fmt: skip


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


_MESSAGE_START = {
    "type": "message_start",
    "message": {
        "model": "deepseek-flash",
        "usage": {"input_tokens": 231, "cache_read_input_tokens": 738432, "cache_creation_input_tokens": 0, "output_tokens": 1},
    },
}  # fmt: skip
_STREAMED_ANSWER = _sse(
    _MESSAGE_START,
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Done!"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1587}},
    {"type": "message_stop"},
)


@pytest.fixture
def spend(monkeypatch):
    """Today's spend, per user id and per IP group: {"user": {7: [rows]}, "ip": {"203.0.113.9": [rows]}}."""
    state = {"user": {}, "ip": {}, "inserted": []}

    async def find_totals(since, user_id=None, ip_group=None):
        assert since == datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return state["user"].get(user_id, []) if user_id is not None else state["ip"].get(ip_group, [])

    async def insert(data):
        state["inserted"].append(data)

    async def get_config(name, default=None):
        return default

    async def get_conversation(conversation_id):
        return _CONVERSATION if conversation_id == _CONVERSATION["id"] else None

    async def get_latest(conversation_id, role):
        assert role == AiMessageRole.USER
        return _USER_MESSAGE

    monkeypatch.setattr(ai_usage_repo, "find_totals", find_totals)
    monkeypatch.setattr(ai_usage_repo, "insert", insert)
    monkeypatch.setattr(system_config, "get", get_config)
    monkeypatch.setattr(ai_conversations_repo, "get", get_conversation)
    monkeypatch.setattr(ai_messages_repo, "get_latest", get_latest)
    return state


# --- the pieces ---


@pytest.mark.parametrize(
    ("ip", "group"),
    [
        ("203.0.113.9", "203.0.113.9"),
        ("2001:db8:abcd:12:1:2:3:4", "2001:db8:abcd:12::/64"),
        ("2001:db8:abcd:12:ffff::1", "2001:db8:abcd:12::/64"),  # the same customer's block
        ("::ffff:203.0.113.9", "203.0.113.9"),  # IPv4 through an IPv6 socket
        ("testclient", None),
        (None, None),
    ],
)
def test_ips_are_grouped_by_what_one_customer_can_hop_around_in(ip, group):
    assert usage.to_ip_group(ip) == group


def test_a_streamed_answers_usage_takes_the_final_counts():
    assert usage.read_usage(_STREAMED_ANSWER) == {
        "model": "deepseek-flash",
        "input_tokens": 231,
        "output_tokens": 1587,
        "cache_read_tokens": 738432,
        "cache_write_tokens": 0,
    }


def test_a_whole_answers_usage_is_read_too():
    body = json.dumps({"model": "claude-haiku-5-5", "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
    assert usage.read_usage(body) == {
        "model": "claude-haiku-5-5",
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


@pytest.mark.parametrize("body", [b"", b'{"type": "error", "error": {"type": "overloaded_error"}}', b"event: ping\n"])
def test_no_usage_in_an_error_or_an_empty_answer(body):
    assert usage.read_usage(body) is None


def test_models_are_priced_by_the_longest_matching_name():
    one_million_out = {"input_tokens": 0, "output_tokens": 1_000_000, "cache_read_tokens": 0, "cache_write_tokens": 0}
    prices = {"claude": {**AI_MODEL_PRICES["claude-opus"]}, **AI_MODEL_PRICES}
    assert usage.to_cost_usd(
        [{**one_million_out, "model": "deepseek-flash"}], prices, AI_UNKNOWN_MODEL_PRICE
    ) == pytest.approx(0.28)
    assert usage.to_cost_usd(
        [{**one_million_out, "model": "claude-sonnet-5-5"}], prices, AI_UNKNOWN_MODEL_PRICE
    ) == pytest.approx(15.0)
    # Never heard of it: priced high, so it can't run up a bill under the limit.
    assert usage.to_cost_usd(
        [{**one_million_out, "model": "mystery-1"}], prices, AI_UNKNOWN_MODEL_PRICE
    ) == pytest.approx(25.0)


# --- the limits ---


async def test_under_both_limits_a_turn_can_start(spend):
    spend["user"][7] = [_FLASH_SPEND]  # $1.40 of $2
    spend["ip"]["203.0.113.9"] = [_FLASH_SPEND]
    await usage.check_daily_limits(7, "203.0.113.9")


async def test_a_user_past_their_limit_waits_till_midnight_utc(spend):
    spend["user"][7] = [_FLASH_SPEND, _FLASH_SPEND]  # $2.80

    with pytest.raises(usage.AiLimitReached) as reached:
        await usage.check_daily_limits(7, "203.0.113.9")
    assert reached.value.scope == "user"
    assert reached.value.resets_at.hour == 0 and reached.value.resets_at > datetime.now(UTC)


async def test_an_ip_past_its_limit_stops_every_account_on_it(spend):
    spend["ip"]["203.0.113.9"] = [_FLASH_SPEND] * 5  # $7 of $6, across accounts; this one's spent nothing

    with pytest.raises(usage.AiLimitReached, match="ip"):
        await usage.check_daily_limits(7, "203.0.113.9")
    await usage.check_daily_limits(7, None)  # no IP to go by: only the user's own limit


async def test_the_hard_limit_is_a_little_past_the_daily_one(spend):
    spend["user"][7] = [{**_FLASH_SPEND, "input_tokens": 15_000_000}]  # $2.10: past the daily $2, under 1.2x
    await usage.check_hard_limits(5)
    spend["user"][7] = [_FLASH_SPEND, _FLASH_SPEND]  # $2.80: past $2.40
    with pytest.raises(usage.AiLimitReached):
        await usage.check_hard_limits(5)


# --- sending a message ---


async def test_text_is_refused_once_a_limit_is_used_up_but_answers_go_through(spend, monkeypatch):
    stored = []

    async def find_messages(conversation_id):
        return []

    async def append(conversation_id, role, content, meta=None):
        stored.append(meta)
        return {"id": 2, "seq": 2, "role": role, "content": content, "meta": meta}

    async def deliver(conversation, message):
        pass

    async def mark_active(conversation_id):
        pass

    async def find_projects(criteria):
        return [{"id": 3, "user_id": 7}]

    monkeypatch.setattr(ai_messages_repo, "find", find_messages)
    monkeypatch.setattr(projects_repo, "find", find_projects)
    monkeypatch.setattr(conversations, "_append", append)
    monkeypatch.setattr(conversations, "_deliver", deliver)
    monkeypatch.setattr(conversations.activity, "mark_active", mark_active)
    spend["ip"]["203.0.113.9"] = [_FLASH_SPEND] * 5

    with pytest.raises(usage.AiLimitReached):
        await conversations.post_user_message(
            _CONVERSATION, {"type": "text", "text": "hi", "attachments": []}, "203.0.113.9"
        )
    assert stored == []  # refused before anything's stored

    # From another network it goes through, and the IP is kept with the message.
    await conversations.post_user_message(
        _CONVERSATION, {"type": "text", "text": "hi", "attachments": []}, "198.51.100.1"
    )
    assert stored == [{"client_ip": "198.51.100.1"}]


# --- the model proxy ---


class _FakeUpstream:
    status = 200

    def __init__(self, body: bytes):
        self.headers = {"content-type": "text/event-stream"}
        self.content = self
        self._body = body
        self.released = False

    async def iter_any(self):
        for start in range(0, len(self._body), 100):
            yield self._body[start : start + 100]

    def release(self):
        self.released = True


@pytest.fixture
def proxy(spend, monkeypatch):
    monkeypatch.setattr(settings, "session_token_secret", "session-token-secret-for-tests")
    upstream = _FakeUpstream(_STREAMED_ANSWER)
    recorded = []

    async def open_model_request(method, path, query, headers, body):
        return upstream

    async def mark_active(conversation_id):
        pass

    monkeypatch.setattr(agent_service, "open_model_request", open_model_request)
    monkeypatch.setattr("src.controllers.agent.activity.mark_active", mark_active)
    # The recording is spawned: capture it, and run it here.
    monkeypatch.setattr(usage, "spawn", recorded.append)
    return {"client": TestClient(create_listener_app()), "upstream": upstream, "recorded": recorded}


def _session():
    return {"Authorization": f"Bearer {tokens.to_session_token(5)}"}


def test_every_answer_is_passed_through_and_metered(proxy, spend):
    response = proxy["client"].post("/api/agent/model/v1/messages", json={}, headers=_session())

    assert response.status_code == 200
    assert response.content == _STREAMED_ANSWER
    assert proxy["upstream"].released
    [recording] = proxy["recorded"]
    asyncio.run(recording)
    assert spend["inserted"] == [
        {
            "model": "deepseek-flash",
            "input_tokens": 231,
            "output_tokens": 1587,
            "cache_read_tokens": 738432,
            "cache_write_tokens": 0,
            "user_id": 7,
            "project_id": 3,
            "conversation_id": 5,
            "ip_group": "203.0.113.9",
        }
    ]


def test_past_the_hard_limit_the_proxy_refuses_without_asking_the_model(proxy, spend):
    spend["user"][7] = [_FLASH_SPEND] * 3

    response = proxy["client"].post("/api/agent/model/v1/messages", json={}, headers=_session())

    # A permission error in the model API's shape: Claude Code would retry a 429 for nothing.
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "permission_error"
    assert not proxy["upstream"].released  # never opened
