# Payments tests (modules/payments): what we charge for a domain, what a project's purchases allow (access), who pays
# where (live / sandbox), and taking in Polar's webhooks -- signature, our rows following what Polar says. The tables
# and Polar are faked (an in-memory Polar holding its objects); webhooks are really signed.

import base64
import hashlib
import hmac
import itertools
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import settings
from src.modules import payments
from src.modules.payments.services import catalog, checkouts, polar, purchases, subscriptions
from src.utils.auth import get_current_user

_NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


# --- domain prices ---


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        # Priced off 60% first term + 40% renewal: a promo first year lowers it some, not all the way.
        ({"price_usd": 8.9, "renewal_price_usd": 12.9}, {"price_per_year": 15, "discountable": True}),  # 10.50
        ({"price_usd": 2.0, "renewal_price_usd": 35.0}, {"price_per_year": 20, "discountable": True}),  # 15.20
        ({"price_usd": 2.99, "renewal_price_usd": 49.99}, {"price_per_year": 30, "discountable": False}),  # 21.79
        # A renewal of $100 or more counts alone, however cheap the first year.
        ({"price_usd": 5.0, "renewal_price_usd": 100.0}, {"price_per_year": None, "sold": False}),
        ({"price_usd": 30.0}, {"price_per_year": 30}),  # no renewal price: the first term counts for both
        ({"price_usd": 10.0, "renewal_price_usd": 10.0}, {"price_per_year": 10, "discountable": True}),
        ({"price_usd": 41.0, "renewal_price_usd": 41.0}, {"price_per_year": None, "sold": False}),
        ({"price_usd": 12.0, "renewal_price_usd": 12.0, "premium": True}, {"sold": False}),
        ({"price_usd": 140.0, "renewal_price_usd": 70.0, "min_years": 2}, {"sold": False}),
        ({"available": False}, {"sold": False, "price_per_year": None}),
    ],
)
def test_a_domain_is_priced_at_the_first_price_point_covering_it(quote, expected):
    offer = payments.to_domain_offer({"available": True, "min_years": 1, "premium": False, **quote})
    assert offer == {**offer, **expected}
    assert offer["sold"] == (offer["price_per_year"] is not None)


def test_every_product_we_sell_has_a_live_product_id_and_reads_back():
    live = payments.Provider.POLAR
    for point in payments.DOMAIN_PRICE_POINTS:
        product_id = catalog.get_product_id(live, point["product_key"])
        assert payments.describe_product(live, product_id) == {
            "product": "domain",
            "price_per_year": point["price_usd"],
        }
    yearly_id = catalog.get_product_id(live, payments.PLANS[payments.Plan.YEARLY]["product_key"])
    assert payments.describe_product(live, yearly_id) == {"product": "plan", "plan": "yearly"}
    assert payments.describe_product(live, "not-ours") is None
    # A live product id means nothing in the sandbox: separate worlds.
    assert payments.describe_product(payments.Provider.POLAR_SANDBOX, yearly_id) is None


# --- access ---


_IDS = itertools.count(1)


def _subscription(product="plan", status="active", ends_at=None, updated_at=_NOW, **fields):
    return {
        "id": next(_IDS),
        "created_at": _NOW - timedelta(days=60),
        "billing": "recurring",
        "product": product,
        "status": status,
        "ends_at": ends_at,
        "updated_at": updated_at,
        "plan": "monthly",
        **fields,
    }


def _one_time(created_at, status="active"):
    return {
        "id": next(_IDS),
        "billing": "lifetime",
        "product": "one_time",
        "plan": "one_time",
        "status": status,
        "created_at": created_at,
    }


def test_nothing_bought_allows_nothing():
    access = payments.to_access([], _NOW)
    assert (
        not (access["can_download"] or access["can_edit"] or access["can_host"]) and access["plan_subscription"] is None
    )


def test_a_running_plan_allows_everything():
    access = payments.to_access([_subscription(status="past_due")], _NOW)
    assert (
        access["can_download"]
        and access["can_edit"]
        and access["can_host"]
        and access["plan_subscription"]["plan"] == "monthly"
    )


def test_a_one_time_purchase_gives_the_code_and_30_days_of_edits_but_no_hosting():
    fresh = payments.to_access([_one_time(_NOW - timedelta(days=10))], _NOW)
    assert fresh["can_download"] and fresh["can_edit"] and not fresh["can_host"]
    assert fresh["edit_until"] == _NOW + timedelta(days=20)
    old = payments.to_access([_one_time(_NOW - timedelta(days=31))], _NOW)
    assert old["can_download"] and not old["can_edit"]
    refunded = payments.to_access([_one_time(_NOW, status="refunded")], _NOW)
    assert not refunded["can_download"]


