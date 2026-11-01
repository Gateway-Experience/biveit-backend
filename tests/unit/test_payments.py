# Payments tests (modules/payments): what we charge for a domain, what a project's purchases allow (access), and
# taking in webhooks -- signature, the stand-in provider's purchases going through the real path, our rows following
# what the provider says. The tables and Redis are faked; the real provider isn't involved (payments_mock).

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from src.config import settings
from src.modules import payments
from src.modules.payments.services import lemonsqueezy, mock_lemonsqueezy, webhooks

_NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


# --- domain prices ---


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        # Priced off the dearer of registering and renewing: a cheap first year doesn't hide a dear renewal.
        ({"price_usd": 8.9, "renewal_price_usd": 12.9}, {"price_per_year": 15, "discountable": True}),
        ({"price_usd": 2.0, "renewal_price_usd": 35.0}, {"price_per_year": 40, "discountable": False}),
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


def test_every_price_point_has_its_variants():
    for point in payments.DOMAIN_PRICE_POINTS:
        assert payments.describe_variant(point["variant_id"]) == {
            "product": "domain", "price_per_year": point["price_usd"], "discounted": False,
        }  # fmt: skip
        if point["discountable"]:
            assert payments.describe_variant(point["discounted_variant_id"])["discounted"]
    assert payments.describe_variant("mock-plan-yearly") == {"product": "plan", "plan": "yearly"}
    assert payments.describe_variant("not-ours") is None


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
    ("provider_object", "provider_status", "status"),
    [
        ("subscription", "on_trial", "trialing"),
        ("subscription", "past_due", "past_due"),
        ("subscription", "cancelled", "cancelled"),
        ("subscription", "unpaid", "ended"),
        ("subscription", "paused", "ended"),
        ("order", "paid", "active"),
        ("order", "refunded", "refunded"),
    ],
)
def test_lemonsqueezys_statuses_are_read_as_ours(provider_object, provider_status, status):
    assert lemonsqueezy.to_status(provider_object, provider_status) == status


def test_a_status_we_dont_know_fails_loudly():
    with pytest.raises(ValueError):
        lemonsqueezy.to_status(payments.ProviderObject.SUBSCRIPTION, "on_vacation")


# --- webhooks ---


@pytest.fixture
def stores(monkeypatch):
    """Redis (for the stand-in provider), the subscriptions table and the events log, all in memory."""
    state = {"redis": {}, "subscriptions": {}, "events": []}

    async def set_value(key, value, ttl):
        state["redis"] = {**state["redis"], key: value}

    async def get_value(key):
        return state["redis"].get(key)

    async def delete(key):
        state["redis"] = {k: v for k, v in state["redis"].items() if k != key}

    async def find(criteria):
        return [row for row in state["subscriptions"].values() if all(row.get(k) == v for k, v in criteria.items())]

    async def upsert(row):
        key = (row["provider_object"], row["provider_ref"])
        known = state["subscriptions"].get(key, {})
        # Like the SQL: custom_data kept once it's there.
        created = {"id": len(state["subscriptions"]) + 1, "created_at": _NOW}
        merged = {**created, **known, **row, "custom_data": known.get("custom_data") or row.get("custom_data")}
        state["subscriptions"] = {**state["subscriptions"], key: merged}
        return merged

    async def insert_event(event):
        row = {**event, "id": len(state["events"]) + 1, "created_at": _NOW}
        state["events"] = [*state["events"], row]
        return row

    async def get_event(event_id):
        return next((event for event in state["events"] if event["id"] == event_id), None)

    monkeypatch.setattr(settings, "payments_mock", True)
    monkeypatch.setattr(settings, "frontend_url", "https://app.test")
    monkeypatch.setattr(mock_lemonsqueezy.redis, "set_value", set_value)
    monkeypatch.setattr(mock_lemonsqueezy.redis, "get_value", get_value)
    monkeypatch.setattr(mock_lemonsqueezy.redis, "delete", delete)
    monkeypatch.setattr(webhooks.subscriptions_repo, "find", find)
    monkeypatch.setattr(webhooks.subscriptions_repo, "upsert", upsert)
    monkeypatch.setattr(webhooks.events_repo, "insert", insert_event)
    monkeypatch.setattr(webhooks.events_repo, "get", get_event)
    return state


_USER = {"id": 7, "username": "anh@example.com"}
_PROJECT = {"id": 42}


def _mock_checkout_id(url):
    return url.rsplit("/", 1)[-1]


