# The session's endpoints (controllers/agent): the session-token gate, then each endpoint -- messages
# stored for the token's conversation, devenv kicked off in the background, dev servers controlled.
# Repos are faked -- no db.

import json

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import settings
from src.constants import AiMessageRole, DevServerAction
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services import devenv
from src.services.agent import building, devenvs, sessions, tokens

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
    return TestClient(create_app())


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

    async def publish(channel, payload=""):
        pass

    async def update_project(project_id, updates, expected=None):
        state["project_updates"] = [*state["project_updates"], (project_id, updates, expected)]
        return {**_PROJECT, **updates}

    monkeypatch.setattr(projects_repo, "update", update_project)
    monkeypatch.setattr(ai_conversations_repo, "get", get_conversation)
    monkeypatch.setattr(projects_repo, "find", find_projects)
    monkeypatch.setattr(ai_messages_repo, "insert", insert_message)
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


@pytest.mark.parametrize("path", ["/api/agent/messages", "/api/agent/devenv", "/api/agent/dev-servers"])
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


# --- devenv ---


@pytest.fixture
def pushed(monkeypatch):
    """Runs start_implementation's background job inline and records what it pushes into the session."""
    state = {"jobs": [], "pushed": []}

    async def fake_ensure_session(project, conversation_id):
        return 47000

    async def fake_send(port, content):
        state["pushed"] = [*state["pushed"], content]

    async def fake_set_building(conversation_id, summary=None):
        state["building"] = summary

    monkeypatch.setattr(devenvs, "spawn", lambda job: state["jobs"].append(job))
    monkeypatch.setattr(building, "set_building", fake_set_building)
    monkeypatch.setattr(sessions, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(sessions, "send", fake_send)
    return state


async def test_devenv_starts_in_the_background_and_reports_ready(client, db, pushed, monkeypatch):
    async def fake_ensure_devenv(project, conversation_id):
        assert (project["id"], conversation_id) == (3, 5)
        return {
            "url": "https://project-3.dev.example.com",
            "backend": {"port": 8003, "process": "project_3_be", "log": "logs/be.log"},
            "frontend": {"port": 16003, "process": "project_3_fe", "log": "logs/fe.log"},
        }

    monkeypatch.setattr(devenv, "ensure_devenv", fake_ensure_devenv)
    response = client.post("/api/agent/devenv", headers=_session())
    assert response.status_code == 202 and pushed["pushed"] == []  # nothing yet: it's in the background
    # The stage moves first, compare-and-set from specing -- before anything is set up.
    assert db["project_updates"] == [(3, {"stage": "implementing"}, {"stage": "specing"})]

    await pushed["jobs"][0]
    report = pushed["pushed"][0][0]["text"]
    assert report.startswith("[Dev environment ready]")
    assert "https://project-3.dev.example.com" in report and "project_3_be" in report
    assert pushed["building"] == "Building the first version" and "building tool" in report


async def test_a_failed_devenv_is_reported_to_the_agent(client, db, pushed, monkeypatch):
    async def failing_ensure_devenv(project, conversation_id):
        raise RuntimeError("`npm install` exited 1: ERESOLVE")

    monkeypatch.setattr(devenv, "ensure_devenv", failing_ensure_devenv)
    client.post("/api/agent/devenv", headers=_session())
    await pushed["jobs"][0]
    report = pushed["pushed"][0][0]["text"]
    assert report.startswith("[Dev environment failed]") and "ERESOLVE" in report


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


async def test_devenv_on_an_implementing_project_keeps_the_stage(client, db, pushed, monkeypatch):
    async def fake_ensure_devenv(project, conversation_id):
        return {
            "url": "http://localhost:10102",
            "backend": {"port": 10101, "process": "project_3_be", "log": "logs/be.log"},
            "frontend": {"port": 10102, "process": "project_3_fe", "log": "logs/fe.log"},
        }

    monkeypatch.setattr(devenv, "ensure_devenv", fake_ensure_devenv)
    implementing = {**_PROJECT, "stage": "implementing"}

    async def find_projects(criteria):
        return [implementing]

    monkeypatch.setattr(projects_repo, "find", find_projects)
    assert client.post("/api/agent/devenv", headers=_session()).status_code == 202
    assert db["project_updates"] == []  # already there: a retry just re-runs the setup
    await pushed["jobs"][0]


# --- building ---


@pytest.fixture
def fake_building_redis(monkeypatch):
    """building.py's redis calls over a dict (TTLs recorded, not enforced), and its wake-ups recorded."""
    state = {"values": {}, "ttls": {}, "wakeups": []}

    async def set_value(key, value, ttl_seconds):
        state["values"][key] = value
        state["ttls"][key] = ttl_seconds

    async def get_value(key):
        return state["values"].get(key)

    async def delete(key):
        state["values"].pop(key, None)

    async def publish_devenv_changed(conversation_id):
        state["wakeups"] = [*state["wakeups"], conversation_id]

    monkeypatch.setattr(building.redis, "set_value", set_value)
    monkeypatch.setattr(building.redis, "get_value", get_value)
    monkeypatch.setattr(building.redis, "delete", delete)
    monkeypatch.setattr(building, "publish_devenv_changed", publish_devenv_changed)
    return state


def test_the_agent_turns_building_on_and_off(client, db, fake_building_redis):
    on = client.post(
        "/api/agent/building", json={"active": True, "summary": "Adding a booking flow"}, headers=_session()
    )
    assert on.status_code == 200
    assert fake_building_redis["ttls"]["building:5"] == 3600
    started = json.loads(fake_building_redis["values"]["building:5"])
    assert started["summary"] == "Adding a booking flow"

    # A refresh (say, an hour-long build) keeps when it started, and the summary if none is given, and
    # pushes the expiry out.
    client.post("/api/agent/building", json={"active": True}, headers=_session())
    refreshed = json.loads(fake_building_redis["values"]["building:5"])
    assert (refreshed["since"], refreshed["summary"]) == (started["since"], started["summary"])
    assert refreshed["until"] >= started["until"]

    client.post("/api/agent/building", json={"active": False}, headers=_session())
    assert "building:5" not in fake_building_redis["values"]
    assert fake_building_redis["wakeups"] == [5, 5, 5]  # every change wakes the tabs


def test_building_needs_a_session_token_and_a_valid_body(client, db, fake_building_redis):
    assert client.post("/api/agent/building", json={"active": True}).status_code == 403
    assert client.post("/api/agent/building", json={"summary": "x"}, headers=_session()).status_code == 422


async def test_stopping_the_session_clears_building(db, fake_building_redis, monkeypatch):
    async def fake_adapter_stop(project, conversation_id):
        return {"tmux_session": True, "pids": []}

    monkeypatch.setattr(sessions.adapter, "stop_session", fake_adapter_stop)
    await building.set_building(5, "Big change")
    await sessions.stop_session({"id": 3, "current_port": None}, 5)
    assert await building.get_building(5) is None


# --- implementing done ---


def test_implementing_done_is_recorded_once_and_ends_building(client, db, monkeypatch):
    project = {**_PROJECT, "stage": "implementing", "implementation_done_at": None}
    cleared = []

    async def find_projects(criteria):
        return [project]

    async def update_project(project_id, updates, expected=None):
        project.update(updates)  # the fake db row
        return project

    async def fake_clear_building(conversation_id):
        cleared.append(conversation_id)

    monkeypatch.setattr(projects_repo, "find", find_projects)
    monkeypatch.setattr(projects_repo, "update", update_project)
    monkeypatch.setattr(building, "clear_building", fake_clear_building)

    first = client.post("/api/agent/implementing-done", headers=_session())
    assert first.status_code == 200 and first.json()["implementation_done_at"]
    second = client.post("/api/agent/implementing-done", headers=_session())
    assert second.json() == first.json()  # the first sign-off is kept
    assert cleared == [5, 5]


def test_implementing_done_needs_implementing_to_have_started(client, db):
    response = client.post("/api/agent/implementing-done", headers=_session())  # _PROJECT is specing
    assert response.status_code == 409 and response.json()["detail"] == "IMPLEMENTATION_NOT_STARTED"