def test_a_cancelled_plan_runs_to_its_end_then_has_a_grace_period():
    running = _subscription(status="cancelled", ends_at=_NOW + timedelta(days=3))
    assert payments.to_access([running], _NOW)["can_edit"]
    ended = _subscription(status="ended", ends_at=_NOW - timedelta(days=2))
    in_grace = payments.to_access([ended], _NOW)
    assert (
        in_grace["can_host"]
        and in_grace["can_edit"]
        and in_grace["plan_subscription"] is None
        and in_grace["can_download"]
    )
    long_gone = _subscription(status="ended", ends_at=_NOW - timedelta(days=8))
    gone = payments.to_access([long_gone], _NOW)
    assert not (gone["can_host"] or gone["can_edit"]) and gone["can_download"]


def test_the_latest_running_plan_wins_whatever_order_the_rows_come_in():
    older = _subscription(
        plan="monthly", status="cancelled", ends_at=_NOW + timedelta(days=3), created_at=_NOW - timedelta(days=90)
    )
    newer = _subscription(plan="yearly", created_at=_NOW - timedelta(days=1))
    assert payments.to_access([newer, older], _NOW)["plan_subscription"]["plan"] == "yearly"
    assert payments.to_access([older, newer], _NOW)["plan_subscription"]["plan"] == "yearly"


def test_a_plan_hosts_while_it_runs_and_through_its_grace_period():
    assert payments.to_access([_subscription(status="past_due")], _NOW)["can_host"]
    assert not payments.is_in_grace(_subscription(status="past_due"), _NOW)  # still running: not grace
    assert payments.is_in_grace(_subscription(status="ended", ends_at=_NOW - timedelta(days=6)), _NOW)
    assert not payments.is_in_grace(_subscription(status="ended", ends_at=_NOW - timedelta(days=8)), _NOW)
    # Payments failed for good: no ends_at, so it stopped when the provider last changed it.
    assert payments.is_in_grace(_subscription(status="ended", updated_at=_NOW - timedelta(days=1)), _NOW)
    # Never in effect, or the money went back: no grace.
    assert not payments.is_in_grace(_subscription(status="pending", updated_at=_NOW - timedelta(days=1)), _NOW)
    assert not payments.is_in_grace(_subscription(status="refunded", updated_at=_NOW - timedelta(days=1)), _NOW)


def test_the_running_domain_subscription_is_picked_out():
    domain = _subscription(product="domain", status="trialing", product_data={"domain": "cozy.com"})
    old_domain = _subscription(
        product="domain", status="ended", ends_at=_NOW - timedelta(days=40), product_data={"domain": "old.com"}
    )
    access = payments.to_access([old_domain, _subscription(), domain], _NOW)
    assert access["domain_subscription"]["product_data"]["domain"] == "cozy.com"


@pytest.mark.parametrize(
    ("polar_subscription", "status"),
    [
        ({"status": "trialing"}, "trialing"),
        ({"status": "active", "cancel_at_period_end": False}, "active"),
        ({"status": "active", "cancel_at_period_end": True}, "cancelled"),  # runs out its period
        ({"status": "past_due"}, "past_due"),
        ({"status": "incomplete"}, "pending"),
        ({"status": "canceled"}, "ended"),
        ({"status": "unpaid"}, "ended"),
        ({"status": "paused"}, "ended"),
    ],
)
def test_polars_subscription_statuses_are_read_as_ours(polar_subscription, status):
    assert polar.to_subscription_status(polar_subscription) == status


@pytest.mark.parametrize(
    ("order_status", "status"),
    [("paid", "active"), ("partially_refunded", "active"), ("refunded", "refunded"), ("pending", "pending")],
)
def test_polars_order_statuses_are_read_as_ours(order_status, status):
    assert polar.to_order_status({"status": order_status}) == status


def test_a_status_we_dont_know_fails_loudly():
    with pytest.raises(ValueError):
        polar.to_subscription_status({"status": "on_vacation"})


# --- who pays where ---


