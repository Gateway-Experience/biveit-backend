# Intentions: the Polar client -- the few calls payments needs, passing Polar's objects through as it shapes them.
# Every call names which Polar it goes to (constants.Provider: live or the sandbox -- separate worlds with their own
# token, products and webhook secret), so a purchase is always handled in the world it was made in.
#
# How their API works (https://polar.sh/docs/api-reference):
# - JSON over HTTPS, Bearer organization access token. Live: api.polar.sh; sandbox: sandbox-api.polar.sh.
# - A checkout is for products (we always send one). Its `metadata` is copied onto the order and subscription it
#   makes, so fetching those later still says whose they are.
# - Amounts are in cents.
# - Webhooks follow Standard Webhooks: headers webhook-id / webhook-timestamp / webhook-signature, the signature a
#   base64 HMAC-SHA256 of "<id>.<timestamp>.<raw body>" keyed with the secret's base64 part (after "whsec_").
# - A subscription cancelled at period end stays `active` with cancel_at_period_end set, until it ends (`canceled`).
#   Revoking (DELETE) ends it now; neither refunds anything -- refunds are per order.
# References: services/webhooks.py (what reads the objects).

import base64
import hashlib
import hmac
import time
from typing import Any

import pydash

from src.config import settings
from src.modules.payments.constants import WEBHOOK_TOLERANCE_SECONDS, Provider, SubscriptionStatus
from src.utils import http

_API_URLS = {Provider.POLAR: "https://api.polar.sh/v1", Provider.POLAR_SANDBOX: "https://sandbox-api.polar.sh/v1"}
# Each org's customer portal: customers sign in with the email they paid with (Polar emails a code) and manage their
# subscriptions, invoices and payment method there. Our org slugs: "biveit" live, "biveit-sandbox" in the sandbox.
_PORTAL_URLS = {
    Provider.POLAR: "https://polar.sh/biveit/portal",
    Provider.POLAR_SANDBOX: "https://sandbox.polar.sh/biveit-sandbox/portal",
}

# Their order statuses in ours (constants.SubscriptionStatus). A partly refunded order still paid for the purchase.
_ORDER_STATUSES = {
    "draft": SubscriptionStatus.PENDING,
    "pending": SubscriptionStatus.PENDING,
    "paid": SubscriptionStatus.ACTIVE,
    "partially_refunded": SubscriptionStatus.ACTIVE,
    "refunded": SubscriptionStatus.REFUNDED,
    "void": SubscriptionStatus.ENDED,
}
# Their subscription statuses in ours -- except `active`, which also depends on cancel_at_period_end
# (to_subscription_status). A paused subscription collects nothing, so for us it's over.
_SUBSCRIPTION_STATUSES = {
    "incomplete": SubscriptionStatus.PENDING,
    "trialing": SubscriptionStatus.TRIALING,
    "past_due": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.ENDED,
    "unpaid": SubscriptionStatus.ENDED,
    "paused": SubscriptionStatus.ENDED,
    "incomplete_expired": SubscriptionStatus.ENDED,
}


class ProviderError(Exception):
    """Polar refused or failed a call. `status`: its HTTP status (0 when it never answered)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


async def create_checkout(provider: Provider, product_id: str, checkout_data: dict) -> dict:
    """A checkout for one product. `checkout_data`: the rest of Polar's checkout fields (customer_email, metadata,
    success_url, trial_interval...). Returns Polar's checkout ({id, url, ...}). [I/O]"""
    return await _call_api(provider, "POST", "checkouts/", {"products": [product_id], **checkout_data})


def get_portal_url(provider: Provider) -> str:
    """[Pure] Where a customer manages what they bought in that Polar world -- the org's customer portal."""
    return _PORTAL_URLS[provider]


async def get_subscription(provider: Provider, subscription_ref: str) -> dict:
    """A subscription as it is now ({id, status, product_id, cancel_at_period_end, current_period_end, ends_at,
    ended_at, trial_end, metadata...}). [I/O]"""
    return await _call_api(provider, "GET", f"subscriptions/{subscription_ref}")


async def get_order(provider: Provider, order_ref: str) -> dict:
    """An order as it is now ({id, status, billing_reason, product_id, subscription_id, refundable_amount, metadata,
    created_at...}). [I/O]"""
    return await _call_api(provider, "GET", f"orders/{order_ref}")


async def find_subscription_orders(provider: Provider, subscription_ref: str) -> list[dict]:
    """A subscription's orders, newest first (its first payment, renewals, plan changes). [I/O]"""
    response = await _call_api(
        provider, "GET", f"orders/?subscription_id={subscription_ref}&sorting=-created_at&limit=100"
    )
    return response["items"]


async def find_checkout_orders(provider: Provider, checkout_id: str) -> list[dict]:
    """The orders made by one of our checkouts (matched on the checkout_id in their metadata): none until it's
    paid. [I/O]"""
    response = await _call_api(provider, "GET", f"orders/?metadata%5Bcheckout_id%5D={checkout_id}&limit=100")
    return response["items"]


