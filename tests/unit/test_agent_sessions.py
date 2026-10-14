# Session channel tests (services/agent/sessions): translating our stored messages to the session's
# event-channel format and back, and pushing a message in. Pure functions are tested directly; the HTTP
# push fakes utils/http.

import pytest

from src.services.agent import sessions
from src.utils import http


def _msg(role, *blocks):
    return {"role": role, "content": list(blocks)}


def _answer(tool_id, *answers):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": list(answers)}


_PICKED = {"status": "submitted", "value": {"selected": ["A"], "custom": None}}


# --- to_session_content ---


def test_text_and_attachments_go_as_text_items():
    attachment = {
        "type": "attachment",
        "path": "uploads/ab12_logo.png",
        "name": "logo.png",
        "mime": "image/png",
        "size": 10,
    }
    content = sessions.to_session_content(_msg("user", attachment, {"type": "text", "text": "use this"}))
    assert content == [
        {"type": "text", "text": "[The user attached a file: uploads/ab12_logo.png (logo.png, image/png, 10 bytes)]"},
        {"type": "text", "text": "use this"},
    ]


def test_answers_keep_their_order_and_untouched_becomes_skipped():
    # The stored message already answers every question in the order asked (post_user_message's job),
    # so the channel's match-by-position works by keeping that order.
    content = sessions.to_session_content(
        _msg("user", _answer("q1", {"status": "aborted"}, {"status": "untouched"}), _answer("q2", _PICKED))
    )
    assert content == [
        {"type": "tool_result", "content": [{"status": "aborted"}, {"status": "skipped"}]},
        {"type": "tool_result", "content": [_PICKED]},
    ]


# --- to_stored_content ---


def test_session_tool_uses_get_ids_and_names():
    stored = sessions.to_stored_content(
        [
            {"type": "text", "text": "pick one"},
            {
                "type": "tool_use",
                "input": {"questions": [{"options": ["A"], "multi_select": False, "allow_custom": True}]},
            },
            {"type": "tool_use", "input": {"actions": [{"name": "pick_style", "params": {}}]}},
        ]
    )
    assert stored[0] == {"type": "text", "text": "pick one"}
    assert [block["name"] for block in stored[1:]] == ["ask_user", "request_actions"]
    assert all(block["id"].startswith("toolu_") for block in stored[1:])
    assert stored[1]["id"] != stored[2]["id"]


def test_a_tool_use_that_already_has_an_id_and_name_keeps_them():
    block = {"type": "tool_use", "id": "toolu_given", "name": "ask_user", "input": {"questions": []}}
    assert sessions.to_stored_content([block]) == [block]


# --- send ---


async def test_send_retries_until_a_fresh_session_listens(monkeypatch):
    calls = []

    async def flaky_request(method, url, json=None, **_):
        calls.append((method, url, json))
        if len(calls) < 3:
            raise ConnectionRefusedError("not listening yet")
        return 202, {}, {}

    monkeypatch.setattr(http, "request", flaky_request)
    monkeypatch.setattr(sessions, "SESSION_SEND_ATTEMPTS", 5)
    monkeypatch.setattr(sessions, "exponential_backoff", lambda *a, **k: lambda attempt: 0)

    await sessions.send(8899, [{"type": "text", "text": "hi"}])
    assert len(calls) == 3
    assert calls[-1] == ("POST", "http://127.0.0.1:8899/", {"content": [{"type": "text", "text": "hi"}]})


@pytest.mark.parametrize("outcome", ["unreachable", "rejected"])
async def test_send_raises_session_unavailable(outcome, monkeypatch):
    async def request(method, url, json=None, **_):
        if outcome == "unreachable":
            raise ConnectionRefusedError("nobody home")
        return 500, {}, {}

    monkeypatch.setattr(http, "request", request)
    monkeypatch.setattr(sessions, "exponential_backoff", lambda *a, **k: lambda attempt: 0)
    with pytest.raises(sessions.SessionUnavailable):
        await sessions.send(8899, [{"type": "text", "text": "hi"}])
