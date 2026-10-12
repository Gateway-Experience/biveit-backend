# Projects API tests (controllers/projects). The repo is monkeypatched, so no db is involved -- what's
# under test is the controller: the owner always comes from the session, and the body is validated.

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.repositories import projects as projects_repo
from src.utils.auth import get_current_user

_CALLER = {"id": 7, "username": "alice", "user_type": "USER"}


def _project(**overrides):
    return {
        "id": 1,
        "user_id": _CALLER["id"],
        "name": "Portfolio",
        "description": None,
        "meta": {},
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-02T00:00:00Z",
        "deleted_at": None,
        **overrides,
    }


@pytest.fixture
def client() -> TestClient:
    # No `with` block, so the lifespan (db connect) never runs -- the repo is faked anyway.
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _CALLER
    return TestClient(app)


def test_list_returns_the_callers_projects(monkeypatch, client):
    calls = {}

    async def fake_find(user_id):
        calls["user_id"] = user_id
        return [_project(id=2), _project(id=1)]

    monkeypatch.setattr(projects_repo, "find", fake_find)

    response = client.get("/api/projects")
    assert response.status_code == 200
    assert [project["id"] for project in response.json()] == [2, 1]
    assert calls["user_id"] == _CALLER["id"]


def test_list_is_empty_when_the_caller_has_none(monkeypatch, client):
    async def fake_find(user_id):
        return []

    monkeypatch.setattr(projects_repo, "find", fake_find)
    assert client.get("/api/projects").json() == []


def test_create_owns_the_project_by_the_caller(monkeypatch, client):
    calls = {}

    async def fake_insert(data):
        calls["data"] = data
        return _project(**data)

    monkeypatch.setattr(projects_repo, "insert", fake_insert)

    response = client.post("/api/projects", json={"name": "Landing page", "description": "For the launch"})
    assert response.status_code == 201
    assert calls["data"] == {"name": "Landing page", "description": "For the launch", "user_id": _CALLER["id"]}


@pytest.mark.parametrize("body", [{"name": ""}, {"name": "x" * 201}, {}, {"name": "ok", "user_id": 99}])
def test_create_rejects_bad_bodies(body, client):
    # Includes trying to pick the owner via the body -- forbidden, not silently dropped.
    assert client.post("/api/projects", json=body).status_code == 422


def test_routes_need_a_logged_in_caller():
    client = TestClient(create_app())
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/projects", json={"name": "x"}).status_code == 401