async def change_product(provider: Provider, subscription_ref: str, product_id: str) -> None:
    """Move a subscription to another product (a plan change), charging the prorated difference now. [I/O]"""
    await _call_api(
        provider,
        "PATCH",
        f"subscriptions/{subscription_ref}",
        {"product_id": product_id, "proration_behavior": "invoice"},
    )


async def revoke_subscription(provider: Provider, subscription_ref: str) -> None:
    """End a subscription right now (not at period end). Refunds nothing. Already ended: nothing to do. [I/O]"""
    try:
        await _call_api(provider, "DELETE", f"subscriptions/{subscription_ref}")
    except ProviderError as error:
        if error.status != 403:  # 403: already revoked
            raise


async def refund_order(provider: Provider, order: dict, reason: str) -> None:
    """Refund whatever's left of an order (net of tax -- Polar works the tax out). Nothing left: nothing to do.
    `reason`: one of Polar's (customer_request, service_disruption...). [I/O]"""
    if not order.get("refundable_amount"):
        return
    await _call_api(
        provider, "POST", "refunds/", {"order_id": order["id"], "reason": reason, "amount": order["refundable_amount"]}
    )


async def find_products(provider: Provider) -> list[dict]:
    """The organization's products that aren't archived (up to 100 -- we have about ten). [I/O]"""
    return (await _call_api(provider, "GET", "products/?limit=100&is_archived=false"))["items"]


async def create_product(provider: Provider, product: dict) -> dict:
    """Make a product (Polar's product-create body). Returns it. [I/O]"""
    return await _call_api(provider, "POST", "products/", product)


def to_order_status(order: dict) -> SubscriptionStatus:
    """[Pure] An order's status, in ours. Raises ValueError for one we don't know -- better a webhook that fails (and
    gets retried once this knows it) than a purchase quietly read wrong."""
    status = _ORDER_STATUSES.get(order["status"])
    if status is None:
        raise ValueError(f"unknown polar order status: {order['status']}")
    return status


def to_subscription_status(subscription: dict) -> SubscriptionStatus:
    """[Pure] A subscription's status, in ours: an active one set to cancel at period end is our `cancelled` (runs
    until its ends_at). Raises ValueError for one we don't know (see to_order_status)."""
    if subscription["status"] == "active":
        return SubscriptionStatus.CANCELLED if subscription.get("cancel_at_period_end") else SubscriptionStatus.ACTIVE
    status = _SUBSCRIPTION_STATUSES.get(subscription["status"])
    if status is None:
        raise ValueError(f"unknown polar subscription status: {subscription['status']}")
    return status


def is_signature_valid(provider: Provider, raw_body: bytes, headers: dict[str, str], now: float | None = None) -> bool:
    """[Pure-ish] Whether a webhook was signed with that Polar's secret (Standard Webhooks, see the top of this file),
    and isn't older (or newer) than WEBHOOK_TOLERANCE_SECONDS. `headers`: lower-cased names. `now`: for tests."""
    secret = _get_credentials(provider)["webhook_secret"]
    webhook_id, timestamp = headers.get("webhook-id", ""), headers.get("webhook-timestamp", "")
    if not secret or not webhook_id or not timestamp.isdigit():
        return False
    if abs((now if now is not None else time.time()) - int(timestamp)) > WEBHOOK_TOLERANCE_SECONDS:
        return False
    signed = f"{webhook_id}.{timestamp}.".encode() + raw_body
    expected = [
        base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode() for key in _to_signing_keys(secret)
    ]
    # The header lists one or more "v1,<signature>", space separated (several while a secret is being rotated).
    signatures = [entry.partition(",")[2] for entry in headers.get("webhook-signature", "").split()]
    return any(hmac.compare_digest(key_signature, signature) for key_signature in expected for signature in signatures)


def _to_signing_keys(secret: str) -> list[bytes]:
    """[Pure] The HMAC keys a whsec_ secret may stand for -- Polar signs with one of two, depending on the secret:
    the raw bytes of the whole string (its own scheme, which its SDK feeds to Standard Webhooks base64-encoded), or
    Standard Webhooks' own reading, the base64 part after "whsec_" decoded. Like Polar's SDK, we try both."""
    try:
        decoded = [base64.b64decode(secret.removeprefix("whsec_"), validate=True)]
    except ValueError:
        decoded = []
    return [secret.encode(), *decoded]


def _get_credentials(provider: Provider) -> dict:
    if provider == Provider.POLAR:
        return {"token": settings.polar_access_token, "webhook_secret": settings.polar_webhook_secret}
    return {"token": settings.polar_sandbox_access_token, "webhook_secret": settings.polar_sandbox_webhook_secret}


async def _call_api(provider: Provider, method: str, path: str, body: dict | None = None) -> Any:
    token = _get_credentials(provider)["token"]
    if not token:
        raise ProviderError(f"{provider}: no access token configured")
    status, response, _headers = await http.request(
        method,
        f"{_API_URLS[provider]}/{path}",
        json=body,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    if not 200 <= status < 300:
        raise ProviderError(f"{provider} {method} {path}: {status} {pydash.get(response, 'detail')}", status)
    return response
