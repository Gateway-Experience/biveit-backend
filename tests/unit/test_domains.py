# The domains module (modules/domains): what a Name.com call sends and how its failures surface, and how a
# search reads what the user typed, and the admin test bench's gates (admins only, sandbox only).

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import settings
from src.modules import domains
from src.modules.domains.services import namecom
from src.utils.auth import get_current_user


@pytest.fixture(autouse=True)
def _namecom_accounts(monkeypatch):
    """Both of our Name.com accounts configured (with made-up credentials): a call without them fails up front."""
    for name in ["namecom_username", "namecom_api_token", "namecom_sandbox_username", "namecom_sandbox_api_token"]:
        monkeypatch.setattr(settings, name, f"test-{name}")


_ADMIN = {"id": 1, "username": "admin@biveit.com", "user_type": "USER", "role": "ADMIN"}
_USER = {"id": 2, "username": "someone@x.com", "user_type": "USER", "role": None}


async def test_a_call_sends_basic_auth_and_keeps_the_colon_verb(monkeypatch):
    sent = {}

    async def fake_request(method, url, **kwargs):
        sent.update(method=method, url=url, **kwargs)
        return 200, {"results": []}, {}

    monkeypatch.setattr(settings, "namecom_sandbox_username", "me-test")
    monkeypatch.setattr(settings, "namecom_sandbox_api_token", "token")
    monkeypatch.setattr(namecom.http, "request", fake_request)
    await namecom.check_availability(True, ["foo.com"])
    assert sent == {
        "method": "POST",
        "url": "https://api.dev.name.com/core/v1/domains:checkAvailability",
        "json": {"domainNames": ["foo.com"]},
        "headers": {"Authorization": "Basic bWUtdGVzdDp0b2tlbg=="},  # base64("me-test:token")
    }


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [(400, {"message": "Invalid Argument"}, "Invalid Argument"), (429, {}, "no error message")],
)
async def test_a_failed_call_raises_with_their_message(monkeypatch, status, body, message):
    async def fake_request(method, url, **kwargs):
        return status, body, {}

    monkeypatch.setattr(namecom.http, "request", fake_request)
    with pytest.raises(namecom.RegistrarError) as raised:
        await namecom.get_pricing(True, "foo.com")
    assert (raised.value.status, raised.value.message) == (status, message)


async def test_checking_puts_the_typed_tld_first_and_only_prices_plain_registrations(monkeypatch):
    checked = {}

    async def fake_check_availability(sandbox, domain_names):
        checked["domain_names"] = domain_names
        return [
            {"domainName": "myshop.io", "purchasable": False},
            {"domainName": "myshop.com", "purchasable": True, "purchaseType": "registration", "purchasePrice": 12.99},
            {"domainName": "myshop.net", "purchasable": True, "purchaseType": "aftermarket", "purchasePrice": 900},
        ]

    monkeypatch.setattr(namecom, "check_availability", fake_check_availability)
    offers = await domains.check_domains("https://www.My Shop.io/about", ["com", "io", "net"])
    assert checked["domain_names"] == ["myshop.io", "myshop.com", "myshop.net"]
    assert [(offer["domain"], offer["available"], offer["price_usd"]) for offer in offers] == [
        ("myshop.io", False, None),
        ("myshop.com", True, 12.99),
        ("myshop.net", False, None),  # aftermarket: not a one-click buy
    ]


async def test_a_query_with_no_usable_name_is_refused():
    with pytest.raises(domains.InvalidDomainQuery):
        await domains.check_domains("  ...  ")


@pytest.mark.parametrize(("env", "refused"), [("local", True), ("canary", True), ("prod", False)])
async def test_spending_on_live_namecom_is_refused_outside_prod(monkeypatch, env, refused):
    async def fake_request(method, url, **kwargs):
        return 200, {"domain": {"domainName": "foo.com"}, "order": 1, "totalPaid": 12.99}, {}

    monkeypatch.setattr(settings, "env", env)
    monkeypatch.setattr(namecom.http, "request", fake_request)
    if refused:
        with pytest.raises(namecom.LiveSpendRefused):
            await namecom.renew_domain(False, "foo.com", 1)
    else:
        await namecom.renew_domain(False, "foo.com", 1)


async def test_creating_a_domain_without_a_registrant_is_refused():
    with pytest.raises(ValueError):
        await namecom.create_domain(True, "foo.com", 1, {"admin": {"firstName": "A"}})


