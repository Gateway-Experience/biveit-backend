# Launch flow tests (services/launch + the domain side of modules/deployments): which stage a launch is in, what a
# checkout may be (and when a domain is free for a year), checking a user's own domain, and fulfilment after a
# payment -- registering a bought domain, or refunding it when it's gone. Payments, the registrar, DNS and the
# tables are faked.

from datetime import UTC, datetime, timedelta

import pytest

from src.config import settings
from src.dto.launch import LaunchCheckoutRequest
from src.modules import deployments
from src.modules.deployments.services import sites
from src.modules.domains import DomainUnavailable, RegistrarError
from src.services.launch import access as launch_access
from src.services.launch import checkouts, fulfilment, intents, offers, status

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
        "access": {"can_download": True, "can_host": True},
        "one_time": None,
        "launch": None,
        "domain_subscription": None,
        "site_domains": [],
        "domain": None,
        "deployment": {"deploying": False, "last_deployment": None},
    }
    return {**base, **overrides}


_ACTIVE = {"domain": "cozy.com", "kind": "purchased", "activated_at": _NOW, "servable": True, "check": None}


@pytest.mark.parametrize(
    ("overrides", "stage"),
    [
        ({"access": {"can_download": False, "can_host": False}}, "not_paid"),
        ({"access": {"can_download": True, "can_host": False}, "one_time": {"paid_at": _NOW}}, "code_only"),
        ({"access": {"can_download": True, "can_host": False}}, "ended"),
        ({"launch": {"domain": {"kind": "buy", "name": "cozy.com"}, "domain_failure": None}}, "finish_domain"),
        ({"launch": {"domain": {"kind": "buy"}, "domain_failure": {"domain": "cozy.com"}}}, "choose_domain"),
        ({"domain_subscription": {"domain": "cozy.com"}}, "registering_domain"),
        ({}, "choose_domain"),
        (
            {"domain": {**_ACTIVE, "kind": "external", "servable": False}, "site_domains": [{"domain": "cozy.com"}]},
            "waiting_dns",
        ),
        ({"domain": _ACTIVE, "site_domains": [{"domain": "cozy.com"}]}, "deploying"),
        (
            {
                "domain": _ACTIVE,
                "deployment": {
                    "deploying": False,
                    "last_deployment": {"started_at": _NOW - timedelta(days=1), "outcome": "succeeded"},
                },
            },
            "deploying",  # the last one predates the domain: one's about to start
        ),
        (
            {
                "domain": _ACTIVE,
                "deployment": {"deploying": False, "last_deployment": {"started_at": _NOW, "outcome": "succeeded"}},
            },
            "live",
        ),
        (
            {
                "domain": _ACTIVE,
                "deployment": {"deploying": False, "last_deployment": {"started_at": _NOW, "outcome": "rolled_back"}},
            },
            "failed",
        ),
        (
            {
                "domain": _ACTIVE,
                "deployment": {"deploying": True, "last_deployment": {"started_at": _NOW, "outcome": "running"}},
            },
            "deploying",
        ),
    ],
)
def test_the_stage_says_where_the_launch_is(overrides, stage):
    assert status.to_stage(_status(**overrides)) == stage


# --- checkouts ---


@pytest.fixture
def launch_world(monkeypatch):
    """Payments, the registrar quote, the domain check and the intents log faked; records what was asked for."""
    state = {
        "access": {"can_download": False, "plan": None, "domain_subscription": None},
        "intents": {},
        "checkouts": [],
    }

    async def get_project_access(project_id):
        return state["access"]

    async def quote_domain(name):
        return state.get(
            "quote",
            {"domain": name, "available": True, "sold": True, "price_per_year": 15, "free_first_year_eligible": True},
        )

    async def check_external_domain(project, domain):
        return {"domain": domain, "blocked": state.get("blocked"), "records": [], "verified": False}

    async def record_intent(launch_id, user, project, intent):
        state["intents"] = {**state["intents"], launch_id: intent}

    async def get_intent(launch_id):
        return state["intents"].get(launch_id)

    async def get_domain_failures(project_id):
        return state.get("failures", [])

    async def create_plan_checkout(user, project, plan, extra):
        state["checkouts"] = [*state["checkouts"], ("plan", plan)]
        return f"https://pay/{plan}"

    async def create_domain_checkout(user, project, domain, price, free_first_year, extra):
        state["checkouts"] = [*state["checkouts"], ("domain", domain, price, free_first_year)]
        return f"https://pay/{domain}"

    monkeypatch.setattr(checkouts.payments, "get_project_access", get_project_access)
    monkeypatch.setattr(checkouts.payments, "create_plan_checkout", create_plan_checkout)
    monkeypatch.setattr(checkouts.payments, "create_domain_checkout", create_domain_checkout)
    monkeypatch.setattr(checkouts.offers, "quote_domain", quote_domain)
    monkeypatch.setattr(checkouts.deployments, "check_external_domain", check_external_domain)
    monkeypatch.setattr(checkouts.intents, "record_intent", record_intent)
    monkeypatch.setattr(checkouts.intents, "get_intent", get_intent)
    monkeypatch.setattr(checkouts.intents, "get_domain_failures", get_domain_failures)
    return state


