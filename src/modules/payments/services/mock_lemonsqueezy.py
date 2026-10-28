# Intentions: a stand-in for Lemon Squeezy (settings.payments_mock), so the whole purchase flow runs before the store
# exists: checkout links lead to our own fake checkout page, "paying" there creates the subscription / order the
# real provider would, and hands back the webhooks it would send -- signed with WEBHOOK_SECRET -- for the caller to
# feed through the same path as real ones. Objects keep Lemon Squeezy's shapes ({"id", "attributes"}).
#
# State lives in Redis for MOCK_TTL_SECONDS: checkouts, subscriptions and orders, each under its own key. It's a
# test fixture, not a source of truth -- losing it just means old mock purchases can't be re-fetched.

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

from src.config import settings
from src.modules.payments.constants import PLANS, Plan, Product
from src.modules.payments.services import catalog
from src.utils import redis

WEBHOOK_SECRET = "mock-lemonsqueezy-webhook-secret"
MOCK_TTL_SECONDS = 90 * 24 * 3600


class MockCheckoutNotFound(Exception):
    """No such mock checkout (or it's already been paid)."""


async def create_checkout(variant_id: str, email: str, custom_data: dict) -> str:
    """A mock checkout, kept in Redis; its URL is our FE's fake checkout page."""
    checkout_id = uuid.uuid4().hex
    checkout = {"id": checkout_id, "variant_id": variant_id, "email": email, "custom_data": custom_data}
    await _save("checkouts", checkout_id, checkout)
    return f"{settings.frontend_url}/mock-checkout/{checkout_id}"


async def get_checkout(checkout_id: str) -> dict:
    """What the fake checkout page shows: {id, variant_id, email, custom_data, item: catalog.describe_variant}."""
    checkout = await _load("checkouts", checkout_id)
    if checkout is None:
        raise MockCheckoutNotFound(checkout_id)
    return {**checkout, "item": catalog.describe_variant(checkout["variant_id"])}


async def pay(checkout_id: str) -> list[tuple[bytes, str]]:
    """ "Pay" a mock checkout: create what it buys and return the webhooks Lemon Squeezy would send, as (raw body,
    X-Signature) pairs. A checkout can only be paid once."""
    checkout = await get_checkout(checkout_id)
    await redis.delete(_key("checkouts", checkout_id))
    item, now = checkout["item"], datetime.now(UTC)
    if item and item["product"] == Product.ONE_TIME:
        order = _to_object(uuid.uuid4().hex, {
            "status": "paid", "total_usd": PLANS[Plan.ONE_TIME]["price_usd"] * 100, "user_email": checkout["email"],
            "first_order_item": {"variant_id": checkout["variant_id"]}, "created_at": now.isoformat(),
        })  # fmt: skip
        await _save("orders", order["id"], order)
        return [_to_webhook("order_created", "orders", order, checkout["custom_data"])]
    is_trial = bool(item and item.get("free_first_year"))
    interval_days = 30 if item and item.get("plan") == Plan.MONTHLY else 365
    subscription = _to_object(uuid.uuid4().hex, {
        "status": "on_trial" if is_trial else "active", "variant_id": checkout["variant_id"],
        "user_email": checkout["email"], "renews_at": (now + timedelta(days=interval_days)).isoformat(),
        "trial_ends_at": (now + timedelta(days=365)).isoformat() if is_trial else None, "ends_at": None,
        "created_at": now.isoformat(),
    })  # fmt: skip
    await _save("subscriptions", subscription["id"], subscription)
    return [_to_webhook("subscription_created", "subscriptions", subscription, checkout["custom_data"])]


async def get_object(collection: str, object_id: str) -> dict:
    """A mock subscription / order, as get_subscription / get_order return them."""
    found = await _load(collection, object_id)
    if found is None:
        raise KeyError(f"no mock {collection} {object_id}")
    return found


async def cancel_subscription(subscription_id: str) -> None:
    """Cancelled: runs to the end of its period (or trial), like the real thing."""
    subscription = await get_object("subscriptions", subscription_id)
    attributes = subscription["attributes"]
    ends_at = attributes.get("trial_ends_at") or attributes.get("renews_at")
    await _save(
        "subscriptions",
        subscription_id,
        _to_object(subscription_id, {**attributes, "status": "cancelled", "ends_at": ends_at}),
    )


async def refund_latest_payment(subscription_id: str) -> None:
    """Nothing to move in a mock -- only checks the subscription exists."""
    await get_object("subscriptions", subscription_id)


def _to_object(object_id: str, attributes: dict) -> dict:
    return {"id": object_id, "attributes": {**attributes, "test_mode": True}}


def _to_webhook(event_name: str, data_type: str, data: dict, custom_data: dict) -> tuple[bytes, str]:
    """[Pure] A webhook as Lemon Squeezy sends it: the raw body and its signature."""
    body = json.dumps(
        {"meta": {"event_name": event_name, "custom_data": custom_data}, "data": {"type": data_type, **data}}
    )
    raw_body = body.encode()
    return raw_body, hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()


def _key(collection: str, object_id: str) -> str:
    return f"payments:mock:{collection}:{object_id}"


async def _save(collection: str, object_id: str, value: dict) -> None:
    await redis.set_value(_key(collection, object_id), json.dumps(value), MOCK_TTL_SECONDS)


async def _load(collection: str, object_id: str) -> dict | None:
    raw = await redis.get_value(_key(collection, object_id))
    return json.loads(raw) if raw else None