@pytest.mark.parametrize(
    ("server_sandbox", "user", "provider"),
    [
        # No choice of their own: the server's default.
        (False, {"role": None, "meta": {}}, "polar"),
        (False, {"role": "ADMIN", "meta": {}}, "polar"),
        (True, {"role": None, "meta": {}}, "polar_sandbox"),
        (True, {"role": "ADMIN", "meta": {"sandbox_mode": None}}, "polar_sandbox"),  # reset to the default
        # An admin's own choice wins, either way.
        (False, {"role": "ADMIN", "meta": {"sandbox_mode": True}}, "polar_sandbox"),
        (True, {"role": "ADMIN", "meta": {"sandbox_mode": False}}, "polar"),
        # A user can write their own meta: the flag means nothing without the role.
        (False, {"role": None, "meta": {"sandbox_mode": True}}, "polar"),
        (True, {"role": None, "meta": {"sandbox_mode": False}}, "polar_sandbox"),
    ],
)
def test_an_admin_picks_their_own_mode_and_everyone_else_gets_the_servers(monkeypatch, server_sandbox, user, provider):
    monkeypatch.setattr(settings, "sandbox_mode", server_sandbox)
    assert payments.get_payment_provider(user) == provider
    assert payments.is_sandbox(user) == (provider == "polar_sandbox")


# --- webhooks ---

_SECRET = "whsec_" + base64.b64encode(b"test-signing-key").decode()
_SANDBOX = payments.Provider.POLAR_SANDBOX
_YEARLY_ID, _MONTHLY_ID, _ONE_TIME_ID, _DOMAIN_15_ID = "prod-yearly", "prod-monthly", "prod-one-time", "prod-domain-15"


