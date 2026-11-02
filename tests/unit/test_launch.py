# Launch flow tests (services/launch + the domain side of modules/deployments): which stage a project is in, what a
# checkout may be (and when a domain is free for a year), checking a user's own domain, and fulfilment after a
# payment -- registering a bought domain, or refunding it when it's gone. Payments, the registrar, DNS and the
# tables are faked.

from datetime import UTC, datetime, timedelta

import pytest

from src.config import settings
from src.dto.launch import LaunchCheckoutRequest, Registrant
from src.modules import deployments
from src.modules.deployments.services import sites
from src.modules.domains import DomainUnavailable, RegistrarError
from src.services.launch import access as launch_access
from src.services.launch import checkouts, domain_events, fulfilment, offers, status

_NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
_PROJECT = {"id": 42, "hash": "p42", "user_id": 7, "name": "Cozy Bakery", "build_approved_at": _NOW}
_USER = {"id": 7, "username": "anh@example.com"}
_REGISTRANT = {
    "first_name": "Anh", "last_name": "Le", "address1": "1 Street", "city": "HCMC", "state": "HCM", "zip": "70000",
    "country": "vn", "email": "anh@example.com", "phone": "+84.901234567",
}  # fmt: skip


# --- the stage ---


def _status(**overrides):
    base = {
        "access": {"can_download": True, "can_host": True, "has_had_plan": True},
        "plan": {"plan": "monthly"},
        "one_time": None,
        "registration_failure": None,
        "domain_subscription": None,
        "site_domains": [],
        "domain": None,
        "setting_up": None,
        "setup_failure": None,
        "deployment": {"deploying": False, "last_deployment": None},
    }
    return {**base, **overrides}


_ACTIVE = {"domain": "cozy.com", "kind": "purchased", "activated_at": _NOW, "servable": True, "check": None}
_ATTACHED = [{"domain": "cozy.com", "kind": "external", "servable": False, "active": False}]


@pytest.mark.parametrize(
    ("overrides", "stage"),
    [
        ({"plan": None, "access": {"can_download": False, "has_had_plan": False}}, "not_paid"),
        ({"plan": None, "access": {"can_download": True, "has_had_plan": False}, "one_time": {"paid_at": _NOW}}, "code_only"),
        ({"plan": None, "access": {"can_download": True, "has_had_plan": True}}, "ended"),
        # In a grace period hosting carries on, but the plan's over: it's ended, and asks for a new plan.
        ({"plan": None, "access": {"can_download": True, "can_host": True, "has_had_plan": True}}, "ended"),
        ({}, "choose_domain"),
        ({"registration_failure": {"domain": "cozy.com"}}, "choose_domain"),
        ({"domain_subscription": {"domain": "cozy.com"}}, "registering_domain"),
        ({"site_domains": _ATTACHED}, "waiting_dns"),
        ({"site_domains": _ATTACHED, "setting_up": "cozy.com"}, "deploying"),
        ({"site_domains": _ATTACHED, "setup_failure": {"domain": "cozy.com"}}, "failed"),
        ({"domain": _ACTIVE}, "live"),
        # Being switched away from: still live on the active one while the other is set up.
        ({"domain": _ACTIVE, "setting_up": "new.com", "deployment": {"deploying": True}}, "live"),
        ({"domain": _ACTIVE, "deployment": {"deploying": True}}, "deploying"),
        ({"domain": {**_ACTIVE, "kind": "external", "servable": False}}, "not_servable"),
    ],
)  # fmt: skip
def test_the_stage_says_where_a_project_is(overrides, stage):
    assert status.to_stage(_status(**overrides)) == stage


# --- checkouts ---


_PLAN_STARTED = _NOW - timedelta(days=1)


@pytest.fixture
def launch_world(monkeypatch):
    """Payments (its checkouts kept in memory, like their events) and the registrar quote faked; records what was
    asked for."""
    state = {
        "access": {
            "can_download": False,
            "has_had_plan": False,
            "plan_subscription": None,
            "domain_subscription": None,
        },
        "checkouts": {},  # id -> what payments would record in its payment.checkout_created event
        "domain_subscriptions": [],
        "failures": [],
    }

    async def get_project_access(project_id):
        return state["access"]

    async def quote_domain(name, sandbox):
        return state.get(
            "quote",
            {"domain": name, "available": True, "sold": True, "price_per_year": 15, "discountable": True},
        )

    async def get_domain_failures(project_id):
        return state["failures"]

    async def find_subscriptions(criteria):
        return state["domain_subscriptions"]

    def record_checkout(checkout):
        checkout_id = len(state["checkouts"]) + 1
        state["checkouts"] = {**state["checkouts"], checkout_id: {"checkout_id": checkout_id, **checkout}}
        return {"checkout_id": checkout_id, "url": f"https://pay/{checkout_id}"}

    async def create_plan_checkout(user, project, plan):
        return record_checkout({"product": "plan", "plan": plan})

    async def create_domain_checkout(user, project, domain, price, discounted, details):
        return record_checkout(
            {
                "product": "domain",
                "domain": domain,
                "price_per_year": price,
                "discounted": discounted,
                **details,
            }
        )

    for name, fake in [
        ("get_project_access", get_project_access),
        ("create_plan_checkout", create_plan_checkout),
        ("create_domain_checkout", create_domain_checkout),
        ("find_subscriptions", find_subscriptions),
    ]:
        monkeypatch.setattr(checkouts.payments, name, fake)
    monkeypatch.setattr(checkouts.offers, "quote_domain", quote_domain)
    monkeypatch.setattr(domain_events, "get_domain_failures", get_domain_failures)
    return state


