# Session channel tests (services/agent/sessions): translating our stored messages to the session's
# channel format and back, and the runner (services/agent/runners) underneath. Pure functions are tested directly; the
# runner is faked.

import pytest

from src.services.agent import sessions


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


def test_answers_go_as_they_are_stored():
    # The runner matches answers to its questions by tool_use_id, and knows every status (untouched too).
    content = sessions.to_session_content(
        _msg("user", _answer("q1", {"status": "aborted"}, {"status": "untouched"}), _answer("q2", _PICKED))
    )
    assert content == [
        {"type": "tool_result", "tool_use_id": "q1", "content": [{"status": "aborted"}, {"status": "untouched"}]},
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


# --- the introduction ---


async def test_the_introduction_carries_the_form_and_the_owners_language(monkeypatch):
    pushed = []

    async def fake_get_user(user_id):
        assert user_id == 7
        return {"id": 7, "language": "vi"}

    async def fake_ensure_session(project, conversation_id):
        pass

    async def fake_send(project, conversation_id, content):
        pushed.append(content[0]["text"])

    monkeypatch.setattr(sessions.users_repo, "get", fake_get_user)
    monkeypatch.setattr(sessions, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(sessions, "send", fake_send)

    project = {"id": 1, "user_id": 7, "name": "Bean & Brew", "bootstrap_data": {"kind": "café"}}
    await sessions.introduce_project(project, 5)
    assert "Project name: Bean & Brew" in pushed[0] and '"kind": "café"' in pushed[0]
    assert "Their language: Vietnamese (vi)" in pushed[0]


# --- the runner (services/agent/runners) ---


async def test_sessions_go_through_the_runner(monkeypatch):
    calls = []

    async def fake_ensure(project, conversation_id):
        calls.append(("ensure", conversation_id))

    async def fake_send(project, conversation_id, content):
        calls.append(("send", conversation_id, content))

    async def no_runner(project, conversation_id):
        raise sessions.runners.RunnerUnavailable("no key")

    monkeypatch.setattr(sessions.runners, "ensure_session", fake_ensure)
    monkeypatch.setattr(sessions.runners, "send", fake_send)
    await sessions.send({"id": 3}, 5, [{"type": "text", "text": "hi"}])
    assert calls == [("ensure", 5), ("send", 5, [{"type": "text", "text": "hi"}])]

    monkeypatch.setattr(sessions.runners, "ensure_session", no_runner)
    with pytest.raises(sessions.SessionUnavailable):  # callers only ever see SessionUnavailable
        await sessions.ensure_session({"id": 3}, 5)


async def test_stopping_stops_the_runner(monkeypatch):
    async def fake_runner_stop(project, conversation_id):
        return True

    async def no_open_build(conversation_id, description):
        pass

    monkeypatch.setattr(sessions.runners, "stop_session", fake_runner_stop)
    monkeypatch.setattr(sessions.signals, "end_open_build", no_open_build)
    assert await sessions.stop_session({"id": 3}, 5) == {"runner": True}