def _sign(body: dict, secret: str = _SECRET, timestamp: int | None = None) -> tuple[bytes, dict]:
    """A webhook as Polar would send it: (raw body, lower-cased headers), signed per Standard Webhooks."""
    raw_body = json.dumps(body).encode()
    webhook_id, sent_at = "msg_1", str(timestamp if timestamp is not None else int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signature = base64.b64encode(hmac.new(key, f"{webhook_id}.{sent_at}.".encode() + raw_body, hashlib.sha256).digest())
    return raw_body, {
        "webhook-id": webhook_id,
        "webhook-timestamp": sent_at,
        "webhook-signature": f"v1,{signature.decode()}",
    }


@pytest.fixture
def stores(monkeypatch):
    """The subscriptions table, the events log, and a sandbox Polar (its objects, and what was asked of it), all in
    memory. `pay(checkout)` makes what paying would: a subscription (+ its first order), or an order."""
    state = {"subscriptions": {}, "events": [], "polar": {}, "checkouts": [], "calls": []}

    async def find(criteria, include_deleted=False):
        return [row for row in state["subscriptions"].values() if all(row.get(k) == v for k, v in criteria.items())]

    async def upsert(row):
        key = (row["provider"], row["provider_object"], row["provider_ref"])
        known = state["subscriptions"].get(key, {})
        # Like the SQL: custom_data kept once it's there.
        created = {"id": len(state["subscriptions"]) + 1, "created_at": _NOW}
        merged = {
            **created, **known, **row, "custom_data": known.get("custom_data") or row.get("custom_data"),
            "updated_at": datetime.now(UTC),  # like the SQL: every write stamps it
        }  # fmt: skip
        state["subscriptions"] = {**state["subscriptions"], key: merged}
        return merged

    async def find_stale(statuses, updated_before, providers):
        rows = state["subscriptions"].values()
        return [
            row
            for row in rows
            if row["status"] in statuses and row["updated_at"] < updated_before and row["provider"] in providers
        ]

    async def find_by_checkout_ids(checkout_ids):
        rows = state["subscriptions"].values()
        return [row for row in rows if str(row["custom_data"].get("checkout_id")) in checkout_ids]

    async def find_events(criteria, created_after=None):
        return [event for event in state["events"] if event["type"] == criteria["type"]]

    async def find_checkout_orders(provider, checkout_id):
        objects = state["polar"].values()
        return [
            obj for obj in objects if obj["id"].startswith("ord") and obj["metadata"].get("checkout_id") == checkout_id
        ]

    async def insert_event(event):
        row = {**event, "id": len(state["events"]) + 1, "created_at": _NOW}
        state["events"] = [*state["events"], row]
        return row

    async def get_event(event_id):
        return next((event for event in state["events"] if event["id"] == event_id), None)

    async def create_checkout(provider, product_id, checkout_data):
        checkout = {"id": f"co-{len(state['checkouts']) + 1}", "url": "https://polar.test/checkout", "provider": provider,
                    "product_id": product_id, **checkout_data}  # fmt: skip
        state["checkouts"] = [*state["checkouts"], checkout]
        return checkout

    async def get_object(provider, object_ref):
        return state["polar"][object_ref]

    async def find_subscription_orders(provider, subscription_ref):
        orders = [obj for obj in state["polar"].values() if obj.get("subscription_id") == subscription_ref]
        return sorted(orders, key=lambda order: order["created_at"], reverse=True)

    async def record_call(*args):
        state["calls"] = [*state["calls"], args]

    products = {
        "plan-yearly": _YEARLY_ID,
        "plan-monthly": _MONTHLY_ID,
        "one-time": _ONE_TIME_ID,
        "domain-15": _DOMAIN_15_ID,
    }
    monkeypatch.setattr(settings, "frontend_url", "https://app.test")
    monkeypatch.setattr(settings, "sandbox_mode", True)
    monkeypatch.setattr(settings, "polar_sandbox_webhook_secret", _SECRET)
    monkeypatch.setitem(payments.constants.PRODUCT_IDS, _SANDBOX, products)
    monkeypatch.setattr(purchases.subscriptions_repo, "find", find)
    monkeypatch.setattr(purchases.subscriptions_repo, "upsert", upsert)
    monkeypatch.setattr(checkouts.events_repo, "insert", insert_event)
    monkeypatch.setattr(checkouts.events_repo, "get", get_event)
    monkeypatch.setattr(checkouts.events_repo, "find", find_events)
    monkeypatch.setattr(purchases.subscriptions_repo, "find_stale", find_stale)
    monkeypatch.setattr(purchases.subscriptions_repo, "find_by_checkout_ids", find_by_checkout_ids)
    monkeypatch.setattr(polar, "find_checkout_orders", find_checkout_orders)
    monkeypatch.setattr(polar, "create_checkout", create_checkout)
    monkeypatch.setattr(polar, "get_subscription", get_object)
    monkeypatch.setattr(polar, "get_order", get_object)
    monkeypatch.setattr(polar, "find_subscription_orders", find_subscription_orders)
    for name in ("change_product", "revoke_subscription", "refund_order"):
        monkeypatch.setattr(polar, name, lambda *args, name=name: record_call(name, *args))
    return state


def _pay(stores, checkout: dict) -> list[dict]:
    """[Test helper] What Polar makes when a checkout's paid, saved in the fake Polar; returns the webhooks' bodies it
    would send. A recurring product: a subscription (a trial when the checkout asked) and its first order; a one-time
    one: an order."""
    metadata, product_id = checkout["metadata"], checkout["product_id"]
    if product_id == _ONE_TIME_ID:
        order = {"id": "ord-1", "status": "paid", "paid": True, "product_id": product_id, "subscription_id": None,
                 "billing_reason": "purchase", "metadata": metadata, "created_at": "2026-10-28T12:00:00Z"}  # fmt: skip
        stores["polar"] = {**stores["polar"], "ord-1": order}
        return [{"type": "order.paid", "data": order}]
    trial = checkout.get("trial_interval") == "year"
    subscription = {
        "id": "sub-1", "status": "trialing" if trial else "active", "cancel_at_period_end": False,
        "product_id": product_id, "metadata": metadata, "current_period_end": "2027-10-28T12:00:00Z",
        "trial_end": "2027-10-28T12:00:00Z" if trial else None, "ends_at": None, "ended_at": None,
    }  # fmt: skip
    order = {"id": "ord-1", "status": "paid", "paid": not trial, "refundable_amount": 0 if trial else 15900,
             "product_id": product_id, "subscription_id": "sub-1", "billing_reason": "subscription_create",
             "metadata": metadata, "created_at": "2026-10-28T12:00:00Z"}  # fmt: skip
    stores["polar"] = {**stores["polar"], "sub-1": subscription, "ord-1": order}
    return [{"type": "subscription.created", "data": subscription}, {"type": "order.paid", "data": order}]


async def _deliver(body: dict) -> dict | None:
    return await payments.handle_webhook(_SANDBOX, *_sign(body))


_USER = {"id": 7, "username": "anh@example.com", "role": None, "meta": {}}
_PROJECT = {"id": 42}


async def test_a_paid_yearly_plan_becomes_our_subscription_row_pointing_at_its_checkout(stores):
    checkout = await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.YEARLY)
    assert checkout["url"] == "https://polar.test/checkout"
    # The checkout is recorded first, with what it's for and where; our ids ride along in Polar's metadata.
    assert stores["events"][0]["type"] == "payment.checkout_created"
    recorded = await payments.get_recorded_checkout(checkout["checkout_id"])
    assert (recorded["product"], recorded["plan"], recorded.get("domain"), recorded["project_id"]) == (
        "plan", "yearly", None, 42,
    )  # fmt: skip
    assert recorded["provider"] == "polar_sandbox"
    [polar_checkout] = stores["checkouts"]
    assert (polar_checkout["product_id"], polar_checkout["customer_email"]) == (_YEARLY_ID, "anh@example.com")
    assert polar_checkout["success_url"] == "https://app.test/projects/42"
    subscription_created, order_paid = _pay(stores, polar_checkout)
    row = await _deliver(subscription_created)
    assert (row["provider"], row["target_type"], row["target_id"], row["billing"], row["provider_object"]) == (
        "polar_sandbox", "PROJECT", 42, "recurring", "subscription",
    )  # fmt: skip
    assert (row["product"], row["plan"], row["product_data"], row["status"]) == ("plan", "yearly", {}, "active")
    assert row["renews_at"] == datetime(2027, 10, 28, 12, tzinfo=UTC)
    assert row["custom_data"] == {
        "checkout_id": str(checkout["checkout_id"]), "product": "plan", "plan": "yearly", "user_id": 7,
        "project_id": 42, "origin": "https://app.test",
    }  # fmt: skip
    assert await payments.get_recorded_checkout(row["custom_data"]["checkout_id"]) == recorded
    assert stores["events"][-1]["type"] == "payment.webhook" and stores["events"][-1]["grouping_ref"] == "42"
    # Its first payment's order just refreshes the same row -- not a purchase of its own.
    assert (await _deliver(order_paid))["id"] == row["id"]
    assert len(stores["subscriptions"]) == 1