def _on_the_plan(world, plan="yearly"):
    """[Pure] The world's access once a plan is running."""
    return {
        **world["access"],
        "can_download": True,
        "can_host": True,
        "has_had_plan": True,
        "plan_subscription": {
            "plan": plan,
            "status": "active",
            "created_at": _PLAN_STARTED,
            "provider_ref": "plan-1",
            "custom_data": {"checkout_id": "41"},
        },
    }


def _bought(world):
    """[Pure] What the checkouts were for, in order: (product, plan or domain, free first year)."""
    return [
        (checkout["product"], checkout.get("plan") or checkout.get("domain"), checkout.get("discounted"))
        for checkout in world["checkouts"].values()
    ]


async def test_a_quote_is_the_registrars_answer_with_our_price(monkeypatch):
    async def check_domains(query, tlds, sandbox=False):
        return [{"domain": query, "available": True, "premium": False, "price_usd": 12, "renewal_price_usd": 14}]

    async def get_min_years(domain_name, sandbox=False):
        return 1

    # monkeypatch refuses names the module doesn't have -- so this also checks the domains module exports them.
    monkeypatch.setattr(offers.domains, "check_domains", check_domains)
    monkeypatch.setattr(offers.domains, "get_min_years", get_min_years)
    quote = await offers.quote_domain("cozy.com", False)
    assert (quote["domain"], quote["available"], quote["sold"], quote["price_per_year"]) == ("cozy.com", True, True, 15)


def _request(**fields):
    return LaunchCheckoutRequest(**fields)


def _domain_request(name, **domain_fields):
    return _request(domain={"name": name, **domain_fields}, registrant=_REGISTRANT)


async def test_a_plan_checkout_is_just_the_plan(launch_world):
    result = await checkouts.start_checkout(_PROJECT, _USER, _request(plan="yearly"))
    assert result == {"product": "plan", "url": "https://pay/1"}
    assert launch_world["checkouts"][1] == {"checkout_id": 1, "product": "plan", "plan": "yearly"}


async def test_the_yearly_plans_first_domain_is_free_and_holds_its_registrant(launch_world):
    launch_world["access"] = _on_the_plan(launch_world)
    result = await checkouts.start_checkout(_PROJECT, _USER, _domain_request("Cozy.com", price_per_year=15))
    assert result == {"product": "domain", "url": "https://pay/1"}
    assert _bought(launch_world) == [("domain", "cozy.com", True)]
    assert launch_world["checkouts"][1]["registrant"]["country"] == "VN"
    # The discount explains itself: what it is, and the paid yearly plan that earned it.
    assert launch_world["checkouts"][1]["discount"] == {
        "terms": "first_year_free",
        "plan_checkout_id": 41,
        "plan_subscription_ref": "plan-1",
    }


async def test_a_domain_on_the_monthly_plan_pays_full_price(launch_world):
    launch_world["access"] = _on_the_plan(launch_world, "monthly")
    await checkouts.start_checkout(_PROJECT, _USER, _domain_request("cozy.com"))
    assert _bought(launch_world) == [("domain", "cozy.com", False)]
    assert launch_world["checkouts"][1]["discount"] is None


async def test_after_a_bought_domain_fails_another_keeps_the_free_year_until_one_stands(launch_world):
    # a.com was bought free, then couldn't be registered: its subscription is cancelled (but still "running" until
    # its period ends) and the failure recorded.
    sub_a = {"provider_ref": "sub-a", "status": "cancelled", "trial_ends_at": _NOW, "product_data": {"domain": "a.com"}}
    launch_world["access"] = {**_on_the_plan(launch_world), "domain_subscription": sub_a}
    launch_world["domain_subscriptions"] = [sub_a]
    launch_world["failures"] = [{"subscription_ref": "sub-a", "domain": "a.com"}]
    await checkouts.start_checkout(_PROJECT, _USER, _domain_request("b.com"))
    assert _bought(launch_world)[-1] == ("domain", "b.com", True)
    # b.com was bought free and stands (then, say, cancelled): the discount is used up.
    sub_b = {"provider_ref": "sub-b", "status": "ended", "trial_ends_at": _NOW, "product_data": {"domain": "b.com"}}
    launch_world["domain_subscriptions"] = [sub_a, sub_b]
    launch_world["access"] = {**launch_world["access"], "domain_subscription": None}
    await checkouts.start_checkout(_PROJECT, _USER, _domain_request("c.com"))
    assert _bought(launch_world)[-1] == ("domain", "c.com", False)