async def test_registering_echoes_the_premium_price_and_makes_the_user_every_contact(monkeypatch):
    sent = {}

    async def fake_check_availability(sandbox, domain_names):
        return [{"domainName": "foo.ai", "purchasable": True, "purchaseType": "registration", "premium": True}]

    async def fake_get_pricing(sandbox, domain_name, years):
        return {"purchasePrice": 400.0}

    async def fake_create_domain(sandbox, domain_name, years, contacts, purchase_price, idempotency_key):
        sent.update(contacts=contacts, purchase_price=purchase_price, idempotency_key=idempotency_key)
        return {"domain": {"domainName": "foo.ai", "expireDate": "2028-10-26T00:00:00Z"}, "order": 7, "totalPaid": 400}

    monkeypatch.setattr(namecom, "check_availability", fake_check_availability)
    monkeypatch.setattr(namecom, "get_pricing", fake_get_pricing)
    monkeypatch.setattr(namecom, "create_domain", fake_create_domain)
    user = {"firstName": "An", "email": "an@example.com"}
    registered = await domains.register_domain("foo.ai", 2, user, "order-1")
    assert sent == {
        "contacts": {"registrant": user, "admin": user, "tech": user, "billing": user},
        "purchase_price": 400.0,
        "idempotency_key": "order-1",
    }
    assert (registered["registrar_order_id"], registered["paid_usd"]) == (7, 400)


async def test_registering_an_aftermarket_domain_is_refused(monkeypatch):
    async def fake_check_availability(sandbox, domain_names):
        return [{"domainName": "foo.com", "purchasable": True, "purchaseType": "aftermarket"}]

    monkeypatch.setattr(namecom, "check_availability", fake_check_availability)
    with pytest.raises(domains.DomainUnavailable):
        await domains.register_domain("foo.com", 1, {"firstName": "An"}, "order-1")


@pytest.mark.parametrize(
    ("message", "unavailable"),
    [("Domain registration rejected by registry", True), ("Invalid phone number", False)],
)
async def test_a_name_the_registry_turns_down_counts_as_unavailable(monkeypatch, message, unavailable):
    async def fake_check_availability(sandbox, domain_names):
        return [{"domainName": "test2.app", "purchasable": True, "purchaseType": "registration"}]

    async def fake_create_domain(sandbox, domain_name, years, contacts, purchase_price, idempotency_key):
        raise domains.RegistrarError("namecom", 400, message)

    monkeypatch.setattr(namecom, "check_availability", fake_check_availability)
    monkeypatch.setattr(namecom, "create_domain", fake_create_domain)
    with pytest.raises(domains.DomainUnavailable if unavailable else domains.RegistrarError):
        await domains.register_domain("test2.app", 1, {"firstName": "An"}, "order-1")


def _client_as(user):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


@pytest.mark.parametrize(("user", "status"), [(_USER, 403), (_ADMIN, 200)])
def test_the_admin_test_bench_is_for_admins_and_always_works_on_the_sandbox(monkeypatch, user, status):
    worlds = []

    async def fake_get_balance(sandbox):
        worlds.append(sandbox)
        return 100000

    monkeypatch.setattr(namecom, "get_balance", fake_get_balance)
    monkeypatch.setattr(namecom, "get_account_name", lambda sandbox: "me-test" if sandbox else "me")
    response = _client_as(user).get("/api/admin/domain-test/account")
    assert response.status_code == status
    if status == 200:
        assert response.json() == {"username": "me-test", "balance_usd": 100000} and worlds == [True]


def test_a_registrar_refusal_reaches_the_test_bench_with_its_status_and_message(monkeypatch):
    async def fake_update_domain(sandbox, domain_name, changes):
        raise namecom.RegistrarError("namecom", 409, "Domain can not be unlocked until 2026-12-25T11:19:09Z")

    monkeypatch.setattr(namecom, "update_domain", fake_update_domain)
    response = _client_as(_ADMIN).patch("/api/admin/domain-test/domains/foo.com", json={"locked": False})
    assert (response.status_code, response.json()["detail"]) == (
        409,
        "Domain can not be unlocked until 2026-12-25T11:19:09Z",
    )


@pytest.fixture
def memory_cache(monkeypatch):
    """cache.with_cache over a dict instead of Redis; returns the dict (key -> value)."""
    stored = {}

    async def fake_with_cache(key, expires_in, func):
        if key not in stored:
            stored[key] = await func()
        return stored[key]

    monkeypatch.setattr(domains.services.domains.cache, "with_cache", fake_with_cache)
    return stored


