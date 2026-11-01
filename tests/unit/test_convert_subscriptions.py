# The one-off payments conversion (scripts/convert_subscriptions.py): how old rows map onto today's subscriptions.
# The database part was run against a scratch copy; these cover the mapping.

from datetime import UTC, datetime

from scripts.convert_subscriptions import find_unknown_statuses, to_new_one_time, to_new_subscription

_PAID_AT = datetime(2026, 10, 20, tzinfo=UTC)


def _old_subscription(status="on_trial", **fields):
    return {
        "id": 3, "provider": "lemonsqueezy", "provider_ref": "sub-1", "user_id": 7, "target_type": "PROJECT",
        "target_id": 42, "product": "domain", "plan": None, "product_data": {"domain": "cozy.com"}, "status": status,
        "renews_at": None, "ends_at": None, "trial_ends_at": None, "custom_data": {}, "snapshot": {},
        "created_at": _PAID_AT, "updated_at": _PAID_AT, **fields,
    }  # fmt: skip


def _old_order(status="paid", product="one_time"):
    return {
        "id": 9, "provider": "lemonsqueezy", "provider_ref": "ord-1", "user_id": 7, "target_type": "PROJECT",
        "target_id": 42, "product": product, "status": status, "total_usd": 19, "custom_data": {"project_id": 42},
        "snapshot": {}, "created_at": _PAID_AT, "updated_at": _PAID_AT,
    }  # fmt: skip


def test_an_old_subscription_becomes_a_recurring_row_with_our_status_and_its_id():
    row = to_new_subscription(_old_subscription())
    assert (row["id"], row["provider_object"], row["billing"], row["status"]) == (
        3,
        "subscription",
        "recurring",
        "trialing",
    )
    assert row["product_data"] == {"domain": "cozy.com"}


def test_a_one_time_order_becomes_a_lifetime_row_paid_when_it_was():
    row = to_new_one_time(_old_order())
    assert (row["provider_object"], row["billing"], row["product"], row["plan"]) == (
        "order",
        "lifetime",
        "one_time",
        "one_time",
    )
    assert (row["status"], row["created_at"], row["custom_data"]) == ("active", _PAID_AT, {"project_id": 42})
    assert "id" not in row and to_new_one_time(_old_order("refunded"))["status"] == "refunded"


def test_statuses_the_mapping_doesnt_know_are_listed():
    assert find_unknown_statuses([_old_subscription(), _old_subscription("weird")], [_old_order("lost")]) == [
        "order:lost",
        "subscription:weird",
    ]
