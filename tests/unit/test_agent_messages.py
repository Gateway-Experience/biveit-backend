# POST /agent/{port}/messages tests: the controller (admin-token gate, body validation, 404) and the
# conversation service behind it (port -> project, current conversation, how the row is filled in).
# Repos are faked -- no db.

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import settings
from src.constants import AiMessageRole
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.utils import redis

_ADMIN_TOKEN = "admin-token-of-a-realistic-length-000001"
_PROJECT = {"id": 3, "user_id": 7, "current_port": 8899}
_CONTENT = [{"type": "text", "text": "Here's your homepage!"}]


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "admin_token", _ADMIN_TOKEN)
    return TestClient(create_app())


def _admin():
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


@pytest.fixture
def db(monkeypatch):
    """Fakes the three repos over in-memory lists; returns them for assertions."""
    state = {"projects": [_PROJECT], "conversations": [], "messages": []}

    async def find_projects(criteria):
        return [project for project in state["projects"] if project["current_port"] == criteria["current_port"]]

    async def find_conversations(criteria):
        return [conv for conv in state["conversations"] if conv["project_id"] == criteria["project_id"]][::-1]

    async def insert_conversation(data):
        conversation = {"id": len(state["conversations"]) + 1, **data}
        state["conversations"] = [*state["conversations"], conversation]
        return conversation

    async def insert_message(data):
        # A full row, like RETURNING gives -- the response is validated against AiMessage.
        message = {
            "id": len(state["messages"]) + 1,
            "seq": len(state["messages"]) + 1,
            "model": None,
            "stop_reason": None,
            "usage": None,
            "meta": {},
            "created_at": "2026-10-12T00:00:00Z",
            **data,
        }
        state["messages"] = [*state["messages"], message]
        return message

    monkeypatch.setattr(projects_repo, "find", find_projects)
    monkeypatch.setattr(ai_conversations_repo, "find", find_conversations)
    monkeypatch.setattr(ai_conversations_repo, "insert", insert_conversation)

    @asynccontextmanager
    async def fake_lock(key, ttl_seconds, wait_seconds):
        yield

    monkeypatch.setattr(ai_messages_repo, "insert", insert_message)
    monkeypatch.setattr(redis, "lock", fake_lock)
    return state


# --- controller ---


def test_requires_the_admin_token(client, db):
    assert client.post("/api/agent/8899/messages", json={"content": _CONTENT}).status_code == 403
    bad = {"Authorization": "Bearer a-user-jwt-is-not-enough"}
    assert client.post("/api/agent/8899/messages", json={"content": _CONTENT}, headers=bad).status_code == 403


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
    assert client.post("/api/agent/8899/messages", json=body, headers=_admin()).status_code == 422


def test_unknown_port_is_404(client, db):
    response = client.post("/api/agent/1234/messages", json={"content": _CONTENT}, headers=_admin())
    assert response.status_code == 404
    assert response.json()["detail"] == "SESSION_NOT_FOUND"


def test_stores_the_message_and_returns_it(client, db):
    response = client.post("/api/agent/8899/messages", json={"content": _CONTENT}, headers=_admin())
    assert response.status_code == 201
    assert response.json()["content"] == _CONTENT
    assert db["messages"][0]["content"] == _CONTENT


def test_keeps_unknown_block_fields_verbatim(client, db):
    # Blocks are stored as-is: a field we've never heard of (or a new block type) must survive.
    blocks = [{"type": "thinking", "thinking": "", "signature": "abc=="}, {"type": "brand_new", "x": {"y": 1}}]
    client.post("/api/agent/8899/messages", json={"content": blocks}, headers=_admin())
    assert db["messages"][0]["content"] == blocks


# --- service ---


async def test_first_message_starts_a_conversation_started_by_the_owner(db):
    message = await agent_service.receive_session_message(8899, _CONTENT)

    assert db["conversations"] == [{"id": 1, "project_id": 3, "user_id": 7}]
    assert message["conversation_id"] == 1
    assert message["role"] == AiMessageRole.ASSISTANT
    assert message["meta"] == {"session_port": 8899}


async def test_later_messages_go_to_the_newest_conversation(db):
    db["conversations"] = [{"id": 1, "project_id": 3}, {"id": 2, "project_id": 3}]

    message = await agent_service.receive_session_message(8899, _CONTENT)
    assert message["conversation_id"] == 2
    assert len(db["conversations"]) == 2  # none created


async def test_a_port_claimed_by_two_projects_is_refused(db):
    db["projects"] = [_PROJECT, {**_PROJECT, "id": 4}]
    with pytest.raises(agent_service.SessionNotFound):
        await agent_service.receive_session_message(8899, _CONTENT)