async def test_a_discounted_domain_is_a_years_trial_and_the_one_time_purchase_a_lifetime_row(stores):
    checkout = await payments.create_domain_checkout(
        _USER, _PROJECT, "cozy.com", 15, True, {"registrant": {"first_name": "An"}}
    )
    assert (await payments.get_recorded_checkout(checkout["checkout_id"]))["registrant"] == {"first_name": "An"}
    [domain_checkout] = stores["checkouts"]
    assert (domain_checkout["product_id"], domain_checkout["trial_interval"], domain_checkout["trial_interval_count"]) == (
        _DOMAIN_15_ID, "year", 1,
    )  # fmt: skip
    row = await _deliver(_pay(stores, domain_checkout)[0])
    assert (row["product"], row["product_data"], row["status"]) == ("domain", {"domain": "cozy.com"}, "trialing")
    assert row["trial_ends_at"] is not None
    await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.ONE_TIME)
    [order_paid] = _pay(stores, stores["checkouts"][-1])
    one_time = await _deliver(order_paid)
    assert (one_time["product"], one_time["billing"], one_time["provider_object"], one_time["status"]) == (
        "one_time", "lifetime", "order", "active",
    )  # fmt: skip
    assert payments.to_access(list(stores["subscriptions"].values()), datetime.now(UTC))["can_download"]


async def test_an_email_polar_wont_take_is_left_for_the_buyer_to_type(stores, monkeypatch):
    create_checkout = polar.create_checkout

    async def picky_create_checkout(provider, product_id, checkout_data):
        if checkout_data.get("customer_email"):
            raise polar.ProviderError("polar_sandbox POST checkouts/: 422 ['customer_email' ...]", 422)
        return await create_checkout(provider, product_id, checkout_data)

    monkeypatch.setattr(polar, "create_checkout", picky_create_checkout)
    checkout = await payments.create_plan_checkout({**_USER, "username": "x@bb.cc"}, _PROJECT, payments.Plan.YEARLY)
    assert checkout["url"] and "customer_email" not in stores["checkouts"][0]


async def test_a_plain_domain_checkout_has_no_trial(stores):
    await payments.create_domain_checkout(_USER, _PROJECT, "cozy.com", 15, False, {})
    assert "trial_interval" not in stores["checkouts"][0]


async def test_purchases_we_didnt_start_here_are_recorded_and_left_alone(stores):
    # Bought some other way (no metadata), or through another deployment of ours sharing the sandbox.
    stores["polar"] = {
        "sub-x": {"id": "sub-x", "status": "active", "product_id": _YEARLY_ID, "metadata": {}},
        "sub-y": {
            "id": "sub-y",
            "status": "active",
            "product_id": _YEARLY_ID,
            "metadata": {"user_id": 7, "project_id": 42, "origin": "http://localhost:5173"},
        },
    }
    assert await _deliver({"type": "subscription.created", "data": stores["polar"]["sub-x"]}) is None
    assert await _deliver({"type": "subscription.created", "data": stores["polar"]["sub-y"]}) is None
    assert stores["subscriptions"] == {} and len(stores["events"]) == 2