async def test_suggestions_leave_out_what_the_exact_search_already_shows(monkeypatch, memory_cache):
    async def fake_check_domains(query, sandbox):
        return [{"domain": "foo.com", "available": True}, {"domain": "foo.ai", "available": False}]

    async def fake_suggest_domains(query, sandbox):
        return [{"domain": "foo.com", "available": True}, {"domain": "foo.shop", "available": True}]

    async def fake_get_min_years(domain_name, sandbox):
        return 1

    monkeypatch.setattr(domains.services.domains, "check_domains", fake_check_domains)
    monkeypatch.setattr(domains.services.domains, "suggest_domains", fake_suggest_domains)
    monkeypatch.setattr(domains.services.domains, "get_min_years", fake_get_min_years)
    result = await domains.search_domains("foo")
    assert [(offer["domain"], offer["min_years"]) for offer in result["exact"]] == [("foo.com", 1), ("foo.ai", None)]
    assert [offer["domain"] for offer in result["suggestions"]] == ["foo.shop"]


async def test_the_shortest_term_is_the_first_one_the_registrar_prices(monkeypatch):
    asked = {"years": []}

    async def fake_get_pricing(sandbox, domain_name, years):
        asked["years"] = [*asked["years"], years]
        if years < 2:
            raise namecom.RegistrarError("namecom", 400, "Invalid value for years for this domain")
        return {"purchasePrice": 199.98}

    monkeypatch.setattr(namecom, "get_pricing", fake_get_pricing)
    assert await namecom.get_min_years(True, "foo.ai") == 2
    assert asked["years"] == [1, 2]


@pytest.fixture
def inet_sells_vn(monkeypatch):
    """A made-up second registrar taking every *.vn, to show one slots in through the routing table alone."""
    service = domains.services.domains
    checked = {}

    async def fake_check_availability(registrar, domain_names):
        checked[registrar] = domain_names
        return [{"domainName": name, "purchasable": True, "purchaseType": "registration"} for name in domain_names]

    async def fake_create_domain(domain_name, years, contacts, purchase_price, idempotency_key):
        return {"domain": {"domainName": domain_name}, "order": 1, "totalPaid": 18}

    inet = SimpleNamespace(
        check_availability=lambda names: fake_check_availability("inet", names), create_domain=fake_create_domain
    )
    monkeypatch.setattr(service, "REGISTRAR_BY_TLD", {"vn": "inet"})
    monkeypatch.setattr(service, "_REGISTRARS", {**service._REGISTRARS, "inet": inet})
    monkeypatch.setattr(namecom, "check_availability", lambda sandbox, names: fake_check_availability("namecom", names))
    return checked


async def test_each_registrar_checks_the_tlds_it_sells_and_the_order_is_kept(inet_sells_vn):
    offers = await domains.check_domains("myshop", ["com", "vn", "com.vn", "io"])
    assert [offer["domain"] for offer in offers] == ["myshop.com", "myshop.vn", "myshop.com.vn", "myshop.io"]
    assert inet_sells_vn == {"namecom": ["myshop.com", "myshop.io"], "inet": ["myshop.vn", "myshop.com.vn"]}


async def test_registering_goes_to_the_routed_registrar_and_says_which(inet_sells_vn):
    registered = await domains.register_domain("myshop.com.vn", 1, {"firstName": "An"}, "order-1")
    assert registered["registrar"] == "inet"


async def test_a_new_name_goes_to_the_registrars_sandbox_in_sandbox_mode(monkeypatch):
    worlds = {}

    async def fake_check_availability(sandbox, domain_names):
        worlds["check"] = sandbox
        return [{"domainName": "foo.com", "purchasable": True, "purchaseType": "registration"}]

    async def fake_create_domain(sandbox, domain_name, years, contacts, purchase_price, idempotency_key):
        worlds["create"] = sandbox
        return {"domain": {"domainName": domain_name}, "order": 1, "totalPaid": 9}

    monkeypatch.setattr(namecom, "check_availability", fake_check_availability)
    monkeypatch.setattr(namecom, "create_domain", fake_create_domain)
    registered = await domains.register_domain("foo.com", 1, {"firstName": "An"}, "order-1", sandbox=True)
    # It's held at the sandbox registrar now -- every later call for it goes there.
    assert registered["registrar"] == "namecom_sandbox" and worlds == {"check": True, "create": True}
    assert domains.get_registrar("foo.com", sandbox=False) == "namecom"


async def test_a_domain_held_on_the_sandbox_is_managed_there(monkeypatch):
    async def fake_list_records(sandbox, domain_name):
        return [{"at": "sandbox" if sandbox else "live"}]

    monkeypatch.setattr(namecom, "list_records", fake_list_records)
    assert await domains.list_records("foo.com", domains.get_sandbox_registrar()) == [{"at": "sandbox"}]
    assert await domains.list_records("foo.com") == [{"at": "live"}]


