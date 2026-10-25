# The session's endpoints (controllers/agent): the session-token gate, then each endpoint -- messages
# stored for the token's conversation (and refused when the agent may not send them), signals recorded,
# dev servers controlled. Plus the implementing milestones the user's approvals set off
# (services/agent/implementation) and the session stop ending a build. Repos are faked -- no db.

import pytest
from fastapi.testclient import TestClient

from src.app import create_listener_app
from src.config import settings
from src.constants import AiMessageRole, DevServerAction
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services import devenv
from src.services.agent import implementation, sessions, tokens

_PROJECT = {
    "id": 3,
    "hash": "p3hash",
    "user_id": 7,
    "user_hash": "u7hash",
    "stage": "specing",
    "implementation_done_at": None,
}
_CONVERSATION = {"id": 5, "project_id": 3, "user_id": 7}
_CONTENT = [{"type": "text", "text": "Here's your homepage!"}]


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "session_token_secret", "session-token-secret-for-tests")


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_listener_app())


def _session(conversation_id=5):
    return {"Authorization": f"Bearer {tokens.to_session_token(conversation_id)}"}


@pytest.fixture
def db(monkeypatch):
    """Fakes the repos over in-memory lists; returns them for assertions."""
    state = {"messages": [], "project_updates": []}

    async def get_conversation(conversation_id):
        return _CONVERSATION if conversation_id == _CONVERSATION["id"] else None

    async def find_projects(criteria):
        return [_PROJECT] if criteria == {"id": _PROJECT["id"]} else []

    async def insert_message(data):
        # A full row, like RETURNING gives -- the response is validated against AiMessage.
        message = {
            "id": len(state["messages"]) + 1,
            "seq": len(state["messages"]) + 1,
            "model": None,
            "stop_reason": None,
            "usage": None,
            "created_at": "2026-10-12T00:00:00Z",
            **data,
        }
        state["messages"] = [*state["messages"], message]
        return message

    async def find_messages_before(conversation_id, before_seq, limit):
        return state["messages"][-limit:]

    async def publish(channel, payload=""):
        pass

    async def update_project(project_id, updates, expected=None):
        state["project_updates"] = [*state["project_updates"], (project_id, updates, expected)]
        return {**_PROJECT, **updates}

    monkeypatch.setattr(projects_repo, "update", update_project)
    monkeypatch.setattr(ai_conversations_repo, "get", get_conversation)
    monkeypatch.setattr(projects_repo, "find", find_projects)
    monkeypatch.setattr(ai_messages_repo, "insert", insert_message)
    monkeypatch.setattr(ai_messages_repo, "find_before", find_messages_before)
    monkeypatch.setattr(agent_service.conversations.redis, "publish", publish)
    return state


# --- the token ---


def test_tokens_speak_for_their_own_conversation_only():
    token = tokens.to_session_token(5)
    assert tokens.get_token_conversation_id(token) == 5
    forged = "6." + token.partition(".")[2]  # someone else's id, this conversation's signature
    assert tokens.get_token_conversation_id(forged) is None
    assert tokens.get_token_conversation_id("garbage") is None


def test_no_secret_means_no_tokens(monkeypatch):
    token = tokens.to_session_token(5)
    monkeypatch.setattr(settings, "session_token_secret", "")
    assert tokens.get_token_conversation_id(token) is None
    with pytest.raises(tokens.SessionTokenSecretMissing):
        tokens.to_session_token(5)