async def test_events_we_dont_act_on_are_only_recorded(stores):
    assert await _deliver({"type": "checkout.updated", "data": {"id": "co-1"}}) is None
    assert stores["events"][-1]["direct_object_type"] == "checkout"


async def test_a_webhook_about_something_polar_doesnt_know_is_kept_but_not_retried(stores, monkeypatch):
    async def get_missing(provider, object_ref):
        raise polar.ProviderError("polar_sandbox GET subscriptions/x: 404", 404)

    monkeypatch.setattr(polar, "get_subscription", get_missing)
    assert await _deliver({"type": "subscription.updated", "data": {"id": "gone"}}) is None
    assert stores["subscriptions"] == {} and len(stores["events"]) == 1


async def test_a_polar_outage_while_taking_in_a_webhook_is_an_error_so_polar_retries(stores, monkeypatch):
    async def get_down(provider, object_ref):
        raise polar.ProviderError("polar_sandbox GET subscriptions/x: 503", 503)

    monkeypatch.setattr(polar, "get_subscription", get_down)
    with pytest.raises(polar.ProviderError):
        await _deliver({"type": "subscription.updated", "data": {"id": "sub-1"}})


async def test_an_unknown_checkout_is_none(stores):
    assert await payments.get_recorded_checkout(None) is None and await payments.get_recorded_checkout("999") is None


@pytest.mark.parametrize(
    "signed",
    [
        _sign({"type": "x"}, secret="whsec_" + base64.b64encode(b"someone-else").decode()),  # another secret
        _sign({"type": "x"}, timestamp=int(time.time()) - 3600),  # too old: a replay
        (
            b'{"type": "x"}',
            {"webhook-id": "msg_1", "webhook-timestamp": str(int(time.time())), "webhook-signature": "v1,forged"},
        ),
    ],
)
async def test_a_webhook_that_isnt_signed_right_is_refused_and_not_recorded(stores, signed):
    with pytest.raises(payments.InvalidSignature):
        await payments.handle_webhook(_SANDBOX, *signed)
    assert stores["events"] == []


async def test_a_webhook_signed_with_polars_own_key_scheme_is_taken_too(stores):
    # Polar's own scheme keys the HMAC with the whole secret string's bytes, not its decoded base64 part.
    raw_body = json.dumps({"type": "checkout.updated", "data": {"id": "co-1"}}).encode()
    webhook_id, sent_at = "msg_2", str(int(time.time()))
    digest = hmac.new(_SECRET.encode(), f"{webhook_id}.{sent_at}.".encode() + raw_body, hashlib.sha256).digest()
    headers = {
        "webhook-id": webhook_id,
        "webhook-timestamp": sent_at,
        "webhook-signature": f"v1,{base64.b64encode(digest).decode()}",
    }
    assert await payments.handle_webhook(_SANDBOX, raw_body, headers) is None
    assert len(stores["events"]) == 1


async def test_a_sandbox_webhook_isnt_taken_as_a_live_one(stores):
    # The live endpoint checks the live secret -- unset here, so nothing passes.
    with pytest.raises(payments.InvalidSignature):
        await payments.handle_webhook(payments.Provider.POLAR, *_sign({"type": "x"}))


async def test_a_cancel_at_period_end_reads_as_cancelled_until_it_ends(stores):
    await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.MONTHLY)
    await _deliver(_pay(stores, stores["checkouts"][0])[0])
    cancelling = {**stores["polar"]["sub-1"], "cancel_at_period_end": True, "ends_at": "2026-11-28T12:00:00Z"}
    stores["polar"] = {**stores["polar"], "sub-1": cancelling}
    row = await _deliver({"type": "subscription.canceled", "data": cancelling})
    assert (row["status"], row["renews_at"]) == ("cancelled", None)
    assert row["ends_at"] == datetime(2026, 11, 28, 12, tzinfo=UTC) and row["custom_data"]["project_id"] == 42


async def test_a_refund_refreshes_the_subscription_it_was_for(stores):
    await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.YEARLY)
    await _deliver(_pay(stores, stores["checkouts"][0])[0])
    stores["polar"] = {**stores["polar"], "sub-1": {**stores["polar"]["sub-1"], "status": "canceled"}}
    row = await _deliver({"type": "refund.created", "data": {"id": "ref-1", "order_id": "ord-1"}})
    assert row["status"] == "ended"


