# Payments tests (modules/payments): what we charge for a domain, what a project's purchases allow (access), and
# taking in webhooks -- signature, the stand-in provider's purchases going through the real path, our rows following
# what the provider says. The tables and Redis are faked; the real provider isn't involved (payments_mock).

from datetime import UTC, datetime, timedelta

import pytest

from src.config import settings
from src.modules import payments
from src.modules.payments.services import mock_lemonsqueezy, webhooks

_NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


# --- domain prices ---


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        # Priced off the dearer of registering and renewing: a cheap first year doesn't hide a dear renewal.
        ({"price_usd": 8.9, "renewal_price_usd": 12.9}, {"price_per_year": 15, "free_first_year_eligible": True}),
        ({"price_usd": 2.0, "renewal_price_usd": 35.0}, {"price_per_year": 40, "free_first_year_eligible": False}),
        ({"price_usd": 10.0, "renewal_price_usd": 10.0}, {"price_per_year": 10, "free_first_year_eligible": True}),
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
            "product": "domain", "price_per_year": point["price_usd"], "free_first_year": False,
        }  # fmt: skip
        if point["free_first_year"]:
            assert payments.describe_variant(point["trial_variant_id"])["free_first_year"]
    assert payments.describe_variant("mock-plan-yearly") == {"product": "plan", "plan": "yearly"}
    assert payments.describe_variant("not-ours") is None


# --- access ---


def _subscription(product="plan", status="active", ends_at=None, updated_at=_NOW, **fields):
    return {
        "product": product,
        "status": status,
        "ends_at": ends_at,
        "updated_at": updated_at,
        "plan": "monthly",
        **fields,
    }


def _order(created_at, status="paid"):
    return {"product": "one_time", "status": status, "created_at": created_at}


def test_nothing_bought_allows_nothing():
    access = payments.to_access([], [], _NOW)
    assert not (access["can_download"] or access["can_edit"] or access["can_host"]) and access["plan"] is None


def test_a_running_plan_allows_everything():
    access = payments.to_access([_subscription(status="past_due")], [], _NOW)
    assert access["can_download"] and access["can_edit"] and access["can_host"] and access["plan"] == "monthly"


def test_a_one_time_purchase_gives_the_code_and_30_days_of_edits_but_no_hosting():
    fresh = payments.to_access([], [_order(_NOW - timedelta(days=10))], _NOW)
    assert fresh["can_download"] and fresh["can_edit"] and not fresh["can_host"]
    assert fresh["edit_until"] == _NOW + timedelta(days=20)
    old = payments.to_access([], [_order(_NOW - timedelta(days=31))], _NOW)
    assert old["can_download"] and not old["can_edit"]
    refunded = payments.to_access([], [_order(_NOW, status="refunded")], _NOW)
    assert not refunded["can_download"]


def test_a_cancelled_plan_runs_to_its_end_then_hosting_has_a_grace_period():
    running = _subscription(status="cancelled", ends_at=_NOW + timedelta(days=3))
    assert payments.to_access([running], [], _NOW)["can_edit"]
    ended = _subscription(status="expired", ends_at=_NOW - timedelta(days=2))
    in_grace = payments.to_access([ended], [], _NOW)
    assert in_grace["can_host"] and not in_grace["can_edit"] and in_grace["plan"] is None and in_grace["can_download"]
    long_gone = _subscription(status="expired", ends_at=_NOW - timedelta(days=8))
    assert not payments.to_access([long_gone], [], _NOW)["can_host"]


def test_the_running_domain_subscription_is_picked_out():
    domain = _subscription(product="domain", status="on_trial", product_data={"domain": "cozy.com"})
    old_domain = _subscription(
        product="domain", status="expired", ends_at=_NOW - timedelta(days=40), product_data={"domain": "old.com"}
    )
    access = payments.to_access([old_domain, _subscription(), domain], [], _NOW)
    assert access["domain_subscription"]["product_data"]["domain"] == "cozy.com"


# --- webhooks ---