async def test_a_quote_is_the_registrars_answer_with_our_price(monkeypatch):
    async def check_domains(query, tlds):
        return [{"domain": query, "available": True, "premium": False, "price_usd": 12, "renewal_price_usd": 14}]

    async def get_min_years(domain_name):
        return 1

    # monkeypatch refuses names the module doesn't have -- so this also checks the domains module exports them.
    monkeypatch.setattr(offers.domains, "check_domains", check_domains)
    monkeypatch.setattr(offers.domains, "get_min_years", get_min_years)
    quote = await offers.quote_domain("cozy.com")
    assert (quote["domain"], quote["available"], quote["sold"], quote["price_per_year"]) == ("cozy.com", True, True, 15)


def _request(**fields):
    return LaunchCheckoutRequest(**fields)


async def test_yearly_with_a_bought_domain_is_two_checkouts_the_domain_free(launch_world):
    result = await checkouts.start_checkout(
        _PROJECT,
        _USER,
        _request(
            plan="yearly", domain={"kind": "buy", "name": "Cozy.com", "price_per_year": 15}, registrant=_REGISTRANT
        ),
    )
    assert [checkout["product"] for checkout in result["checkouts"]] == ["plan", "domain"]
    assert launch_world["checkouts"] == [("plan", "yearly"), ("domain", "cozy.com", 15, True)]
    intent = launch_world["intents"][result["launch_id"]]
    assert intent["registrant"]["country"] == "VN" and intent["domain"]["free_first_year"]


async def test_monthly_pays_for_its_domain_and_a_later_domain_pays_too(launch_world):
    await checkouts.start_checkout(
        _PROJECT, _USER, _request(plan="monthly", domain={"kind": "buy", "name": "cozy.com"}, registrant=_REGISTRANT)
    )
    assert launch_world["checkouts"][-1] == ("domain", "cozy.com", 15, False)
    launch_world["access"] = {**launch_world["access"], "plan": "yearly", "can_download": True}
    await checkouts.start_checkout(
        _PROJECT, _USER, _request(domain={"kind": "buy", "name": "later.com"}, registrant=_REGISTRANT)
    )
    assert launch_world["checkouts"][-1] == ("domain", "later.com", 15, False)  # not in the yearly plan's launch


async def test_finishing_a_yearly_launch_keeps_the_free_year(launch_world):
    first = await checkouts.start_checkout(
        _PROJECT, _USER, _request(plan="yearly", domain={"kind": "buy", "name": "cozy.com"}, registrant=_REGISTRANT)
    )
    launch_world["access"] = {**launch_world["access"], "plan": "yearly", "can_download": True}
    await checkouts.start_checkout(
        _PROJECT,
        _USER,
        _request(domain={"kind": "buy", "name": "other.com"}, registrant=_REGISTRANT, launch_id=first["launch_id"]),
    )
    assert launch_world["checkouts"][-1] == ("domain", "other.com", 15, True)
    assert [checkout[0] for checkout in launch_world["checkouts"]].count("plan") == 1