async def test_the_discount_is_once_per_project_even_on_a_later_yearly_plan(launch_world):
    # A domain bought free under an earlier yearly plan: a new yearly plan doesn't bring another.
    earlier = {"provider_ref": "sub-old", "status": "ended", "trial_ends_at": _PLAN_STARTED - timedelta(days=30)}
    launch_world["domain_subscriptions"] = [earlier]
    launch_world["access"] = _on_the_plan(launch_world)
    await checkouts.start_checkout(_PROJECT, _USER, _domain_request("cozy.com"))
    assert _bought(launch_world) == [("domain", "cozy.com", False)]


async def test_a_domain_paid_in_full_doesnt_use_up_the_discount(launch_world):
    on_monthly = {"provider_ref": "sub-old", "status": "ended", "trial_ends_at": None}
    launch_world["domain_subscriptions"] = [on_monthly]
    launch_world["access"] = _on_the_plan(launch_world)
    await checkouts.start_checkout(_PROJECT, _USER, _domain_request("cozy.com"))
    assert _bought(launch_world) == [("domain", "cozy.com", True)]


async def test_a_domain_can_be_bought_after_the_plan_ended(launch_world):
    launch_world["access"] = {**launch_world["access"], "can_download": True, "has_had_plan": True}
    await checkouts.start_checkout(_PROJECT, _USER, _domain_request("cozy.com"))
    assert _bought(launch_world) == [("domain", "cozy.com", False)]


async def test_the_launch_access_drops_a_failed_domain(monkeypatch):
    subscription = {"provider_ref": "sub-a", "status": "cancelled"}

    async def get_project_access(project_id):
        return {"can_host": True, "domain_subscription": subscription}

    async def get_domain_failures(project_id):
        return [{"subscription_ref": "sub-a", "domain": "a.com"}] if project_id == 42 else []

    monkeypatch.setattr(launch_access.payments, "get_project_access", get_project_access)
    monkeypatch.setattr(launch_access.domain_events, "get_domain_failures", get_domain_failures)
    failed = await launch_access.get_launch_access(42)
    assert failed["domain_subscription"] is None and failed["domain_failures"][0]["domain"] == "a.com"
    assert (await launch_access.get_launch_access(7))["domain_subscription"] == subscription


def test_a_failed_domain_with_the_plan_running_asks_for_another_domain():
    stage = status.to_stage(_status(registration_failure={"domain": "a.com"}, domain_subscription=None))
    assert stage == "choose_domain"


@pytest.mark.parametrize(
    ("access", "fields", "code"),
    [
        ({"plan_subscription": {"plan": "monthly"}}, {"plan": "yearly"}, "ALREADY_SUBSCRIBED"),
        ({"can_download": True}, {"plan": "one_time"}, "ALREADY_PURCHASED"),
        ({}, {"domain": {"name": "cozy.com"}, "registrant": _REGISTRANT}, "PLAN_REQUIRED"),
        (
            {"has_had_plan": True, "domain_subscription": {"domain": "a.com"}},
            {"domain": {"name": "b.com"}, "registrant": _REGISTRANT},
            "DOMAIN_ALREADY_BOUGHT",
        ),
    ],
)
async def test_what_isnt_allowed_now_is_refused(launch_world, access, fields, code):
    launch_world["access"] = {**launch_world["access"], **access}
    with pytest.raises(checkouts.LaunchRefused) as refused:
        await checkouts.start_checkout(_PROJECT, _USER, _request(**fields))
    assert refused.value.code == code and launch_world["checkouts"] == {}


async def test_a_domain_that_changed_since_the_dialog_is_refused(launch_world):
    launch_world["access"] = _on_the_plan(launch_world)
    launch_world["quote"] = {
        "domain": "cozy.com",
        "available": True,
        "sold": True,
        "price_per_year": 20,
        "discountable": True,
    }
    with pytest.raises(checkouts.LaunchRefused) as refused:
        await checkouts.start_checkout(_PROJECT, _USER, _domain_request("cozy.com", price_per_year=15))
    assert refused.value.code == "PRICE_CHANGED" and refused.value.details == {"price_per_year": 20}
    launch_world["quote"] = {"domain": "cozy.com", "available": False, "sold": False, "price_per_year": None}
    with pytest.raises(checkouts.LaunchRefused, match="DOMAIN_UNAVAILABLE"):
        await checkouts.start_checkout(_PROJECT, _USER, _domain_request("cozy.com"))


async def test_an_unapproved_site_cant_launch(launch_world):
    with pytest.raises(checkouts.LaunchRefused, match="NOT_APPROVED"):
        await checkouts.start_checkout({**_PROJECT, "build_approved_at": None}, _USER, _request(plan="one_time"))


