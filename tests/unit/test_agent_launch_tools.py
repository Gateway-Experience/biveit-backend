# The agent's launch endpoints (controllers/agent, /agent/launch/...): the plans and what the project has bought,
# domains to buy with our price, and the domain discount -- all for the session token's own project, read-only.
# Repos, the registrar and payments' access are faked.

import pytest
from fastapi.testclient import TestClient

from src.app import create_listener_app
from src.config import settings
from src.modules import domains
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import projects as projects_repo
from src.repositories import users as users_repo
from src.services.agent import tokens
from src.services.launch import access as launch_access
from src.services.launch import checkouts

_PROJECT = {"id": 3, "user_id": 7, "name": "Hanoi Bike Rental"}
_CONVERSATION = {"id": 5, "project_id": 3, "user_id": 7}
_ACCESS = {
    "can_download": True, "can_edit": True, "can_host": True, "has_had_plan": True, "edit_until": None,
    "plan_subscription": {"plan": "yearly", "status": "active", "renews_at": "2027-11-03T00:00:00Z", "ends_at": None},
    "one_time_subscription": None, "domain_subscription": None, "domain_failures": [],
}  # fmt: skip


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "session_token_secret", "session-token-secret-for-tests")


@pytest.fixture
def client(monkeypatch) -> TestClient:
    async def get_conversation(conversation_id):
        return _CONVERSATION if conversation_id == _CONVERSATION["id"] else None

    async def find_projects(criteria):
        return [_PROJECT] if criteria == {"id": _PROJECT["id"]} else []

    async def get_access(project_id):
        assert project_id == _PROJECT["id"]
        return _ACCESS

    monkeypatch.setattr(ai_conversations_repo, "get", get_conversation)
    monkeypatch.setattr(projects_repo, "find", find_projects)
    monkeypatch.setattr(launch_access, "get_launch_access", get_access)
    return TestClient(create_listener_app())


def _session(conversation_id=5):
    return {"Authorization": f"Bearer {tokens.to_session_token(conversation_id)}"}


@pytest.mark.parametrize(
    "path", ["/api/agent/launch/plans", "/api/agent/launch/domains", "/api/agent/launch/domain-discount"]
)
def test_every_launch_endpoint_needs_a_session_token(path, client):
    assert client.post(path, json={}).status_code == 403
    assert client.post(path, json={}, headers=_session(99)).status_code == 404  # a gone conversation


def test_plans_come_with_what_the_project_has(client):
    response = client.post("/api/agent/launch/plans", headers=_session())

    assert response.status_code == 200
    body = response.json()
    assert {plan["key"] for plan in body["plans_on_sale"]} == {"monthly", "yearly", "one_time"}
    assert body["this_site"]["plan"]["plan"] == "yearly"
    assert body["this_site"]["one_time"] is None
    assert body["this_site"]["access"]["can_host"] is True


def test_domains_are_searched_for_the_project_with_our_price(client, monkeypatch):
    searched = []

    async def fake_search(query, sandbox):
        searched.append((query, sandbox))
        return {
            "exact": [{"domain": "bikes.com", "available": False, "price_usd": 12, "min_years": 1}],
            "suggestions": [{"domain": "bikes.shop", "available": True, "price_usd": 4, "renewal_price_usd": 9, "min_years": 1}],
        }  # fmt: skip

    async def get_user(user_id):
        return {"id": user_id, "role": "user", "meta": {}}

    monkeypatch.setattr(domains, "search_domains", fake_search)
    monkeypatch.setattr(users_repo, "get", get_user)
    monkeypatch.setattr(settings, "sandbox_mode", False)

    response = client.post("/api/agent/launch/domains", json={"query": "bikes"}, headers=_session())

    assert response.status_code == 200
    assert searched == [("bikes", False)]
    # Sellable first, priced at our price point ($9 renewal -> $10/yr), taken ones unpriced.
    assert response.json()["domains"] == [
        {"domain": "bikes.shop", "available": True, "price_per_year": 10, "discountable": True, "sold": True},
        {"domain": "bikes.com", "available": False, "price_per_year": None, "discountable": False, "sold": False},
    ]