async def test_after_a_bought_domain_fails_another_can_be_bought_keeping_the_free_year(launch_world):
    first = await checkouts.start_checkout(
        _PROJECT, _USER, _request(plan="yearly", domain={"kind": "buy", "name": "a.com"}, registrant=_REGISTRANT)
    )
    # a.com was paid for, then couldn't be registered: its subscription is cancelled (but still "running" until
    # its period ends) and the failure recorded.
    launch_world["access"] = {
        "can_download": True, "can_host": True, "plan": "yearly",
        "plan_subscription": {"custom_data": {"launch_id": first["launch_id"]}},
        "domain_subscription": {"provider_ref": "sub-a", "status": "cancelled", "product_data": {"domain": "a.com"}},
    }  # fmt: skip
    launch_world["failures"] = [{"subscription_ref": "sub-a", "launch_id": first["launch_id"], "domain": "a.com"}]
    await checkouts.start_checkout(
        _PROJECT,
        _USER,
        _request(domain={"kind": "buy", "name": "b.com"}, registrant=_REGISTRANT, launch_id=first["launch_id"]),
    )
    assert launch_world["checkouts"][-1] == ("domain", "b.com", 15, True)


async def test_the_launchs_access_drops_a_domain_subscription_that_failed(monkeypatch):
    subscription = {"provider_ref": "sub-a", "status": "cancelled"}

    async def get_project_access(project_id):
        return {"can_host": True, "domain_subscription": subscription}

    async def get_domain_failures(project_id):
        return [{"subscription_ref": "sub-a", "launch_id": "l1", "domain": "a.com"}] if project_id == 42 else []

    monkeypatch.setattr(launch_access.payments, "get_project_access", get_project_access)
    monkeypatch.setattr(launch_access.intents, "get_domain_failures", get_domain_failures)
    failed = await launch_access.get_launch_access(42)
    assert failed["domain_subscription"] is None and failed["domain_failures"][0]["domain"] == "a.com"
    assert (await launch_access.get_launch_access(7))["domain_subscription"] == subscription


def test_a_failed_domain_with_the_plan_running_asks_for_another_domain():
    launch = {"domain": {"kind": "buy", "name": "a.com"}, "domain_failure": {"domain": "a.com"}}
    assert status.to_stage(_status(launch=launch, domain_subscription=None)) == "choose_domain"


@pytest.mark.parametrize(
    ("access", "fields", "code"),
    [
        ({"plan": "monthly"}, {"plan": "yearly", "domain": {"kind": "own", "name": "cozy.com"}}, "ALREADY_SUBSCRIBED"),
        ({"can_download": True}, {"plan": "one_time"}, "ALREADY_PURCHASED"),
        ({}, {"plan": "monthly"}, "DOMAIN_REQUIRED"),
        ({}, {"domain": {"kind": "own", "name": "cozy.com"}}, "PLAN_REQUIRED"),
        (
            {"plan": "yearly", "domain_subscription": {"domain": "a.com"}},
            {"domain": {"kind": "buy", "name": "b.com"}},
            "DOMAIN_ALREADY_BOUGHT",
        ),
    ],
)
async def test_what_isnt_allowed_now_is_refused(launch_world, access, fields, code):
    launch_world["access"] = {**launch_world["access"], **access}
    registrant = {"registrant": _REGISTRANT} if fields.get("domain", {}).get("kind") == "buy" else {}
    with pytest.raises(checkouts.LaunchRefused) as refused:
        await checkouts.start_checkout(_PROJECT, _USER, _request(**fields, **registrant))
    assert refused.value.code == code and launch_world["checkouts"] == []


async def test_a_domain_that_changed_since_the_dialog_is_refused(launch_world):
    launch_world["quote"] = {
        "domain": "cozy.com",
        "available": True,
        "sold": True,
        "price_per_year": 20,
        "free_first_year_eligible": True,
    }
    with pytest.raises(checkouts.LaunchRefused) as refused:
        await checkouts.start_checkout(
            _PROJECT,
            _USER,
            _request(
                plan="yearly", domain={"kind": "buy", "name": "cozy.com", "price_per_year": 15}, registrant=_REGISTRANT
            ),
        )
    assert refused.value.code == "PRICE_CHANGED" and refused.value.details == {"price_per_year": 20}
    launch_world["quote"] = {"domain": "cozy.com", "available": False, "sold": False, "price_per_year": None}
    with pytest.raises(checkouts.LaunchRefused, match="DOMAIN_UNAVAILABLE"):
        await checkouts.start_checkout(
            _PROJECT, _USER, _request(plan="yearly", domain={"kind": "buy", "name": "cozy.com"}, registrant=_REGISTRANT)
        )