@pytest.fixture
def own_domain_world(monkeypatch):
    """What adding a user's own domain touches, faked: the domain check and attaching."""
    state = {"blocked": None, "attaches": []}

    async def check_external_domain(project, domain):
        return {"domain": domain.lower(), "blocked": state["blocked"], "records": [], "verified": False}

    async def attach_domain(project, domain, kind):
        state["attaches"] = [*state["attaches"], (domain, kind)]

    async def fulfil(project_id):
        pass

    monkeypatch.setattr(status.deployments, "check_external_domain", check_external_domain)
    monkeypatch.setattr(status.deployments, "attach_domain", attach_domain)
    monkeypatch.setattr(status.fulfilment, "fulfil", fulfil)
    return state


async def test_adding_an_own_domain_attaches_it_no_plan_needed(own_domain_world):
    check = await status.add_own_domain(_PROJECT, "Mine.com")
    assert check["domain"] == "mine.com"
    assert own_domain_world["attaches"] == [("mine.com", "external")]


async def test_a_users_own_domain_another_project_has_is_refused(own_domain_world):
    own_domain_world["blocked"] = "taken"
    with pytest.raises(checkouts.LaunchRefused, match="DOMAIN_TAKEN"):
        await status.add_own_domain(_PROJECT, "cozy.com")
    assert own_domain_world["attaches"] == []


async def test_attaching_a_domain_never_makes_it_active(monkeypatch):
    calls = {"attached": [], "activated": []}

    async def attach(project_id, domain, kind):
        calls["attached"] = [*calls["attached"], domain]

    async def activate(project_id, domain):
        calls["activated"] = [*calls["activated"], domain]

    monkeypatch.setattr(sites.site_domains_repo, "attach", attach)
    monkeypatch.setattr(sites.site_domains_repo, "activate", activate)
    await deployments.attach_domain(_PROJECT, "First.com", deployments.SiteDomainKind.EXTERNAL)
    assert calls == {"attached": ["first.com"], "activated": []}


@pytest.mark.parametrize(
    ("typed", "stored"),
    [("+84.901234567", "+84901234567"), ("+84 90 123 4567", "+84901234567"), ("+1 (415) 555-0100", "+14155550100")],
)
def test_the_registrants_phone_is_kept_as_plain_e164(typed, stored):
    assert Registrant(**{**_REGISTRANT, "phone": typed}).phone == stored


@pytest.mark.parametrize("typed", ["0901234567", "+0901234567", "+84", "+84 abc 1234"])
def test_a_phone_that_isnt_e164_is_refused(typed):
    with pytest.raises(ValueError):
        Registrant(**{**_REGISTRANT, "phone": typed})


def test_the_registrar_gets_the_phone_without_the_dot_older_launches_stored():
    assert fulfilment._to_registrar_contact({**_REGISTRANT, "phone": "+84.901234567"})["phone"] == "+84901234567"


def test_a_checkout_is_one_thing_and_a_domain_needs_its_registrant():
    with pytest.raises(ValueError, match="registrant"):
        _request(domain={"name": "cozy.com"})
    with pytest.raises(ValueError, match="one at a time"):
        _request(plan="yearly", domain={"name": "cozy.com"}, registrant=_REGISTRANT)


# --- a user's own domain ---


@pytest.fixture
def dns(monkeypatch):
    """DNS answers and the attached domains faked."""
    state = {"answers": {}, "owners": []}

    async def resolve(name, record_type):
        return state["answers"].get((name, record_type), set())

    async def find(criteria):
        return [row for row in state["owners"] if row["domain"] == criteria["domain"]]

    monkeypatch.setattr(settings, "public_hosting", True)  # a dev box's .env.local may turn it off
    monkeypatch.setattr(settings, "project_secret_seed", "seed-for-tests")
    monkeypatch.setattr(settings, "server_public_ip", "57.0.0.1")
    monkeypatch.setattr(settings, "dev_domain", "dev.biveitapp.com")
    monkeypatch.setattr(settings, "prod_domain", "prod.biveitapp.com")
    monkeypatch.setattr(settings, "frontend_url", "https://app.biveit.com")
    monkeypatch.setattr(sites, "_resolve", resolve)
    monkeypatch.setattr(sites.site_domains_repo, "find", find)
    return state


async def test_a_users_own_domain_is_verified_once_both_records_are_there(dns):
    check = await deployments.check_external_domain(_PROJECT, "https://Shop.Example.com/about")
    assert check["domain"] == "shop.example.com" and check["blocked"] is None and not check["verified"]
    a_record, txt_record = check["records"]
    assert (a_record["name"], a_record["value"], txt_record["name"]) == (
        "shop.example.com",
        "57.0.0.1",
        "_biveit-verify.shop.example.com",
    )
    dns["answers"] = {("shop.example.com", "A"): {"57.0.0.1"}}
    assert not (await deployments.check_external_domain(_PROJECT, "shop.example.com"))["verified"]
    dns["answers"] = {**dns["answers"], ("_biveit-verify.shop.example.com", "TXT"): {txt_record["value"]}}
    assert (await deployments.check_external_domain(_PROJECT, "shop.example.com"))["verified"]
    # The token is the project's: another project's record doesn't prove anything for this one.
    other = await deployments.check_external_domain({**_PROJECT, "id": 43}, "shop.example.com")
    assert other["records"][1]["value"] != txt_record["value"] and not other["verified"]