async def test_a_world_without_credentials_fails_instead_of_calling(monkeypatch):
    monkeypatch.setattr(settings, "namecom_username", "")
    with pytest.raises(namecom.RegistrarError) as raised:
        await namecom.get_balance(False)
    assert raised.value.status == 401


async def test_a_held_domain_is_managed_where_the_caller_says_it_lives(inet_sells_vn, monkeypatch):
    async def fake_get_domain(sandbox, domain_name):
        return {"domainName": domain_name, "at": "namecom"}

    monkeypatch.setattr(namecom, "get_domain", fake_get_domain)
    # Routing would send a .vn to inet, but this one was registered at namecom -- so that's where it's read.
    assert (await domains.get_domain("myshop.vn", registrar="namecom"))["at"] == "namecom"


async def test_a_repeated_search_is_answered_from_the_cache(monkeypatch, memory_cache):
    calls = {"check": 0}

    async def fake_check_domains(query, sandbox):
        calls["check"] += 1
        return [{"domain": "foo.com", "available": False}]

    async def fake_suggest_domains(query, sandbox):
        return []

    monkeypatch.setattr(domains.services.domains, "check_domains", fake_check_domains)
    monkeypatch.setattr(domains.services.domains, "suggest_domains", fake_suggest_domains)
    first = await domains.search_domains("Foo ")
    assert await domains.search_domains("foo") == first
    assert calls["check"] == 1
    # The sandbox is a different world: asked separately.
    await domains.search_domains("foo", sandbox=True)
    assert calls["check"] == 2


async def test_the_shortest_term_is_looked_up_once_per_tld(monkeypatch, memory_cache):
    asked = {"domains": []}

    async def fake_check_domains(query, sandbox):
        return [{"domain": "foo.shop", "available": True}, {"domain": "foo.ai", "available": True}]

    async def fake_suggest_domains(query, sandbox):
        return [{"domain": "myfoo.shop", "available": True}, {"domain": "foohq.shop", "available": True}]

    async def fake_get_min_years(domain_name, sandbox):
        asked["domains"] = [*asked["domains"], domain_name]
        return 2 if domain_name.endswith(".ai") else 1

    monkeypatch.setattr(domains.services.domains, "check_domains", fake_check_domains)
    monkeypatch.setattr(domains.services.domains, "suggest_domains", fake_suggest_domains)
    monkeypatch.setattr(domains.services.domains, "get_min_years", fake_get_min_years)
    result = await domains.search_domains("foo")
    assert sorted(asked["domains"]) == ["foo.ai", "foo.shop"]
    assert [offer["min_years"] for offer in result["exact"] + result["suggestions"]] == [1, 2, 1, 1]


async def test_a_failed_term_lookup_leaves_out_only_that_tlds_offers(monkeypatch, memory_cache):
    async def fake_check_domains(query, sandbox):
        return [{"domain": "foo.com", "available": True}, {"domain": "foo.io", "available": False}]

    async def fake_suggest_domains(query, sandbox):
        return [{"domain": "foo.shop", "available": True}, {"domain": "foohq.com", "available": True}]

    async def fake_get_min_years(domain_name, sandbox):
        if domain_name.endswith(".shop"):
            raise namecom.RegistrarError("namecom", 429, "Rate Limit Exceeded")
        return 1

    monkeypatch.setattr(domains.services.domains, "check_domains", fake_check_domains)
    monkeypatch.setattr(domains.services.domains, "suggest_domains", fake_suggest_domains)
    monkeypatch.setattr(domains.services.domains, "get_min_years", fake_get_min_years)
    result = await domains.search_domains("foo")
    assert [offer["domain"] for offer in result["exact"]] == ["foo.com", "foo.io"]
    assert [offer["domain"] for offer in result["suggestions"]] == ["foohq.com"]


async def test_namecom_calls_are_spaced_out_under_their_rate_limit(monkeypatch):
    started = {"at": []}

    async def fake_request(method, url, json=None, headers=None):
        started["at"] = [*started["at"], time.monotonic()]
        return 200, {"balance": 1.0}, {}

    monkeypatch.setattr(namecom.http, "request", fake_request)
    monkeypatch.setattr(namecom, "_MAX_REQUESTS_PER_SECOND", 50)
    await asyncio.gather(*[namecom.get_balance(True) for _ in range(5)])
    gaps = [later - earlier for earlier, later in zip(started["at"], started["at"][1:], strict=False)]
    assert len(gaps) == 4 and all(gap >= 0.018 for gap in gaps)