@pytest.mark.parametrize("path", ["/api/agent/messages", "/api/agent/signals", "/api/agent/dev-servers"])
def test_every_endpoint_needs_a_session_token(path, client, db, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-token-of-a-realistic-length-000001")
    assert client.post(path, json={}).status_code == 403
    admin = {"Authorization": "Bearer admin-token-of-a-realistic-length-000001"}
    assert client.post(path, json={}, headers=admin).status_code == 403  # the admin token isn't one


# --- messages ---


@pytest.mark.parametrize(
    "body",
    [
        {"content": []},
        {"content": [{"text": "no type"}]},
        {"content": [{"type": ""}]},
        {},
        {"content": _CONTENT, "role": "user"},
    ],
)
def test_rejects_bad_bodies(body, client, db):
    assert client.post("/api/agent/messages", json=body, headers=_session()).status_code == 422


def test_a_gone_conversation_is_404(client, db):
    response = client.post("/api/agent/messages", json={"content": _CONTENT}, headers=_session(99))
    assert response.status_code == 404
    assert response.json()["detail"] == "CONVERSATION_NOT_FOUND"


def test_stores_the_message_in_the_tokens_conversation(client, db):
    response = client.post("/api/agent/messages", json={"content": _CONTENT}, headers=_session())
    assert response.status_code == 201
    stored = db["messages"][0]
    assert (stored["conversation_id"], stored["role"], stored["content"]) == (5, AiMessageRole.ASSISTANT, _CONTENT)


def test_keeps_unknown_block_fields_verbatim(client, db):
    # Blocks are stored as-is: a field we've never heard of (or a new block type) must survive.
    blocks = [{"type": "thinking", "thinking": "", "signature": "abc=="}, {"type": "brand_new", "x": {"y": 1}}]
    client.post("/api/agent/messages", json={"content": blocks}, headers=_session())
    assert db["messages"][0]["content"] == blocks


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ([{"type": "signal", "slug": "IMPLEMENTATION_DONE"}], "send_signal"),
        ([{"type": "tool_use", "input": {"actions": [{"name": "DEPLOY_NOW", "params": {}}]}}], "unknown actions"),
        # _PROJECT is still spec-ing: there's no implementation to approve yet.
        ([{"type": "tool_use", "input": {"actions": [{"name": "APPROVE_IMPLEMENTATION", "params": {}}]}}], "spec"),
    ],
)
def test_refuses_what_the_agent_may_not_send(content, reason, client, db):
    response = client.post("/api/agent/messages", json={"content": content}, headers=_session())
    assert response.status_code == 422 and response.json()["detail"]["code"] == "INVALID_MESSAGE"
    assert reason in response.json()["detail"]["reason"]
    assert db["messages"] == []


def test_an_approval_request_is_stored_as_request_actions(client, db):
    content = [{"type": "tool_use", "input": {"actions": [{"name": "APPROVE_SPEC", "params": {}}]}}]
    assert client.post("/api/agent/messages", json={"content": content}, headers=_session()).status_code == 201
    stored = db["messages"][0]["content"][0]
    assert stored["name"] == "request_actions" and stored["id"].startswith("toolu_")


# --- dev servers ---


def test_dev_servers_need_implementing_to_have_started(client, db, monkeypatch):
    monkeypatch.setattr(devenv, "has_app", lambda project, conversation_id: False)
    response = client.post("/api/agent/dev-servers", json={"action": "status"}, headers=_session())
    assert response.status_code == 409 and response.json()["detail"] == "IMPLEMENTATION_NOT_STARTED"


def test_dev_servers_are_controlled_for_the_tokens_project(client, db, monkeypatch):
    calls = []

    async def fake_control(project, conversation_id, action):
        calls.append((project["id"], conversation_id, action))
        return [{"app": "backend", "name": "project_3_be", "status": "online", "port": 8003}]

    monkeypatch.setattr(devenv, "has_app", lambda project, conversation_id: True)
    monkeypatch.setattr(devenv, "control_dev_servers", fake_control)
    response = client.post("/api/agent/dev-servers", json={"action": "restart"}, headers=_session())
    assert response.status_code == 200
    assert response.json()["servers"][0]["status"] == "online"
    assert calls == [(3, 5, DevServerAction.RESTART)]


def test_unknown_dev_server_actions_are_422(client, db):
    response = client.post("/api/agent/dev-servers", json={"action": "delete-all"}, headers=_session())
    assert response.status_code == 422


# --- signals ---