async def test_on_a_dev_box_public_dns_checks_count_as_passed(dns, monkeypatch):
    # A sandbox-registered domain never reaches public DNS, and nothing points at a dev box anyway.
    monkeypatch.setattr(settings, "public_hosting", False)
    assert (await deployments.check_external_domain(_PROJECT, "shop.example.com"))["verified"]
    assert await deployments.is_pointed_at_server("cozy.com")
    # What makes a domain off-limits still does.
    assert (await deployments.check_external_domain(_PROJECT, "x.biveitapp.com"))["blocked"] == "ours"


async def test_an_aaaa_record_must_go_before_the_domain_counts_as_ready(dns):
    check = await deployments.check_external_domain(_PROJECT, "shop.example.com")
    txt_value = check["records"][1]["value"]
    dns["answers"] = {
        ("shop.example.com", "A"): {"57.0.0.1"},
        ("_biveit-verify.shop.example.com", "TXT"): {txt_value},
        ("shop.example.com", "AAAA"): {"2001:db8::1"},
    }
    check = await deployments.check_external_domain(_PROJECT, "shop.example.com")
    assert check["records"][2] == {
        "type": "AAAA", "name": "shop.example.com", "value": "2001:db8::1", "action": "remove", "found": True,
    }  # fmt: skip
    assert not check["verified"]


@pytest.mark.parametrize(
    ("domain", "blocked"),
    [
        ("not a domain", "invalid"),
        ("x.biveitapp.com", "ours"),
        ("biveit.com", "ours"),
        ("taken.com", "taken"),
        ("mine.com", None),
    ],
)
async def test_some_domains_can_never_be_brought(dns, domain, blocked):
    dns["owners"] = [{"domain": "taken.com", "project_id": 99}, {"domain": "mine.com", "project_id": 42}]
    assert (await deployments.check_external_domain(_PROJECT, domain))["blocked"] == blocked


# --- fulfilment ---


@pytest.fixture
def fulfilment_world(monkeypatch):
    """Everything fulfilment touches, faked: payments, the registrar, attached domains, setting a domain up."""
    subscription = {
        "provider": "polar",
        "product_data": {"domain": "cozy.com"},
        "provider_ref": "sub-1",
        "custom_data": {"checkout_id": "2"},
    }
    state = {
        "access": {"can_host": True, "domain_subscription": subscription},
        "attached": [], "registered": [], "records": [], "refunded": [], "cancelled": [], "failures": [],
        "registrar_has": set(), "unavailable": False, "servable": {}, "now": _NOW, "points_at_us": False,
        "registrations": [], "set_up": [], "activation": {"domain": None, "failure": None}, "registrars": set(),
    }  # fmt: skip

    async def get_project_access(project_id):
        return state["access"]

    async def find_projects(criteria):
        return [_PROJECT]

    async def set_if_absent(key, ttl):
        return True

    async def delete(key):
        pass

    async def get_recorded_checkout(checkout_id):
        # The domain's checkout: who it's registered to.
        domain_checkout = {
            "checkout_id": 2,
            "product": "domain",
            "domain": "cozy.com",
            "registrant": {**_REGISTRANT, "country": "VN"},
        }
        return domain_checkout if checkout_id == "2" else None

    async def get_domain_failures(project_id):
        return state["failures"]

    async def record_domain_registered(project_id, checkout_id, subscription_ref, registration, registrant):
        state["registrations"] = [
            *state["registrations"],
            (checkout_id, subscription_ref, registration, registrant["country"]),
        ]

    async def record_domain_failure(project_id, checkout_id, subscription_ref, domain, reason):
        failure = {"checkout_id": checkout_id, "subscription_ref": subscription_ref, "domain": domain, "reason": reason}
        state["failures"] = [*state["failures"], failure]

    async def get_domain(domain, registrar):
        state["registrars"] = {*state["registrars"], registrar}
        if domain not in state["registrar_has"]:
            raise RegistrarError("namecom", 404, "not found")
        return {"domainName": domain}

    async def register_domain(domain, years, registrant, key, sandbox):
        if state["unavailable"]:
            raise DomainUnavailable(domain)
        state["registered"] = [*state["registered"], (domain, years, registrant["country"], key)]
        registrar = "namecom_sandbox" if sandbox else "namecom"
        state["registrars"] = {*state["registrars"], registrar}
        return {"domain": domain, "registrar": registrar, "registrar_order_id": 7, "paid_usd": 9.99}

    async def list_records(domain, registrar):
        state["registrars"] = {*state["registrars"], registrar}
        return state["records"]

    async def create_record(domain, record, registrar):
        state["registrars"] = {*state["registrars"], registrar}
        state["records"] = [*state["records"], record]

    async def get_site_domains(project):
        return state["attached"]

    async def attach_domain(project, domain, kind):
        # Like the real one: kept if it's there, never made active.
        if not any(row["domain"] == domain for row in state["attached"]):
            state["attached"] = [
                *state["attached"],
                {"domain": domain, "kind": kind, "activated_at": None, "created_at": state["now"]},
            ]

    async def is_servable(project, site_domain):
        return state["servable"].get(site_domain["domain"], site_domain["kind"] != "external")

    async def is_pointed_at_server(domain):
        return state["points_at_us"]

    async def get_activation(project):
        return state["activation"]

    async def get_current_conversation(project_id, user_id):
        return {"id": 9}

    async def activate_domain(project, conversation_id, domain):
        # Like the real one, done at once: set up, then active.
        state["set_up"] = [*state["set_up"], domain]
        state["attached"] = [
            {**row, "activated_at": state["now"] if row["domain"] == domain else None} for row in state["attached"]
        ]

    async def revoke_subscription(subscription):
        state["cancelled"] = [*state["cancelled"], subscription["provider_ref"]]

    async def refund_latest_payment(subscription):
        state["refunded"] = [*state["refunded"], subscription["provider_ref"]]

    monkeypatch.setattr(settings, "server_public_ip", "57.0.0.1")
    for target, name, fake in [
        (fulfilment.payments, "get_project_access", get_project_access),
        (fulfilment.payments, "revoke_subscription", revoke_subscription),
        (fulfilment.payments, "refund_latest_payment", refund_latest_payment),
        (fulfilment.projects_repo, "find", find_projects),
        (fulfilment.redis, "set_if_absent", set_if_absent),
        (fulfilment.redis, "delete", delete),
        (fulfilment.payments, "get_recorded_checkout", get_recorded_checkout),
        (domain_events, "get_domain_failures", get_domain_failures),
        (domain_events, "record_domain_registered", record_domain_registered),
        (domain_events, "record_domain_failure", record_domain_failure),
        (fulfilment.domains, "get_domain", get_domain),
        (fulfilment.domains, "register_domain", register_domain),
        (fulfilment.domains, "list_records", list_records),
        (fulfilment.domains, "create_record", create_record),
        (fulfilment.deployments, "get_site_domains", get_site_domains),
        (fulfilment.deployments, "attach_domain", attach_domain),
        (fulfilment.deployments, "is_servable", is_servable),
        (fulfilment.deployments, "is_pointed_at_server", is_pointed_at_server),
        (fulfilment.deployments, "get_activation", get_activation),
        (fulfilment.deployments, "activate_domain", activate_domain),
        (fulfilment.agent_service, "get_current_conversation", get_current_conversation),
    ]:
        monkeypatch.setattr(target, name, fake)
    return state