async def test_a_paid_yearly_plan_becomes_our_subscription_row_pointing_at_its_checkout(stores):
    checkout = await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.YEARLY)
    assert checkout["url"] == f"https://app.test/mock-checkout/{_mock_checkout_id(checkout['url'])}"
    # The checkout is recorded first, with what it's for.
    assert stores["events"][0]["type"] == "payment.checkout_created"
    recorded = await payments.get_checkout(checkout["checkout_id"])
    assert (recorded["product"], recorded["plan"], recorded.get("domain"), recorded["project_id"]) == (
        "plan",
        "yearly",
        None,
        42,
    )
    [(raw_body, signature)] = await payments.pay_mock_checkout(_mock_checkout_id(checkout["url"]))
    row = await payments.handle_webhook(raw_body, signature)
    assert (row["target_type"], row["target_id"], row["billing"], row["provider_object"]) == (
        "PROJECT", 42, "recurring", "subscription",
    )  # fmt: skip
    assert (row["product"], row["plan"], row["product_data"], row["status"]) == ("plan", "yearly", {}, "active")
    assert row["custom_data"] == {
        "checkout_id": str(checkout["checkout_id"]),
        "product": "plan",
        "plan": "yearly",
        "user_id": 7,
        "project_id": 42,
    }
    assert await payments.get_checkout(row["custom_data"]["checkout_id"]) == recorded
    assert stores["events"][-1]["type"] == "payment.webhook" and stores["events"][-1]["grouping_ref"] == "42"
    with pytest.raises(payments.MockCheckoutNotFound):  # paid once only
        await payments.pay_mock_checkout(_mock_checkout_id(checkout["url"]))


async def test_a_discounted_domain_is_a_trial_and_the_one_time_purchase_a_lifetime_row(stores):
    checkout = await payments.create_domain_checkout(
        _USER, _PROJECT, "cozy.com", 15, True, {"registrant": {"first_name": "An"}}
    )
    assert (await payments.get_checkout(checkout["checkout_id"]))["registrant"] == {"first_name": "An"}
    [(raw_body, signature)] = await payments.pay_mock_checkout(_mock_checkout_id(checkout["url"]))
    row = await payments.handle_webhook(raw_body, signature)
    assert (row["product"], row["product_data"], row["status"]) == ("domain", {"domain": "cozy.com"}, "trialing")
    assert row["trial_ends_at"] is not None
    checkout = await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.ONE_TIME)
    [(raw_body, signature)] = await payments.pay_mock_checkout(_mock_checkout_id(checkout["url"]))
    one_time = await payments.handle_webhook(raw_body, signature)
    assert (one_time["product"], one_time["billing"], one_time["provider_object"], one_time["status"]) == (
        "one_time", "lifetime", "order", "active",
    )  # fmt: skip
    assert payments.to_access(list(stores["subscriptions"].values()), datetime.now(UTC))["can_download"]


async def test_the_order_for_a_subscriptions_first_payment_isnt_a_purchase_of_its_own(stores):
    checkout = await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.MONTHLY)
    order = mock_lemonsqueezy._to_object(
        "order-1", {"status": "paid", "first_order_item": {"variant_id": "mock-plan-monthly"}}
    )
    await mock_lemonsqueezy._save("orders", "order-1", order)
    custom_data = {"checkout_id": str(checkout["checkout_id"]), "user_id": 7, "project_id": 42, "product": "plan"}
    raw_body, signature = mock_lemonsqueezy._to_webhook("order_created", "orders", order, custom_data)
    assert await payments.handle_webhook(raw_body, signature) is None
    assert stores["subscriptions"] == {} and stores["events"][-1]["type"] == "payment.webhook"


async def test_an_unknown_checkout_is_none(stores):
    assert await payments.get_checkout(None) is None and await payments.get_checkout("999") is None


async def test_a_webhook_with_a_bad_signature_is_refused_and_not_recorded(stores):
    with pytest.raises(payments.InvalidSignature):
        await payments.handle_webhook(b'{"meta": {}, "data": {}}', "forged")
    assert stores["events"] == []


async def test_a_later_webhook_without_custom_data_still_updates_the_row(stores):
    checkout = await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.MONTHLY)
    [(raw_body, signature)] = await payments.pay_mock_checkout(_mock_checkout_id(checkout["url"]))
    subscription_ref = (await payments.handle_webhook(raw_body, signature))["provider_ref"]
    await payments.cancel_subscription(subscription_ref)
    fresh = await mock_lemonsqueezy.get_object("subscriptions", subscription_ref)
    cancelled_body, cancelled_signature = mock_lemonsqueezy._to_webhook(
        "subscription_cancelled", "subscriptions", fresh, {}
    )
    row = await payments.handle_webhook(cancelled_body, cancelled_signature)
    assert row["status"] == "cancelled" and row["ends_at"] is not None and row["custom_data"]["project_id"] == 42


async def test_a_plan_change_keeps_the_same_subscription_on_its_new_plan(stores):
    checkout = await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.MONTHLY)
    [(raw_body, signature)] = await payments.pay_mock_checkout(_mock_checkout_id(checkout["url"]))
    monthly = await payments.handle_webhook(raw_body, signature)
    [(changed_body, changed_signature)] = await payments.change_plan(monthly["provider_ref"], payments.Plan.YEARLY)
    yearly = await payments.handle_webhook(changed_body, changed_signature)
    assert (yearly["id"], yearly["plan"], yearly["custom_data"]) == (monthly["id"], "yearly", monthly["custom_data"])
    assert len(stores["subscriptions"]) == 1


def test_a_domain_needs_a_plan_ever_paid_not_a_running_one():
    ended_plan = _subscription(status="ended", ends_at=_NOW - timedelta(days=90))
    assert payments.to_access([ended_plan], _NOW)["has_had_plan"]
    assert not payments.to_access([_one_time(_NOW - timedelta(days=1))], _NOW)["has_had_plan"]
