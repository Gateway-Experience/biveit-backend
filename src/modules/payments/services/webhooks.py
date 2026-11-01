# Intentions: taking in the payment provider's webhooks -- the only way our copy of what's been bought changes. A
# webhook is a TRIGGER, not the data: its raw payload is appended to `events` (payment.webhook, the history), then
# the object it's about is fetched fresh from the provider and our row overwritten with that. So webhooks arriving
# late, twice or out of order can't leave a stale row: whatever comes last, the row says what the provider says now.
# Both provider objects land in `subscriptions`: a provider subscription as a recurring row, an order as a lifetime
# one -- but only an order for the one-time purchase. The provider also makes an order for a subscription's first
# payment; that subscription's own row already covers it (and the webhook stays in `events`).
# References: migrations/0011_subscriptions.sql (the table), services/lemonsqueezy.py (the objects' shapes).

import json
from datetime import datetime

import pydash

from src.modules.payments.constants import PROVIDER, Billing, PaymentEvent, Product, ProviderObject, TargetType
from src.modules.payments.repositories import subscriptions as subscriptions_repo
from src.modules.payments.services import catalog, lemonsqueezy
from src.repositories import events as events_repo


class InvalidSignature(Exception):
    """A webhook whose signature doesn't match -- not from the provider."""


async def handle_webhook(raw_body: bytes, signature: str) -> dict | None:
    """Record a webhook and bring the row it's about up to date. Returns that row, or None when it's about nothing
    we keep (or can't tell whose it is). Raises InvalidSignature."""
    if not lemonsqueezy.is_signature_valid(raw_body, signature):
        raise InvalidSignature("webhook signature doesn't match")
    payload = json.loads(raw_body)
    custom_data = pydash.get(payload, "meta.custom_data") or {}
    data_type, data_id = pydash.get(payload, "data.type"), str(pydash.get(payload, "data.id"))
    await events_repo.insert(
        {
            "type": PaymentEvent.WEBHOOK,
            "subject_type": "user",
            "subject_ref": str(custom_data.get("user_id") or ""),
            "direct_object_type": data_type,
            "direct_object_ref": data_id,
            "grouping_type": "project",
            "grouping_ref": str(custom_data.get("project_id") or ""),
            "value": payload,
        }
    )
    if data_type == "orders":
        return await _refresh_order(data_id, custom_data)
    if data_type == "subscriptions":
        return await _refresh_subscription(data_id, custom_data)
    if data_type == "subscription-invoices":
        return await _refresh_subscription(str(pydash.get(payload, "data.attributes.subscription_id")), custom_data)
    return None


async def _refresh_subscription(subscription_ref: str, custom_data: dict) -> dict | None:
    known = pydash.head(
        await subscriptions_repo.find(
            {"provider": PROVIDER, "provider_object": ProviderObject.SUBSCRIPTION, "provider_ref": subscription_ref}
        )
    )
    owner = custom_data if custom_data.get("project_id") else (known or {}).get("custom_data") or {}
    if not owner.get("project_id"):
        return None
    fresh = await lemonsqueezy.get_subscription(subscription_ref)
    attributes = fresh["attributes"]
    item = catalog.describe_variant(str(attributes.get("variant_id"))) or {}
    row = await subscriptions_repo.upsert(
        {
            "provider": PROVIDER,
            "provider_object": ProviderObject.SUBSCRIPTION,
            "provider_ref": subscription_ref,
            "billing": Billing.RECURRING,
            "user_id": int(owner["user_id"]),
            "target_type": TargetType.PROJECT,
            "target_id": int(owner["project_id"]),
            "product": item.get("product") or owner.get("product"),
            "plan": item.get("plan"),
            "product_data": {"domain": owner.get("domain")} if item.get("product") == Product.DOMAIN else {},
            "status": lemonsqueezy.to_status(ProviderObject.SUBSCRIPTION, attributes["status"]),
            "renews_at": _to_datetime(attributes.get("renews_at")),
            "ends_at": _to_datetime(attributes.get("ends_at")),
            "trial_ends_at": _to_datetime(attributes.get("trial_ends_at")),
            "custom_data": custom_data,
            "snapshot": fresh,
        }
    )
    return row


async def _refresh_order(order_ref: str, custom_data: dict) -> dict | None:
    known = pydash.head(
        await subscriptions_repo.find(
            {"provider": PROVIDER, "provider_object": ProviderObject.ORDER, "provider_ref": order_ref}
        )
    )
    owner = custom_data if custom_data.get("project_id") else (known or {}).get("custom_data") or {}
    if not owner.get("project_id"):
        return None
    fresh = await lemonsqueezy.get_order(order_ref)
    attributes = fresh["attributes"]
    item = catalog.describe_variant(str(pydash.get(attributes, "first_order_item.variant_id"))) or {}
    # A subscription's first payment: its subscription row is the purchase (see the top of this file).
    if (item.get("product") or owner.get("product")) != Product.ONE_TIME:
        return None
    row = await subscriptions_repo.upsert(
        {
            "provider": PROVIDER,
            "provider_object": ProviderObject.ORDER,
            "provider_ref": order_ref,
            "billing": Billing.LIFETIME,
            "user_id": int(owner["user_id"]),
            "target_type": TargetType.PROJECT,
            "target_id": int(owner["project_id"]),
            "product": Product.ONE_TIME,
            "plan": item.get("plan") or owner.get("plan"),
            "product_data": {},
            "status": lemonsqueezy.to_status(ProviderObject.ORDER, attributes["status"]),
            "custom_data": custom_data,
            "snapshot": fresh,
            # When it was paid; the column's default (now) if the provider didn't say.
            **pydash.omit_by({"created_at": _to_datetime(attributes.get("created_at"))}, pydash.is_none),
        }
    )
    return row


def _to_datetime(value: str | None) -> datetime | None:
    """[Pure] The provider's ISO-8601 timestamps (Python 3.11+ reads their trailing Z) as aware datetimes."""
    return datetime.fromisoformat(value) if value else None