def _active(world):
    return [row["domain"] for row in world["attached"] if row["activated_at"]]


async def test_a_paid_domain_is_registered_pointed_at_us_attached_and_set_up(fulfilment_world):
    # Attached at _NOW, long past: settled in public DNS by the time it's set up.
    await fulfilment.fulfil(42)
    assert fulfilment_world["registered"] == [("cozy.com", 1, "VN", "subscription-sub-1")]
    # Recorded against the checkout that bought it, with who it's registered to.
    assert fulfilment_world["registrations"] == [
        (2, "sub-1", {"domain": "cozy.com", "registrar": "namecom", "registrar_order_id": 7, "paid_usd": 9.99}, "VN")
    ]
    assert fulfilment_world["records"] == [{"host": "", "type": "A", "answer": "57.0.0.1", "ttl": 300}]
    assert fulfilment_world["set_up"] == ["cozy.com"] and _active(fulfilment_world) == ["cozy.com"]
    # Run again (every status poll does): the registrar isn't asked again, nothing's set up again.
    await fulfilment.fulfil(42)
    assert len(fulfilment_world["registered"]) == 1 and fulfilment_world["set_up"] == ["cozy.com"]


async def test_every_registrar_call_for_a_domain_is_in_the_world_its_payment_was(fulfilment_world):
    await fulfilment.fulfil(42)
    assert fulfilment_world["registrars"] == {"namecom"}  # live money: the live registrar


async def test_a_sandbox_payments_domain_lives_on_the_registrars_sandbox(fulfilment_world):
    sandbox_subscription = {**fulfilment_world["access"]["domain_subscription"], "provider": "polar_sandbox"}
    fulfilment_world["access"] = {**fulfilment_world["access"], "domain_subscription": sandbox_subscription}
    await fulfilment.fulfil(42)
    assert fulfilment_world["registrars"] == {"namecom_sandbox"}
    assert fulfilment_world["registrations"][0][2]["registrar"] == "namecom_sandbox"


async def test_a_domain_just_bought_is_set_up_once_public_dns_has_it(fulfilment_world):
    fulfilment_world["now"] = datetime.now(UTC)
    await fulfilment.fulfil(42)
    assert fulfilment_world["attached"][0]["domain"] == "cozy.com" and fulfilment_world["set_up"] == []
    fulfilment_world["points_at_us"] = True
    await fulfilment.fulfil(42)
    assert fulfilment_world["set_up"] == ["cozy.com"]