async def test_a_users_own_domain_another_project_has_is_refused(launch_world):
    launch_world["blocked"] = "taken"
    with pytest.raises(checkouts.LaunchRefused, match="DOMAIN_TAKEN"):
        await checkouts.start_checkout(
            _PROJECT, _USER, _request(plan="monthly", domain={"kind": "own", "name": "cozy.com"})
        )


async def test_an_unapproved_site_cant_launch(launch_world):
    with pytest.raises(checkouts.LaunchRefused, match="NOT_APPROVED"):
        await checkouts.start_checkout({**_PROJECT, "build_approved_at": None}, _USER, _request(plan="one_time"))


@pytest.mark.parametrize(
    ("attached", "verified", "activates"),
    [
        ([], False, True),  # nothing served yet: it's the site's domain from now on
        ([{"domain": "live.com", "activated_at": _NOW}], False, False),  # a live site stays where it is
        ([{"domain": "live.com", "activated_at": _NOW}], True, True),  # ready to serve: switch now
    ],
)
async def test_an_own_domain_on_a_running_plan_only_goes_active_when_it_can_be_served(
    launch_world, monkeypatch, attached, verified, activates
):
    launch_world["access"] = {**launch_world["access"], "can_download": True, "plan": "monthly", "can_host": True}
    attaches = {"calls": []}

    async def check_external_domain(project, domain):
        return {"domain": domain, "blocked": None, "records": [], "verified": verified}

    async def get_site_domains(project):
        return attached

    async def attach_domain(project, domain, kind, activate):
        attaches["calls"] = [*attaches["calls"], (domain, kind, activate)]

    async def fulfil(project_id):
        return None

    monkeypatch.setattr(checkouts.deployments, "check_external_domain", check_external_domain)
    monkeypatch.setattr(checkouts.deployments, "get_site_domains", get_site_domains)
    monkeypatch.setattr(checkouts.deployments, "attach_domain", attach_domain)
    monkeypatch.setattr(checkouts.fulfilment, "fulfil", fulfil)
    result = await checkouts.start_checkout(_PROJECT, _USER, _request(domain={"kind": "own", "name": "mine.com"}))
    assert result["checkouts"] == []
    assert attaches["calls"] == [("mine.com", "external", activates)]


def test_buying_a_domain_needs_its_registrant():
    with pytest.raises(ValueError, match="registrant"):
        _request(plan="yearly", domain={"kind": "buy", "name": "cozy.com"})


# --- a user's own domain ---


