# The admin dashboard's endpoints (controllers/admin): admins only -- 401 with nobody logged in, 403 for a
# normal user -- and what they hand back. platform_status, the repo and close_project are faked: what's under
# test is the gate and the wiring.

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.controllers import admin
from src.utils.auth import get_current_user

_ADMIN = {"id": 1, "username": "admin@biveit.com", "user_type": "USER", "role": "ADMIN"}
_USER = {"id": 2, "username": "someone@x.com", "user_type": "USER", "role": None}
_PROJECT = {"id": 16, "name": "Portfolio", "stage": "building"}
_SECTION = {"title": "Platform", "rows": [{"label": "env", "value": "local"}]}


@pytest.fixture
def services(monkeypatch):
    calls = {"stopped": []}

    async def find_projects(criteria):
        return [_PROJECT] if criteria == {"id": 16} else []

    async def get_platform_sections():
        return [_SECTION]

    async def list_projects():
        return [{"id": 16, "name": "Portfolio", "is_running": True}]

    async def get_project_sections(project, conversation_id):
        return [{"title": "Project", "rows": [{"label": "conversation", "value": str(conversation_id)}]}]

    async def close_project(project):
        calls["stopped"] = [*calls["stopped"], project["id"]]
        return {"sessions": {}, "dev_servers": [], "container": True}

    monkeypatch.setattr(admin.projects_repo, "find", find_projects)
    monkeypatch.setattr(admin.platform_status, "get_platform_sections", get_platform_sections)
    monkeypatch.setattr(admin.platform_status, "list_projects", list_projects)
    monkeypatch.setattr(admin.platform_status, "get_project_sections", get_project_sections)
    monkeypatch.setattr(admin.platform_status, "get_server_usage", lambda: {"cpu": {"percent": 12.5}})
    monkeypatch.setattr(admin.projects_service, "close_project", close_project)
    return calls


def _client_as(user):
    app = create_app()
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/admin/server-usage"),
        ("GET", "/api/admin/platform"),
        ("GET", "/api/admin/projects/16"),
        ("POST", "/api/admin/projects/16/stop"),
    ],
)
def test_only_admins_get_in(method, path, services):
    assert _client_as(None).request(method, path).status_code == 401
    assert _client_as(_USER).request(method, path).status_code == 403
    assert _client_as(_ADMIN).request(method, path).status_code == 200
    assert services["stopped"] == ([16] if path.endswith("/stop") else [])  # only the admin's call got through


def test_the_dashboard_reads(services):
    client = _client_as(_ADMIN)
    assert client.get("/api/admin/server-usage").json() == {"cpu": {"percent": 12.5}}
    assert client.get("/api/admin/platform").json() == {
        "sections": [_SECTION],
        "projects": [{"id": 16, "name": "Portfolio", "is_running": True}],
    }
    detail = client.get("/api/admin/projects/16", params={"conversation": 13}).json()
    assert detail["project"] == _PROJECT and detail["sections"][0]["rows"][0]["value"] == "13"


def test_an_unknown_project_is_404(services):
    client = _client_as(_ADMIN)
    assert client.get("/api/admin/projects/99").status_code == 404
    assert client.post("/api/admin/projects/99/stop").status_code == 404
    assert services["stopped"] == []