async def test_a_plan_change_and_ending_a_subscription_go_to_the_polar_it_was_made_in(stores):
    await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.MONTHLY)
    monthly = await _deliver(_pay(stores, stores["checkouts"][0])[0])
    await payments.change_plan(monthly, payments.Plan.YEARLY)
    await payments.revoke_subscription(monthly)
    await payments.refund_latest_payment(monthly)
    assert stores["calls"] == [
        ("change_product", _SANDBOX, "sub-1", _YEARLY_ID),
        ("revoke_subscription", _SANDBOX, "sub-1"),
        ("refund_order", _SANDBOX, stores["polar"]["ord-1"], "service_disruption"),
    ]


async def test_a_trial_has_nothing_to_refund(stores):
    await payments.create_domain_checkout(_USER, _PROJECT, "cozy.com", 15, True, {})
    domain = await _deliver(_pay(stores, stores["checkouts"][0])[0])
    await subscriptions.refund_latest_payment(domain)
    assert stores["calls"] == []


# --- catching up what webhooks missed ---


async def test_a_purchase_quiet_for_a_day_is_read_again_and_a_busy_one_isnt(stores):
    await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.YEARLY)
    row = await _deliver(_pay(stores, stores["checkouts"][0])[0])
    # Revoked on Polar's side, and every webhook about it lost.
    revoked = {**stores["polar"]["sub-1"], "status": "canceled", "ended_at": "2026-11-02T12:00:00Z"}
    stores["polar"] = {**stores["polar"], "sub-1": revoked}
    assert await payments.catch_up_purchases() == []  # heard from it just now: not asked about yet
    caught_up = await payments.catch_up_purchases(now=row["updated_at"] + timedelta(days=1, seconds=1))
    assert [(r["provider_ref"], r["status"]) for r in caught_up] == [("sub-1", "ended")]
    # Settled for good: an ended purchase isn't asked about again.
    assert await payments.catch_up_purchases(now=datetime.now(UTC) + timedelta(days=2)) == []


async def test_a_paid_checkout_whose_webhooks_were_all_lost_gets_its_row(stores):
    await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.ONE_TIME)
    _pay(stores, stores["checkouts"][0])  # paid at Polar, nothing delivered
    assert stores["subscriptions"] == {}
    [one_time] = await payments.catch_up_purchases()
    assert (one_time["product"], one_time["status"], one_time["target_id"]) == ("one_time", "active", 42)
    assert await payments.catch_up_purchases() == []  # it has its row now


async def test_a_checkout_never_paid_brings_nothing(stores):
    await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.MONTHLY)
    assert await payments.catch_up_purchases() == [] and stores["subscriptions"] == {}


async def test_one_purchase_that_cant_be_caught_up_doesnt_stop_the_others(stores, monkeypatch):
    await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.ONE_TIME)
    await payments.create_plan_checkout(_USER, {"id": 43}, payments.Plan.ONE_TIME)
    _pay(stores, stores["checkouts"][1])  # only the second one's paid
    find_checkout_orders = polar.find_checkout_orders

    async def first_one_fails(provider, checkout_id):
        if checkout_id == str(stores["events"][0]["id"]):
            raise polar.ProviderError("polar_sandbox GET orders/: 503", 503)
        return await find_checkout_orders(provider, checkout_id)

    monkeypatch.setattr(polar, "find_checkout_orders", first_one_fails)
    monkeypatch.setattr(purchases, "log_error", lambda *args, **kwargs: None)
    [caught_up] = await payments.catch_up_purchases()
    assert caught_up["target_id"] == 43


def test_a_domain_needs_a_plan_ever_paid_not_a_running_one():
    ended_plan = _subscription(status="ended", ends_at=_NOW - timedelta(days=90))
    assert payments.to_access([ended_plan], _NOW)["has_had_plan"]
    assert not payments.to_access([_one_time(_NOW - timedelta(days=1))], _NOW)["has_had_plan"]


@pytest.mark.parametrize(
    ("meta", "url"),
    [
        ({"sandbox_mode": False}, "https://polar.sh/biveit/portal"),
        ({"sandbox_mode": True}, "https://sandbox.polar.sh/biveit-sandbox/portal"),
    ],
)
def test_the_billing_portal_is_the_one_of_the_world_the_user_pays_in(meta, url):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "role": "ADMIN", "meta": meta}
    assert TestClient(app).get("/api/payments/portal").json() == {"url": url}