@pytest.fixture
def dns(monkeypatch):
    """DNS answers and the attached domains faked."""
    state = {"answers": {}, "owners": []}

    async def resolve(name, record_type):
        return state["answers"].get((name, record_type), set())

    async def find(criteria):
        return [row for row in state["owners"] if row["domain"] == criteria["domain"]]

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
    """Everything fulfilment touches, faked: payments, the registrar, attached domains, deployments."""
    subscription = {"product_data": {"domain": "cozy.com"}, "provider_ref": "sub-1", "custom_data": {"launch_id": "l1"}}
    state = {
        "access": {"can_host": True, "domain_subscription": subscription, "plan_subscription": {"custom_data": {"launch_id": "l1"}}},
        "attached": [], "registered": [], "records": [], "refunded": [], "cancelled": [], "failures": [], "deployed": [],
        "registrar_has": set(), "unavailable": False, "dns_ready": True, "deployments": [], "live": [],
        "intent_domain": {"kind": "buy", "name": "cozy.com"}, "now": _NOW,
    }  # fmt: skip

    async def get_project_access(project_id):
        return state["access"]

    async def find_projects(criteria):
        return [_PROJECT]

    async def set_if_absent(key, ttl):
        return True

    async def delete(key):
        pass

    async def get_intent(launch_id):
        return {
            "plan": "yearly",
            "domain": state["intent_domain"],
            "registrant": {**_REGISTRANT, "country": "VN"},
        }

    async def get_domain_failures(project_id):
        return state["failures"]

    async def record_domain_failure(launch_id, project_id, subscription_ref, domain, reason):
        state["failures"] = [
            *state["failures"],
            {"subscription_ref": subscription_ref, "domain": domain, "reason": reason},
        ]

    async def get_domain(domain):
        if domain not in state["registrar_has"]:
            raise RegistrarError("namecom", 404, "not found")
        return {"domainName": domain}

    async def register_domain(domain, years, registrant, key):
        if state["unavailable"]:
            raise DomainUnavailable(domain)
        state["registered"] = [*state["registered"], (domain, years, registrant["country"], key)]

    async def list_records(domain):
        return state["records"]

    async def create_record(domain, record):
        state["records"] = [*state["records"], record]

    async def get_site_domains(project):
        return state["attached"]

    async def attach_domain(project, domain, kind, activate):
        # Like the real one: kept if it's there; activating it clears the others.
        known = next((row for row in state["attached"] if row["domain"] == domain), None)
        attached = state["attached"] if known else [
            *state["attached"],
            {"domain": domain, "kind": kind, "activated_at": None, "created_at": state["now"]},
        ]  # fmt: skip
        state["attached"] = [
            {**row, "activated_at": (state["now"] if row["domain"] == domain else None)} if activate else row
            for row in attached
        ]

    async def check_external_domain(project, domain):
        return {"domain": domain, "blocked": None, "records": [], "verified": state["dns_ready"]}

    async def get_served_domains(project):
        return [
            row["domain"]
            for row in state["attached"]
            if row["activated_at"] and (row["kind"] != "external" or state["dns_ready"])
        ]

    async def list_deployments(project):
        return state["deployments"]

    async def get_current_conversation(project_id, user_id):
        return {"id": 9}

    async def deploy(project, conversation_id, enforce_cooldown=True):
        # Like the real one, done at once: it serves what may be served now.
        state["deployed"] = [*state["deployed"], enforce_cooldown]
        state["deployments"] = [{"started_at": state["now"], "outcome": "succeeded"}, *state["deployments"]]
        state["live"] = await get_served_domains(project)

    async def cancel_subscription(ref):
        state["cancelled"] = [*state["cancelled"], ref]

    async def refund_latest_payment(ref):
        state["refunded"] = [*state["refunded"], ref]

    monkeypatch.setattr(settings, "server_public_ip", "57.0.0.1")
    for target, name, fake in [
        (fulfilment.payments, "get_project_access", get_project_access),
        (fulfilment.payments, "cancel_subscription", cancel_subscription),
        (fulfilment.payments, "refund_latest_payment", refund_latest_payment),
        (fulfilment.projects_repo, "find", find_projects),
        (fulfilment.redis, "set_if_absent", set_if_absent),
        (fulfilment.redis, "delete", delete),
        (intents, "get_intent", get_intent),
        (intents, "get_domain_failures", get_domain_failures),
        (intents, "record_domain_failure", record_domain_failure),
        (fulfilment.domains, "get_domain", get_domain),
        (fulfilment.domains, "register_domain", register_domain),
        (fulfilment.domains, "list_records", list_records),
        (fulfilment.domains, "create_record", create_record),
        (fulfilment.deployments, "get_site_domains", get_site_domains),
        (fulfilment.deployments, "attach_domain", attach_domain),
        (fulfilment.deployments, "check_external_domain", check_external_domain),
        (fulfilment.deployments, "get_served_domains", get_served_domains),
        (fulfilment.deployments, "list_deployments", list_deployments),
        (fulfilment.deployments, "deploy", deploy),
        (fulfilment.agent_service, "get_current_conversation", get_current_conversation),
    ]:
        monkeypatch.setattr(target, name, fake)
    return state


async def test_a_paid_domain_is_registered_pointed_at_us_attached_and_deployed(fulfilment_world):
    await fulfilment.fulfil(42)
    assert fulfilment_world["registered"] == [("cozy.com", 1, "VN", "subscription-sub-1")]
    assert fulfilment_world["records"] == [{"host": "", "type": "A", "answer": "57.0.0.1", "ttl": 300}]
    assert fulfilment_world["attached"][0]["domain"] == "cozy.com" and fulfilment_world["attached"][0]["activated_at"]
    assert fulfilment_world["deployed"] == [False]  # the platform's own deploy: no cooldown
    # Run again (every status poll does): the registrar isn't asked again.
    await fulfilment.fulfil(42)
    assert len(fulfilment_world["registered"]) == 1 and fulfilment_world["deployed"] == [False]