@pytest.fixture
def stores(monkeypatch):
    """Redis (for the stand-in provider), the two tables and the events log, all in memory."""
    state = {"redis": {}, "subscriptions": {}, "orders": {}, "events": []}

    async def set_value(key, value, ttl):
        state["redis"] = {**state["redis"], key: value}

    async def get_value(key):
        return state["redis"].get(key)

    async def delete(key):
        state["redis"] = {k: v for k, v in state["redis"].items() if k != key}

    def fake_table(name):
        async def find(criteria):
            return [row for row in state[name].values() if all(row.get(k) == v for k, v in criteria.items())]

        async def upsert(row):
            known = state[name].get(row["provider_ref"], {})
            # Like the SQL: custom_data kept once it's there.
            merged = {**known, **row, "custom_data": known.get("custom_data") or row.get("custom_data")}
            state[name] = {**state[name], row["provider_ref"]: merged}
            return merged

        return find, upsert

    async def insert_event(event):
        state["events"] = [*state["events"], event]

    monkeypatch.setattr(settings, "payments_mock", True)
    monkeypatch.setattr(settings, "frontend_url", "https://app.test")
    monkeypatch.setattr(mock_lemonsqueezy.redis, "set_value", set_value)
    monkeypatch.setattr(mock_lemonsqueezy.redis, "get_value", get_value)
    monkeypatch.setattr(mock_lemonsqueezy.redis, "delete", delete)
    for name, repo in [("subscriptions", webhooks.subscriptions_repo), ("orders", webhooks.orders_repo)]:
        find, upsert = fake_table(name)
        monkeypatch.setattr(repo, "find", find)
        monkeypatch.setattr(repo, "upsert", upsert)
    monkeypatch.setattr(webhooks.events_repo, "insert", insert_event)
    return state


_USER = {"id": 7, "username": "anh@example.com"}
_PROJECT = {"id": 42}


async def test_a_paid_yearly_plan_becomes_our_subscription_row(stores):
    url = await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.YEARLY, {"launch_id": "l1"})
    checkout_id = url.rsplit("/", 1)[-1]
    assert url == f"https://app.test/mock-checkout/{checkout_id}"
    [(raw_body, signature)] = await payments.pay_mock_checkout(checkout_id)
    row = await payments.handle_webhook(raw_body, signature)
    assert (row["target_type"], row["target_id"]) == ("PROJECT", 42)
    assert (row["product"], row["plan"], row["product_data"], row["status"]) == ("plan", "yearly", {}, "active")
    assert row["custom_data"] == {
        "launch_id": "l1",
        "product": "plan",
        "plan": "yearly",
        "user_id": 7,
        "project_id": 42,
    }
    assert stores["events"][0]["type"] == "payment.webhook" and stores["events"][0]["grouping_ref"] == "42"
    with pytest.raises(payments.MockCheckoutNotFound):  # paid once only
        await payments.pay_mock_checkout(checkout_id)


async def test_a_free_first_year_domain_is_a_trial_and_the_one_time_purchase_an_order(stores):
    url = await payments.create_domain_checkout(_USER, _PROJECT, "cozy.com", 15, True, {"launch_id": "l1"})
    [(raw_body, signature)] = await payments.pay_mock_checkout(url.rsplit("/", 1)[-1])
    row = await payments.handle_webhook(raw_body, signature)
    assert (row["product"], row["product_data"], row["status"]) == ("domain", {"domain": "cozy.com"}, "on_trial")
    assert row["trial_ends_at"] is not None
    url = await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.ONE_TIME, {})
    [(raw_body, signature)] = await payments.pay_mock_checkout(url.rsplit("/", 1)[-1])
    order = await payments.handle_webhook(raw_body, signature)
    assert (order["product"], order["status"], order["total_usd"]) == ("one_time", "paid", 19)


async def test_a_webhook_with_a_bad_signature_is_refused_and_not_recorded(stores):
    with pytest.raises(payments.InvalidSignature):
        await payments.handle_webhook(b'{"meta": {}, "data": {}}', "forged")
    assert stores["events"] == []


async def test_a_later_webhook_without_custom_data_still_updates_the_row(stores):
    url = await payments.create_plan_checkout(_USER, _PROJECT, payments.Plan.MONTHLY, {})
    checkout_id = url.rsplit("/", 1)[-1]
    [(raw_body, signature)] = await payments.pay_mock_checkout(checkout_id)
    subscription_ref = (await payments.handle_webhook(raw_body, signature))["provider_ref"]
    await payments.cancel_subscription(subscription_ref)
    fresh = await mock_lemonsqueezy.get_object("subscriptions", subscription_ref)
    cancelled_body, cancelled_signature = mock_lemonsqueezy._to_webhook(
        "subscription_cancelled", "subscriptions", fresh, {}
    )
    row = await payments.handle_webhook(cancelled_body, cancelled_signature)
    assert row["status"] == "cancelled" and row["ends_at"] is not None and row["custom_data"]["project_id"] == 42