def test_the_agent_sends_building_signals(client, db):
    response = client.post(
        "/api/agent/signals", json={"slug": "BUILDING", "description": "Adding a booking flow"}, headers=_session()
    )
    assert response.status_code == 201
    stored = db["messages"][0]
    assert stored["role"] == AiMessageRole.ASSISTANT
    assert stored["content"] == [{"type": "signal", "slug": "BUILDING", "description": "Adding a booking flow"}]
    client.post("/api/agent/signals", json={"slug": "BUILDING_DONE"}, headers=_session())
    assert db["messages"][1]["content"] == [{"type": "signal", "slug": "BUILDING_DONE"}]


def test_the_runner_says_when_the_agent_goes_idle(client, db):
    response = client.post("/api/agent/signals", json={"slug": "AGENT_IDLE"}, headers=_session())
    assert response.status_code == 201
    assert db["messages"][0]["content"] == [{"type": "signal", "slug": "AGENT_IDLE"}]


def test_the_agent_cant_send_our_signals(client, db):
    response = client.post("/api/agent/signals", json={"slug": "IMPLEMENTATION_DONE"}, headers=_session())
    assert response.status_code == 403 and response.json()["detail"] == "SIGNAL_NOT_ALLOWED"
    assert db["messages"] == []


@pytest.mark.parametrize("body", [{}, {"slug": "MADE_UP"}, {"slug": "BUILDING", "description": "x" * 201}])
def test_bad_signals_are_422(body, client, db):
    assert client.post("/api/agent/signals", json=body, headers=_session()).status_code == 422


async def test_stopping_the_session_ends_an_open_build(db, monkeypatch):
    async def fake_runner_stop(project, conversation_id):
        return True

    monkeypatch.setattr(sessions.runners, "stop_session", fake_runner_stop)
    await sessions.signals.append_signal(5, "BUILDING", "Big change")
    await sessions.stop_session({"id": 3}, 5)
    assert db["messages"][-1]["content"][0]["slug"] == "BUILDING_DONE"
    await sessions.stop_session({"id": 3}, 5)
    assert len(db["messages"]) == 2  # nothing open the second time, so nothing more written


async def test_stopping_or_finding_the_session_dead_ends_an_open_turn(db, monkeypatch):
    async def fake_runner_stop(project, conversation_id):
        return True

    async def fake_runner_ensure(project, conversation_id):
        pass

    runner_state = {"up": False}

    async def fake_is_up(project, conversation_id):
        return runner_state["up"]

    monkeypatch.setattr(sessions.runners, "stop_session", fake_runner_stop)
    monkeypatch.setattr(sessions.runners, "ensure_session", fake_runner_ensure)
    monkeypatch.setattr(sessions.runners, "is_session_up", fake_is_up)
    user_message = {"conversation_id": 5, "role": "user", "content": [{"type": "text", "text": "hi"}], "meta": {}}

    await sessions.ai_messages_repo.insert(user_message)
    await sessions.stop_session({"id": 3}, 5)
    assert _slugs(db) == [None, "AGENT_IDLE"]
    await sessions.stop_session({"id": 3}, 5)
    assert _slugs(db) == [None, "AGENT_IDLE"]  # closed already

    # Found dead on an open: the turn it was on is over.
    await sessions.ai_messages_repo.insert(user_message)
    await sessions.ensure_session({"id": 3}, 5)
    assert _slugs(db)[-1] == "AGENT_IDLE"

    # Found up: whatever it's on is still going.
    runner_state["up"] = True
    await sessions.ai_messages_repo.insert(user_message)
    await sessions.ensure_session({"id": 3}, 5)
    assert _slugs(db)[-1] is None


# --- the implementing milestones (the user's approvals) ---


_READY = {
    "url": "https://project-3.dev.example.com",
    "backend": {"port": 8003, "process": "project_3_be", "log": "logs/be.log"},
    "frontend": {"port": 16003, "process": "project_3_fe", "log": "logs/fe.log"},
}


def _slugs(db):
    return [message["content"][0].get("slug") for message in db["messages"]]


