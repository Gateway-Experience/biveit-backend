# Projects API tests (controllers/projects). The repo is monkeypatched, so no db is involved -- what's
# under test is the controller: the owner always comes from the session, the body is validated, and
# opening a project prepares its workspace (seeded on disk for real, in a temp dir).

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import settings
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services import projects as project_service
from src.services.agent import adapter as agent_adapter
from src.utils.auth import get_current_user

_CALLER = {"id": 7, "username": "alice", "user_type": "USER"}


def _project(**overrides):
    return {
        "id": 1,
        "user_id": _CALLER["id"],
        "name": "Portfolio",
        "description": None,
        "current_port": None,
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

    async def fake_find(criteria):
        calls["criteria"] = criteria
        return [_project(id=2), _project(id=1)]

    monkeypatch.setattr(projects_repo, "find", fake_find)

    response = client.get("/api/projects")
    assert response.status_code == 200
    assert [project["id"] for project in response.json()] == [2, 1]
    assert calls["criteria"] == {"user_id": _CALLER["id"]}


def test_list_is_empty_when_the_caller_has_none(monkeypatch, client):
    async def fake_find(criteria):
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


# --- POST /projects/{id}/open ---


@pytest.fixture
def owned_project(monkeypatch):
    """projects_repo.find answers like the db would for the caller: their project 1, nobody else's. Its
    current conversation is 5."""

    async def fake_find(criteria):
        return [_project()] if criteria == {"id": 1, "user_id": _CALLER["id"]} else []

    async def fake_current_conversation(project_id, user_id):
        return {"id": 5, "project_id": project_id}

    monkeypatch.setattr(projects_repo, "find", fake_find)
    monkeypatch.setattr(agent_service, "get_current_conversation", fake_current_conversation)


def test_open_while_mocking_seeds_the_workspace_without_starting_a_session(
    client, owned_project, monkeypatch, tmp_path
):
    (tmp_path / "template").mkdir()
    (tmp_path / "template" / "index.html").write_text("<h1>starter</h1>")
    monkeypatch.setattr(settings, "template_dir", str(tmp_path / "template"))
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))

    async def no_session(project, conversation_id):
        raise AssertionError("no session while mocking")

    monkeypatch.setattr(agent_adapter, "ensure_session", no_session)
    monkeypatch.setattr(project_service, "AGENT_MOCK_REPLIES", True)

    response = client.post("/api/projects/1/open")
    assert response.status_code == 200 and response.json() == {"status": "ready"}
    # Seeded into the CONVERSATION's folder (conversation 5 of project 1), not the project's.
    assert (tmp_path / "storage" / str(_CALLER["id"]) / "1" / "5" / "index.html").read_text() == "<h1>starter</h1>"
    assert not (tmp_path / "storage" / str(_CALLER["id"]) / "1" / "index.html").exists()
    assert client.post("/api/projects/1/open").status_code == 200  # again: idempotent


def test_open_with_the_real_agent_ensures_the_session(client, owned_project, monkeypatch):
    opened = []

    async def fake_ensure_session(project, conversation_id):
        opened.append((project["id"], conversation_id))
        return 8899

    monkeypatch.setattr(project_service, "AGENT_MOCK_REPLIES", False)
    monkeypatch.setattr(agent_service, "ensure_session", fake_ensure_session)

    response = client.post("/api/projects/1/open")
    assert response.json() == {"status": "ready"}  # the port stays internal
    assert opened == [(1, 5)]


def test_open_someone_elses_project_is_404(client, owned_project):
    response = client.post("/api/projects/2/open")
    assert response.status_code == 404 and response.json()["detail"] == "PROJECT_NOT_FOUND"


def test_a_workspace_that_fails_to_start_is_503(client, owned_project, monkeypatch):
    async def broken(project, conversation_id):
        raise FileNotFoundError("doit")

    monkeypatch.setattr(project_service, "AGENT_MOCK_REPLIES", False)
    monkeypatch.setattr(agent_service, "ensure_session", broken)
    assert client.post("/api/projects/1/open").status_code == 503