def test_an_empty_query_searches_around_the_projects_name(client, monkeypatch):
    searched = []

    async def fake_search(query, sandbox):
        searched.append(query)
        return {"exact": [], "suggestions": []}

    async def get_user(user_id):
        return {"id": user_id, "role": "user", "meta": {}}

    monkeypatch.setattr(domains, "search_domains", fake_search)
    monkeypatch.setattr(users_repo, "get", get_user)

    assert client.post("/api/agent/launch/domains", json={}, headers=_session()).json() == {"domains": []}
    assert searched == ["Hanoi Bike Rental"]


def test_a_registrar_failure_is_a_502(client, monkeypatch):
    async def fake_search(query, sandbox):
        raise domains.RegistrarError("namecom", 503, "down")

    async def get_user(user_id):
        return {"id": user_id, "role": "user", "meta": {}}

    monkeypatch.setattr(domains, "search_domains", fake_search)
    monkeypatch.setattr(users_repo, "get", get_user)

    response = client.post("/api/agent/launch/domains", json={"query": "bikes"}, headers=_session())
    assert response.status_code == 502 and response.json()["detail"] == "REGISTRAR_ERROR"


@pytest.mark.parametrize("eligible", [True, False])
def test_the_domain_discount_says_whether_the_project_gets_it(client, monkeypatch, eligible):
    async def fake_has_discount(project, access):
        assert project["id"] == _PROJECT["id"] and access is _ACCESS
        return eligible

    monkeypatch.setattr(checkouts, "has_domain_discount", fake_has_discount)

    response = client.post("/api/agent/launch/domain-discount", headers=_session())

    assert response.status_code == 200
    body = response.json()
    assert body["this_site"] == {"eligible": eligible}
    assert body["discount"]["terms"] == "first_year_free"
    assert body["discount"]["discountable_prices_per_year"] == [10, 15, 20]
    assert body["discount"]["rule"]


def test_a_rate_limited_registrar_is_a_503_busy(client, monkeypatch):
    async def fake_search(query, sandbox):
        raise domains.RegistrarError("namecom", 429, "Rate Limit Exceeded")

    async def get_user(user_id):
        return {"id": user_id, "role": "user", "meta": {}}

    monkeypatch.setattr(domains, "search_domains", fake_search)
    monkeypatch.setattr(users_repo, "get", get_user)

    response = client.post("/api/agent/launch/domains", json={"query": "bikes"}, headers=_session())
    assert response.status_code == 503 and response.json()["detail"] == "REGISTRAR_BUSY"


def test_the_agent_checks_a_list_of_names_in_one_go(client, monkeypatch):
    checked = []

    async def fake_check_domain_names(names, sandbox):
        checked.append(names)
        return [{"domain": "bikes.io", "available": True, "price_usd": 4, "renewal_price_usd": 9, "min_years": 1}]

    async def fake_search(query, sandbox):
        raise AssertionError("names given: no search")

    async def get_user(user_id):
        return {"id": user_id, "role": "user", "meta": {}}

    monkeypatch.setattr(domains, "check_domain_names", fake_check_domain_names)
    monkeypatch.setattr(domains, "search_domains", fake_search)
    monkeypatch.setattr(users_repo, "get", get_user)

    response = client.post("/api/agent/launch/domains", json={"names": ["bikes.io", "bikes"]}, headers=_session())
    assert response.status_code == 200 and checked == [["bikes.io", "bikes"]]
    assert response.json()["domains"][0]["domain"] == "bikes.io"
    too_many = client.post("/api/agent/launch/domains", json={"names": ["x"] * 21}, headers=_session())
    assert too_many.status_code == 422
