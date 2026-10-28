# Intentions: the Lemon Squeezy client -- the few calls payments needs, passing their objects through as they shape
# them ({"id", "attributes": {...}}, JSON:API). With settings.payments_mock on, every call goes to mock_lemonsqueezy
# instead, which keeps the same shapes, so nothing above this file knows the difference.
#
# How their API works (https://docs.lemonsqueezy.com/api):
# - JSON:API over HTTPS: Accept / Content-Type application/vnd.api+json, Bearer API key. Test mode is the same API
#   with a test-mode key.
# - A checkout is created for ONE variant (no cart), with our custom data, which comes back in every webhook about
#   what it bought (meta.custom_data) -- but not when the object is fetched later, so payments keeps it.
# - Amounts are in cents.
# - Webhooks are signed: X-Signature = hex HMAC-SHA256 of the raw body with the webhook's signing secret.
# TODO: written from their docs; not run against a real store yet (there isn't one). Check each call in test mode.
# References: services/mock_lemonsqueezy.py (the stand-in), services/webhooks.py (what reads the objects).

import hashlib
import hmac
from typing import Any

import pydash

from src.config import settings
from src.modules.payments.services import mock_lemonsqueezy
from src.utils import http

_API_URL = "https://api.lemonsqueezy.com/v1"


class ProviderError(Exception):
    """Lemon Squeezy refused or failed a call."""


async def create_checkout(variant_id: str, email: str, custom_data: dict, expires_at: str) -> str:
    """A checkout link for one variant, our custom data attached, the email filled in. Returns its URL. [I/O]"""
    if settings.payments_mock:
        return await mock_lemonsqueezy.create_checkout(variant_id, email, custom_data)
    body = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {"email": email, "custom": custom_data},
                "expires_at": expires_at,
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": settings.lemonsqueezy_store_id}},
                "variant": {"data": {"type": "variants", "id": variant_id}},
            },
        }
    }
    response = await _call_api("POST", "checkouts", body)
    return pydash.get(response, "data.attributes.url")


async def get_subscription(subscription_ref: str) -> dict:
    """A subscription as it is now: {"id", "attributes": {status, variant_id, renews_at, ends_at, trial_ends_at,
    user_email...}}. [I/O]"""
    if settings.payments_mock:
        return await mock_lemonsqueezy.get_object("subscriptions", subscription_ref)
    return (await _call_api("GET", f"subscriptions/{subscription_ref}"))["data"]


async def get_order(order_ref: str) -> dict:
    """An order as it is now: {"id", "attributes": {status, total_usd (cents), first_order_item, created_at...}}."""
    if settings.payments_mock:
        return await mock_lemonsqueezy.get_object("orders", order_ref)
    return (await _call_api("GET", f"orders/{order_ref}"))["data"]


async def cancel_subscription(subscription_ref: str) -> None:
    """Cancel a subscription: it runs until the end of its paid period (or trial), then expires. [I/O]"""
    if settings.payments_mock:
        await mock_lemonsqueezy.cancel_subscription(subscription_ref)
        return
    await _call_api("DELETE", f"subscriptions/{subscription_ref}")


async def refund_latest_payment(subscription_ref: str) -> None:
    """Refund the subscription's most recent payment in full (a trial has none: nothing happens). [I/O]"""
    if settings.payments_mock:
        await mock_lemonsqueezy.refund_latest_payment(subscription_ref)
        return
    invoices = await _call_api("GET", f"subscription-invoices?filter[subscription_id]={subscription_ref}")
    paid = [
        invoice for invoice in pydash.get(invoices, "data", []) if pydash.get(invoice, "attributes.status") == "paid"
    ]
    if paid:
        latest = max(paid, key=lambda invoice: pydash.get(invoice, "attributes.created_at"))
        await _call_api("POST", f"subscription-invoices/{latest['id']}/refund")


def is_signature_valid(raw_body: bytes, signature: str) -> bool:
    """[Pure-ish] Whether a webhook's X-Signature matches its body (compared in constant time)."""
    secret = mock_lemonsqueezy.WEBHOOK_SECRET if settings.payments_mock else settings.lemonsqueezy_webhook_secret
    if not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


async def _call_api(method: str, path: str, body: dict | None = None) -> Any:
    status, response, _headers = await http.request(
        method,
        f"{_API_URL}/{path}",
        json=body,
        headers={
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "Authorization": f"Bearer {settings.lemonsqueezy_api_key}",
        },
    )
    if not 200 <= status < 300:
        raise ProviderError(f"lemonsqueezy {method} {path}: {status} {pydash.get(response, 'errors')}")
    return response
