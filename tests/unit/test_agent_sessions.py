# Session channel tests (services/agent/sessions): translating our stored messages to the session's
# communication-channel format and back, and pushing a message in. Pure functions are tested directly; the HTTP
# push fakes utils/http.

import pytest

from src.config import settings
from src.constants import AgentRuntime
from src.services.agent import sessions, tokens
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
    # The stored message already answers every question in the order asked (post_user_message's job), so
    # the first runtime's match-by-position works by keeping that order; agent2 matches by tool_use_id.
    content = sessions.to_session_content(
        _msg("user", _answer("q1", {"status": "aborted"}, {"status": "untouched"}), _answer("q2", _PICKED))
    )
    assert content == [
        {"type": "tool_result", "tool_use_id": "q1", "content": [{"status": "aborted"}, {"status": "skipped"}]},
        {"type": "tool_result", "tool_use_id": "q2", "content": [_PICKED]},
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


@pytest.fixture(autouse=True)
def _session_token_secret(monkeypatch):
    monkeypatch.setattr(settings, "session_token_secret", "session-token-secret-for-tests")


async def test_send_retries_until_a_fresh_session_listens(monkeypatch):
    calls = []

    async def flaky_request(method, url, json=None, headers=None, **_):
        calls.append((method, url, json, headers))
        if len(calls) < 3:
            raise ConnectionRefusedError("not listening yet")
        return 202, {}, {}

    monkeypatch.setattr(http, "request", flaky_request)
    monkeypatch.setattr(sessions, "SESSION_SEND_ATTEMPTS", 5)
    monkeypatch.setattr(sessions, "exponential_backoff", lambda *a, **k: lambda attempt: 0)

    await sessions._send_to_port(5, 8899, [{"type": "text", "text": "hi"}])
    assert len(calls) == 3
    # Every push carries the conversation's session token -- the inbox refuses anything without it.
    assert calls[-1] == (
        "POST",
        "http://127.0.0.1:8899/",
        {"content": [{"type": "text", "text": "hi"}]},
        {"Authorization": f"Bearer {tokens.to_session_token(5)}"},
    )


@pytest.mark.parametrize("outcome", ["unreachable", "rejected"])
async def test_send_raises_session_unavailable(outcome, monkeypatch):
    async def request(method, url, json=None, **_):
        if outcome == "unreachable":
            raise ConnectionRefusedError("nobody home")
        return 500, {}, {}

    monkeypatch.setattr(http, "request", request)
    monkeypatch.setattr(sessions, "exponential_backoff", lambda *a, **k: lambda attempt: 0)
    with pytest.raises(sessions.SessionUnavailable):
        await sessions._send_to_port(5, 8899, [{"type": "text", "text": "hi"}])


async def test_the_introduction_carries_the_form_and_the_owners_language(monkeypatch):
    pushed = []

    async def fake_get_user(user_id):
        assert user_id == 7
        return {"id": 7, "language": "vi"}

    async def fake_ensure_session(project, conversation_id):
        return 47000

    async def fake_send(project, conversation_id, content):
        pushed.append(content[0]["text"])

    monkeypatch.setattr(sessions.users_repo, "get", fake_get_user)
    monkeypatch.setattr(sessions, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(sessions, "send", fake_send)

    project = {"id": 1, "user_id": 7, "name": "Bean & Brew", "bootstrap_data": {"kind": "café"}}
    await sessions.introduce_project(project, 5)
    assert "Project name: Bean & Brew" in pushed[0] and '"kind": "café"' in pushed[0]
    assert "Their language: Vietnamese (vi)" in pushed[0]


# --- the runtime switch ---


async def test_the_sdk_runtime_goes_through_agent2(monkeypatch):
    calls = []

    async def fake_ensure(project, conversation_id):
        calls.append(("ensure", conversation_id))

    async def fake_send(project, conversation_id, content):
        calls.append(("send", conversation_id, content))

    async def no_runner(project, conversation_id):
        raise sessions.agent2.RunnerUnavailable("no key")

    monkeypatch.setattr(sessions, "AGENT_RUNTIME", AgentRuntime.SDK)
    monkeypatch.setattr(sessions.agent2, "ensure_session", fake_ensure)
    monkeypatch.setattr(sessions.agent2, "send", fake_send)
    await sessions.send({"id": 3}, 5, [{"type": "text", "text": "hi"}])
    assert calls == [("ensure", 5), ("send", 5, [{"type": "text", "text": "hi"}])]

    monkeypatch.setattr(sessions.agent2, "ensure_session", no_runner)
    with pytest.raises(sessions.SessionUnavailable):  # callers only ever see SessionUnavailable
        await sessions.ensure_session({"id": 3}, 5)


async def test_stopping_stops_both_runtimes(monkeypatch):
    async def fake_adapter_stop(project, conversation_id):
        return {"tmux_session": False, "pids": []}

    async def fake_runner_stop(project, conversation_id):
        return True

    async def no_open_build(conversation_id, description):
        pass

    monkeypatch.setattr(sessions.adapter, "stop_session", fake_adapter_stop)
    monkeypatch.setattr(sessions.agent2, "stop_session", fake_runner_stop)
    monkeypatch.setattr(sessions.signals, "end_open_build", no_open_build)
    stopped = await sessions.stop_session({"id": 3, "current_port": None}, 5)
    assert stopped == {"tmux_session": False, "pids": [], "runner": True}