async def test_a_domain_just_bought_is_attempted_anyway_after_two_days(fulfilment_world):
    fulfilment_world["now"] = datetime.now(UTC)
    await fulfilment.fulfil(42)
    assert fulfilment_world["set_up"] == []
    fulfilment_world["attached"] = [
        {**row, "created_at": row["created_at"] - timedelta(days=2, seconds=1)} for row in fulfilment_world["attached"]
    ]
    await fulfilment.fulfil(42)
    assert fulfilment_world["set_up"] == ["cozy.com"]


async def test_a_domain_gone_by_payment_is_refunded_and_the_plan_stays(fulfilment_world):
    fulfilment_world["unavailable"] = True
    await fulfilment.fulfil(42)
    assert fulfilment_world["cancelled"] == ["sub-1"] and fulfilment_world["refunded"] == ["sub-1"]
    assert fulfilment_world["failures"] == [
        {"checkout_id": 2, "subscription_ref": "sub-1", "domain": "cozy.com", "reason": "unavailable"}
    ]
    assert fulfilment_world["attached"] == [] and fulfilment_world["set_up"] == []
    await fulfilment.fulfil(42)  # not retried
    assert fulfilment_world["cancelled"] == ["sub-1"]


async def test_a_plan_with_no_domain_yet_just_waits_for_one(fulfilment_world):
    # Right after the plan's paid, before any domain's bought or added.
    fulfilment_world.update(_own_domains(fulfilment_world))
    await fulfilment.fulfil(42)
    assert fulfilment_world["set_up"] == []


async def test_nothing_happens_without_hosting(fulfilment_world):
    fulfilment_world["access"] = {**fulfilment_world["access"], "can_host": False}
    await fulfilment.fulfil(42)
    assert fulfilment_world["registered"] == [] and fulfilment_world["set_up"] == []


def _own_domains(world, *rows):
    """[Pure] The world with no domain bought, and the user's own domains attached: (domain, minutes ago, active)."""
    return {
        **world,
        "access": {**world["access"], "domain_subscription": None},
        "attached": [
            {
                "domain": domain,
                "kind": "external",
                "activated_at": _NOW if active else None,
                "created_at": _NOW - timedelta(minutes=minutes_ago),
            }
            for domain, minutes_ago, active in rows
        ],
    }


async def test_with_no_active_domain_the_newest_one_is_attempted_records_ready_or_not(fulfilment_world):
    # A user's own domain gets its one attempt right after checkout; without its records, that attempt fails at once
    # (deployments' side) and the user retries.
    fulfilment_world.update(_own_domains(fulfilment_world, ("older.com", 20, False), ("newest.com", 5, False)))
    fulfilment_world["servable"] = {"older.com": True, "newest.com": False}
    await fulfilment.fulfil(42)
    assert fulfilment_world["set_up"] == ["newest.com"]


async def test_an_active_domain_is_never_switched_away_from_even_when_it_stops_being_servable(fulfilment_world):
    fulfilment_world.update(_own_domains(fulfilment_world, ("live.com", 20, True), ("spare.com", 5, False)))
    fulfilment_world["servable"] = {"live.com": False, "spare.com": True}
    await fulfilment.fulfil(42)
    assert fulfilment_world["set_up"] == [] and _active(fulfilment_world) == ["live.com"]


async def test_a_failed_attempt_is_never_retried_from_here(fulfilment_world):
    fulfilment_world.update(_own_domains(fulfilment_world, ("mine.com", 5, False)))
    failure = {"domain": "mine.com", "cause": "dns", "error": "records not found", "at": _NOW}
    fulfilment_world["activation"] = {"domain": None, "failure": failure}
    await fulfilment.fulfil(42)
    assert fulfilment_world["set_up"] == []


async def test_a_domain_attached_again_after_its_failure_gets_a_new_attempt(fulfilment_world):
    fulfilment_world.update(_own_domains(fulfilment_world, ("mine.com", 5, False)))
    failure = {"domain": "mine.com", "cause": "dns", "error": "records not found", "at": _NOW - timedelta(hours=1)}
    fulfilment_world["activation"] = {"domain": None, "failure": failure}
    await fulfilment.fulfil(42)
    assert fulfilment_world["set_up"] == ["mine.com"]


async def test_nothing_starts_while_a_domain_is_being_set_up(fulfilment_world):
    fulfilment_world.update(_own_domains(fulfilment_world, ("mine.com", 5, False)))
    fulfilment_world["activation"] = {"domain": "mine.com", "failure": None}
    await fulfilment.fulfil(42)
    assert fulfilment_world["set_up"] == []


async def test_the_sweep_fulfils_each_hosted_project_once(monkeypatch):
    plans = [
        {"target_type": "PROJECT", "target_id": 1, "billing": "recurring", "status": "active", "ends_at": None},
        {"target_type": "PROJECT", "target_id": 1, "billing": "recurring", "status": "ended", "ends_at": _NOW},
        {"target_type": "PROJECT", "target_id": 2, "billing": "recurring", "status": "cancelled", "ends_at": datetime.now(UTC) + timedelta(days=3)},
        {"target_type": "PROJECT", "target_id": 3, "billing": "recurring", "status": "ended", "ends_at": _NOW - timedelta(days=30)},
        # In its grace period: still hosted, so still swept.
        {"target_type": "PROJECT", "target_id": 4, "billing": "recurring", "status": "ended", "ends_at": datetime.now(UTC) - timedelta(days=2)},
    ]  # fmt: skip
    swept = {"ids": []}

    async def find_subscriptions(criteria):
        return plans

    async def fulfil(project_id):
        swept["ids"] = [*swept["ids"], project_id]

    monkeypatch.setattr(fulfilment.payments, "find_subscriptions", find_subscriptions)
    monkeypatch.setattr(fulfilment, "fulfil", fulfil)
    await fulfilment.fulfil_hosted_projects()
    assert swept["ids"] == [1, 2, 4]


