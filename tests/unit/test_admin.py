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


@pytest.fixture
def deploying(services, monkeypatch):
    """The deployments module and the owner's conversation faked; records the domains set and what's deployed."""
    calls = {"domains": [], "deployed": []}

    async def find_projects(criteria):
        return [{**_PROJECT, "user_id": 2}] if criteria == {"id": 16} else []

    async def get_current_conversation(project_id, user_id):
        assert user_id == 2  # as its owner
        return {"id": 13}

    async def set_domains(project, domains):
        calls["domains"] = [*calls["domains"], domains]
        return domains

    async def deploy(project, conversation_id, enforce_cooldown=True):
        assert not enforce_cooldown  # an admin's deploy skips the user cooldown
        calls["deployed"] = [*calls["deployed"], (project["id"], conversation_id)]
        return {"run_id": "r1", "commit": "abc"}

    monkeypatch.setattr(admin.projects_repo, "find", find_projects)
    monkeypatch.setattr(admin.agent_service, "get_current_conversation", get_current_conversation)
    monkeypatch.setattr(admin.deployments, "set_domains", set_domains)
    monkeypatch.setattr(admin.deployments, "deploy", deploy)
    monkeypatch.setattr(admin.deployments, "get_default_domain", lambda project: "p16hash.prod.biveitapp.com")
    return calls


def test_deploying_without_a_domain_uses_the_default_one(deploying):
    client = _client_as(_ADMIN)
    response = client.post("/api/admin/projects/16/deployments", json={"domains": []})
    assert response.status_code == 202 and response.json() == {"run_id": "r1", "commit": "abc"}
    response = client.post("/api/admin/projects/16/deployments", json={"domains": ["shop.example.com"]})
    assert response.status_code == 202
    assert deploying["domains"] == [["p16hash.prod.biveitapp.com"], ["shop.example.com"]]
    assert deploying["deployed"] == [(16, 13), (16, 13)]
    assert _client_as(_USER).post("/api/admin/projects/16/deployments", json={"domains": []}).status_code == 403


def test_deploying_without_any_domain_to_use_is_refused(deploying, monkeypatch):
    monkeypatch.setattr(admin.deployments, "get_default_domain", lambda project: None)
    response = _client_as(_ADMIN).post("/api/admin/projects/16/deployments", json={"domains": []})
    assert response.status_code == 400 and response.json()["detail"] == "NO_DOMAIN"
    assert deploying["deployed"] == []


def test_a_domain_another_project_has_is_refused(deploying, monkeypatch):
    async def set_domains(project, domains):
        raise admin.deployments.DomainTaken(domains)

    monkeypatch.setattr(admin.deployments, "set_domains", set_domains)
    response = _client_as(_ADMIN).post("/api/admin/projects/16/deployments", json={"domains": ["shop.example.com"]})
    assert response.status_code == 409 and response.json()["detail"] == "DOMAIN_TAKEN"
    assert deploying["deployed"] == []


def test_setting_a_plan_says_why_it_was_refused_and_fulfils_the_project(services, monkeypatch):
    fulfilled = []

    async def create_admin_plan(project, plan, starts_at, ends_at, admin_id):
        if ends_at <= starts_at:
            raise admin.payments.InvalidPlanPeriod()
        return {"id": 5, "provider": "admin", "plan": plan, "created_at": starts_at, "ends_at": ends_at}

    async def update_admin_plan(project_id, plan, starts_at, ends_at):
        raise admin.payments.PlanNotEditable(project_id)

    async def fulfil(project_id):
        fulfilled.append(project_id)

    monkeypatch.setattr(admin.payments, "create_admin_plan", create_admin_plan)
    monkeypatch.setattr(admin.payments, "update_admin_plan", update_admin_plan)
    monkeypatch.setattr(admin.fulfilment, "fulfil", fulfil)
    client = _client_as(_ADMIN)
    body = {"plan": "yearly", "starts_at": "2026-11-04T00:00:00Z", "ends_at": "2027-11-04T00:00:00Z"}

    created = client.post("/api/admin/projects/16/plan", json=body)
    assert created.status_code == 201 and created.json()["plan"]["provider"] == "admin"
    assert fulfilled == [16]
    backwards = client.post("/api/admin/projects/16/plan", json={**body, "ends_at": "2026-11-01T00:00:00Z"})
    assert backwards.status_code == 400 and backwards.json()["detail"] == "INVALID_PERIOD"
    assert client.put("/api/admin/projects/16/plan", json=body).json()["detail"] == "PLAN_NOT_EDITABLE"
    # No timezone: refused, rather than guessed.
    assert (
        client.post("/api/admin/projects/16/plan", json={**body, "starts_at": "2026-11-04T00:00:00"}).status_code == 422
    )