@pytest.fixture
def pushed(monkeypatch):
    """Runs start_implementation's background job inline and records what it pushes into the session."""
    state = {"jobs": [], "pushed": []}

    async def fake_ensure_session(project, conversation_id):
        return 47000

    async def fake_send(project, conversation_id, content):
        state["pushed"] = [*state["pushed"], content]

    monkeypatch.setattr(implementation, "spawn", lambda job: state["jobs"].append(job))
    monkeypatch.setattr(sessions, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(sessions, "send", fake_send)
    return state


async def test_approving_the_spec_starts_implementing_and_reports_ready(db, pushed, monkeypatch):
    async def fake_ensure_devenv(project, conversation_id):
        assert (project["id"], conversation_id) == (3, 5)
        return _READY

    monkeypatch.setattr(implementation.devenv, "ensure_devenv", fake_ensure_devenv)
    await implementation.apply_approvals(_PROJECT, 5, ["APPROVE_SPEC"])
    # The stage moves first, compare-and-set from specing -- before anything is set up.
    assert db["project_updates"] == [(3, {"stage": "implementing"}, {"stage": "specing"})]
    assert _slugs(db) == ["IMPLEMENTATION_STARTED"] and pushed["pushed"] == []  # setup's in the background

    await pushed["jobs"][0]
    assert _slugs(db) == ["IMPLEMENTATION_STARTED", "BUILDING"]  # the first build starts right away
    report = pushed["pushed"][0][0]["text"]
    assert report.startswith("[Dev environment ready]")
    assert "https://project-3.dev.example.com" in report and "BUILDING_DONE" in report


async def test_a_failed_devenv_is_recorded_and_reported_to_the_agent(db, pushed, monkeypatch):
    async def failing_ensure_devenv(project, conversation_id):
        raise RuntimeError("`npm install` exited 1: ERESOLVE")

    monkeypatch.setattr(implementation.devenv, "ensure_devenv", failing_ensure_devenv)
    await implementation.apply_approvals(_PROJECT, 5, ["APPROVE_SPEC"])
    await pushed["jobs"][0]
    assert _slugs(db) == ["IMPLEMENTATION_STARTED", "DEVENV_FAILED"]
    assert "ERESOLVE" in db["messages"][-1]["content"][0]["description"]
    report = pushed["pushed"][0][0]["text"]
    assert report.startswith("[Dev environment failed]") and "ERESOLVE" in report


async def test_reapproving_the_spec_keeps_the_stage_and_just_reruns_the_setup(db, pushed, monkeypatch):
    async def fake_ensure_devenv(project, conversation_id):
        return _READY

    monkeypatch.setattr(implementation.devenv, "ensure_devenv", fake_ensure_devenv)
    await implementation.apply_approvals({**_PROJECT, "stage": "implementing"}, 5, ["APPROVE_SPEC"])
    assert db["project_updates"] == [] and _slugs(db) == []  # already there: no new milestone
    await pushed["jobs"][0]
    assert _slugs(db) == ["BUILDING"]


async def test_signing_off_is_recorded_once_and_ends_the_build(db, monkeypatch):
    project = {**_PROJECT, "stage": "implementing", "implementation_done_at": None}

    async def update_project(project_id, updates, expected=None):
        project.update(updates)  # the fake db row
        return project

    monkeypatch.setattr(projects_repo, "update", update_project)
    await sessions.signals.append_signal(5, "BUILDING", "Last tweaks")
    await implementation.apply_approvals(project, 5, ["APPROVE_IMPLEMENTATION"])
    first_done_at = project["implementation_done_at"]
    assert first_done_at and _slugs(db) == ["BUILDING", "IMPLEMENTATION_DONE", "BUILDING_DONE"]

    await implementation.apply_approvals(project, 5, ["APPROVE_IMPLEMENTATION"])
    assert project["implementation_done_at"] == first_done_at  # the first sign-off is kept
    assert _slugs(db)[-1] == "IMPLEMENTATION_DONE"  # every sign-off is in the log; no build left to end


async def test_signing_off_while_specing_does_nothing(db):
    await implementation.apply_approvals(_PROJECT, 5, ["APPROVE_IMPLEMENTATION"])  # _PROJECT is specing
    assert db["messages"] == [] and db["project_updates"] == []