# --- after launch: upgrading, switching ---


@pytest.fixture
def plan_world(monkeypatch):
    """The launch access and the plan change faked; records the plan changes. `refusal`: the status the provider
    turns a change down with (None: it goes through)."""
    state = {"plan_subscription": None, "changes": [], "refusal": None}

    async def get_launch_access(project_id):
        return {"plan_subscription": state["plan_subscription"]}

    async def change_plan(subscription, plan):
        if state["refusal"]:
            raise checkouts.payments.ProviderError("refused", state["refusal"])
        state["changes"] = [*state["changes"], (subscription["provider_ref"], plan)]

    monkeypatch.setattr(checkouts.launch_access, "get_launch_access", get_launch_access)
    monkeypatch.setattr(checkouts.payments, "change_plan", change_plan)
    return state


async def test_upgrading_moves_the_running_monthly_plan_to_yearly(plan_world):
    plan_world["plan_subscription"] = {"plan": "monthly", "status": "active", "provider_ref": "plan-1"}
    await checkouts.upgrade_plan(_PROJECT)
    assert plan_world["changes"] == [("plan-1", "yearly")]


async def test_an_upgrade_the_provider_turns_down_is_refused_but_its_outage_isnt(plan_world):
    plan_world["plan_subscription"] = {"plan": "monthly", "status": "active", "provider_ref": "plan-1"}
    plan_world["refusal"] = 402  # the payment failed
    with pytest.raises(checkouts.LaunchRefused, match="NOT_UPGRADABLE"):
        await checkouts.upgrade_plan(_PROJECT)
    plan_world["refusal"] = 503
    with pytest.raises(checkouts.payments.ProviderError):
        await checkouts.upgrade_plan(_PROJECT)


@pytest.mark.parametrize(
    "plan_subscription",
    [None, {"plan": "yearly", "status": "active"}, {"plan": "monthly", "status": "cancelled"}],
)
async def test_only_a_running_monthly_plan_can_be_upgraded(plan_world, plan_subscription):
    plan_world["plan_subscription"] = plan_subscription and {**plan_subscription, "provider_ref": "plan-1"}
    with pytest.raises(checkouts.LaunchRefused, match="NOT_UPGRADABLE"):
        await checkouts.upgrade_plan(_PROJECT)
    assert plan_world["changes"] == []


@pytest.fixture
def switch_world(monkeypatch):
    """A running plan, the attached domains (`servable` by name), and setting one up faked."""
    state = {"plan_subscription": {"plan": "monthly"}, "attached": [], "servable": {}, "set_up": [], "busy": False}

    async def get_launch_access(project_id):
        return {"plan_subscription": state["plan_subscription"]}

    async def get_site_domains(project):
        return state["attached"]

    async def is_servable(project, site_domain):
        return state["servable"].get(site_domain["domain"], True)

    async def get_current_conversation(project_id, user_id):
        return {"id": 9}

    async def activate_domain(project, conversation_id, domain):
        if state["busy"]:
            raise deployments.DeploymentInProgress("busy")
        state["set_up"] = [*state["set_up"], domain]

    monkeypatch.setattr(status.launch_access, "get_launch_access", get_launch_access)
    monkeypatch.setattr(status.deployments, "get_site_domains", get_site_domains)
    monkeypatch.setattr(status.deployments, "is_servable", is_servable)
    monkeypatch.setattr(status.deployments, "activate_domain", activate_domain)
    monkeypatch.setattr(status.agent_service, "get_current_conversation", get_current_conversation)
    return state


async def test_switching_sets_the_domain_up(switch_world):
    switch_world["attached"] = [
        {"domain": "old.com", "kind": "external", "activated_at": _NOW},
        {"domain": "new.com", "kind": "external", "activated_at": None},
    ]
    await status.activate_domain(_PROJECT, "New.com")
    assert switch_world["set_up"] == ["new.com"]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"plan_subscription": None}, "PLAN_REQUIRED"),
        ({"servable": {"new.com": False}}, "DOMAIN_NOT_VERIFIED"),
        ({"busy": True}, "BUSY"),
    ],
)
async def test_what_a_switch_needs(switch_world, changes, code):
    switch_world.update({"attached": [{"domain": "new.com", "kind": "external", "activated_at": None}], **changes})
    with pytest.raises(checkouts.LaunchRefused, match=code):
        await status.activate_domain(_PROJECT, "new.com")
    assert switch_world["set_up"] == []