# --- plans set by an admin ---


def test_nothing_counts_before_it_starts():
    later = _subscription(created_at=_NOW + timedelta(days=3), status="cancelled", ends_at=_NOW + timedelta(days=40))
    access = payments.to_access([later], _NOW)
    assert not (access["can_host"] or access["can_download"] or access["has_had_plan"])
    assert not payments.is_running(later, _NOW) and not payments.is_in_grace(later, _NOW)


def test_an_admin_one_time_purchase_gives_edits_until_its_end():
    one_time = {**_one_time(_NOW - timedelta(days=50)), "ends_at": _NOW + timedelta(days=5)}
    access = payments.to_access([one_time], _NOW)
    assert access["can_edit"] and access["edit_until"] == _NOW + timedelta(days=5) and not access["can_host"]


@pytest.fixture
def plan_rows(monkeypatch):
    """subscriptions_repo for the admin plan functions, in memory: {id: row}."""
    rows = {}

    async def find(criteria, include_deleted=False):
        matching = [row for row in rows.values() if all(row.get(k) == v for k, v in criteria.items())]
        return [row for row in matching if include_deleted or not row.get("deleted_at")]

    async def insert(row):
        saved = {**row, "id": len(rows) + 1, "deleted_at": None}
        rows[saved["id"]] = saved
        return saved

    async def update_one(subscription_id, changes):
        rows[subscription_id] = {**rows[subscription_id], **changes}
        return rows[subscription_id]

    async def delete(subscription_id):
        rows[subscription_id] = {**rows[subscription_id], "deleted_at": _NOW}

    for name, fake in {"find": find, "insert": insert, "update_one": update_one, "delete": delete}.items():
        monkeypatch.setattr(subscriptions.subscriptions_repo, name, fake)
    return rows


_GRANTED_PROJECT = {"id": 7, "user_id": 3}


async def test_an_admin_plan_runs_its_period_then_has_the_usual_grace(plan_rows):
    plan = await payments.create_admin_plan(_GRANTED_PROJECT, payments.Plan.YEARLY, _NOW, _NOW + timedelta(days=365), 1)
    assert (plan["provider"], plan["status"], plan["product"]) == ("admin", "cancelled", "plan")
    assert payments.to_access([plan], _NOW + timedelta(days=100))["plan_subscription"]["plan"] == "yearly"
    after = payments.to_access([plan], _NOW + timedelta(days=370))
    assert after["can_host"] and after["plan_subscription"] is None  # grace


async def test_a_project_gets_one_plan_and_its_period_has_to_move_forward(plan_rows):
    await payments.create_admin_plan(_GRANTED_PROJECT, payments.Plan.MONTHLY, _NOW, _NOW + timedelta(days=30), 1)
    with pytest.raises(payments.PlanExists):
        await payments.create_admin_plan(_GRANTED_PROJECT, payments.Plan.YEARLY, _NOW, _NOW + timedelta(days=365), 1)
    with pytest.raises(payments.InvalidPlanPeriod):
        await payments.update_admin_plan(_GRANTED_PROJECT["id"], payments.Plan.YEARLY, _NOW, _NOW)
    upgraded = await payments.update_admin_plan(
        _GRANTED_PROJECT["id"], payments.Plan.ONE_TIME, _NOW, _NOW + timedelta(days=9)
    )
    assert (upgraded["product"], upgraded["billing"], upgraded["status"]) == ("one_time", "lifetime", "active")


async def test_a_bought_plan_can_be_deleted_but_not_edited_and_then_a_new_one_set(plan_rows):
    plan_rows[1] = {"id": 1, "provider": "polar", "product": "plan", "target_type": "PROJECT", "target_id": 7,
                    "created_at": _NOW, "deleted_at": None}  # fmt: skip
    with pytest.raises(payments.PlanNotEditable):
        await payments.update_admin_plan(_GRANTED_PROJECT["id"], payments.Plan.YEARLY, _NOW, _NOW + timedelta(days=1))
    await payments.delete_project_plan(_GRANTED_PROJECT["id"])
    assert await payments.get_project_plan(_GRANTED_PROJECT["id"]) is None
    await payments.create_admin_plan(_GRANTED_PROJECT, payments.Plan.MONTHLY, _NOW, _NOW + timedelta(days=30), 1)
    with pytest.raises(payments.NoPlan):
        await payments.delete_project_plan(99)