async def test_a_domain_gone_by_payment_is_refunded_and_the_plan_stays(fulfilment_world):
    fulfilment_world["unavailable"] = True
    await fulfilment.fulfil(42)
    assert fulfilment_world["cancelled"] == ["sub-1"] and fulfilment_world["refunded"] == ["sub-1"]
    assert fulfilment_world["failures"][0]["reason"] == "unavailable"
    assert fulfilment_world["attached"] == [] and fulfilment_world["deployed"] == []
    await fulfilment.fulfil(42)  # not retried
    assert fulfilment_world["cancelled"] == ["sub-1"]


async def test_nothing_happens_without_hosting(fulfilment_world):
    fulfilment_world["access"] = {**fulfilment_world["access"], "can_host": False}
    await fulfilment.fulfil(42)
    assert fulfilment_world["registered"] == [] and fulfilment_world["deployed"] == []


async def test_a_launchs_own_domain_takes_over_from_an_older_one_once_its_dns_is_there(fulfilment_world):
    world = fulfilment_world
    world["access"] = {**world["access"], "domain_subscription": None}
    world["intent_domain"] = {"kind": "own", "name": "mine.com"}
    world["attached"] = [
        {"domain": "old.com", "kind": "admin", "activated_at": _NOW - timedelta(days=1), "created_at": _NOW - timedelta(days=1)},
    ]  # fmt: skip
    world["dns_ready"] = False
    await fulfilment.fulfil(42)
    assert [(row["domain"], bool(row["activated_at"])) for row in world["attached"]] == [
        ("old.com", True),
        ("mine.com", False),
    ]
    assert world["live"] == ["old.com"]  # still served where it works
    world["dns_ready"], world["now"] = True, _NOW + timedelta(hours=1)
    await fulfilment.fulfil(42)
    assert [(row["domain"], bool(row["activated_at"])) for row in world["attached"]] == [
        ("old.com", False),
        ("mine.com", True),
    ]
    assert world["live"] == ["mine.com"]


async def test_a_launchs_own_domain_doesnt_undo_a_later_switch(fulfilment_world):
    world = fulfilment_world
    world["access"] = {**world["access"], "domain_subscription": None}
    world["intent_domain"] = {"kind": "own", "name": "mine.com"}
    world["attached"] = [
        {"domain": "mine.com", "kind": "external", "activated_at": None, "created_at": _NOW - timedelta(days=2)},
        {"domain": "other.com", "kind": "external", "activated_at": _NOW - timedelta(days=1), "created_at": _NOW - timedelta(days=1)},
    ]  # fmt: skip
    await fulfilment.fulfil(42)
    assert [row["domain"] for row in world["attached"] if row["activated_at"]] == ["other.com"]


async def test_the_sweep_fulfils_each_project_with_a_running_plan_once(monkeypatch):
    plans = [
        {"target_type": "PROJECT", "target_id": 1, "status": "active", "ends_at": None},
        {"target_type": "PROJECT", "target_id": 1, "status": "expired", "ends_at": _NOW},
        {"target_type": "PROJECT", "target_id": 2, "status": "cancelled", "ends_at": datetime.now(UTC) + timedelta(days=3)},
        {"target_type": "PROJECT", "target_id": 3, "status": "expired", "ends_at": _NOW - timedelta(days=30)},
    ]  # fmt: skip
    swept = {"ids": []}

    async def find_subscriptions(criteria):
        return plans

    async def fulfil(project_id):
        swept["ids"] = [*swept["ids"], project_id]

    monkeypatch.setattr(fulfilment.payments, "find_subscriptions", find_subscriptions)
    monkeypatch.setattr(fulfilment, "fulfil", fulfil)
    await fulfilment.fulfil_running_plans()
    assert swept["ids"] == [1, 2]


async def test_a_mock_payment_never_buys_a_real_domain(fulfilment_world, monkeypatch):
    monkeypatch.setattr(fulfilment.settings, "payments_mock", True)
    monkeypatch.setattr(fulfilment.domains, "is_live", lambda: True)
    monkeypatch.setattr(fulfilment, "log_error", lambda *args, **kwargs: None)
    await fulfilment.fulfil(42)
    assert fulfilment_world["registered"] == [] and fulfilment_world["refunded"] == []
    assert fulfilment_world["attached"] == [] and fulfilment_world["deployed"] == []
