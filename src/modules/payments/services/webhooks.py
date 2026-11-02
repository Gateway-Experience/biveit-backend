# Intentions: taking in Polar's webhooks -- the fast way our copy of what's been bought changes. A webhook is a
# TRIGGER, not the data: its raw payload is appended to `events` (payment.webhook, the history), then the object it's
# about is read fresh from the same Polar into our row (services/purchases.py). One about something that isn't ours
# (no metadata of ours, another deployment's) is recorded and nothing else happens.
# References: services/purchases.py (the refresh, and the catch-up for what webhooks miss), services/polar.py.

import json

from src.modules.payments.constants import PaymentEvent, Provider
from src.modules.payments.services import polar, purchases
from src.repositories import events as events_repo


class InvalidSignature(Exception):
    """A webhook whose signature doesn't match -- not from that Polar."""


async def handle_webhook(provider: Provider, raw_body: bytes, headers: dict[str, str]) -> dict | None:
    """Record a webhook from that Polar and bring the row it's about up to date. `headers`: lower-cased names. Returns
    that row, or None when it's about nothing we keep. Raises InvalidSignature."""
    if not polar.is_signature_valid(provider, raw_body, headers):
        raise InvalidSignature("webhook signature doesn't match")
    payload = json.loads(raw_body)
    event_type, data = payload.get("type") or "", payload.get("data") or {}
    metadata = data.get("metadata") or {}
    await events_repo.insert(
        {
            "type": PaymentEvent.WEBHOOK,
            "subject_type": "user",
            "subject_ref": str(metadata.get("user_id") or ""),
            "direct_object_type": event_type.partition(".")[0],  # subscription | order | refund | checkout | ...
            "direct_object_ref": str(data.get("id") or ""),
            "grouping_type": "project",
            "grouping_ref": str(metadata.get("project_id") or ""),
            "value": {**payload, "provider": provider, "webhook_id": headers.get("webhook-id")},
        }
    )
    try:
        if event_type.startswith("subscription."):
            return await purchases.refresh_subscription(provider, data["id"])
        if event_type.startswith("order."):
            return await purchases.refresh_order(provider, data["id"])
        if event_type.startswith("refund."):
            return await purchases.refresh_order(provider, data["order_id"])
    except polar.ProviderError as error:
        # Intentions: an object that Polar itself doesn't know (404) never will, so there's nothing to keep -- answering
        # with an error would only make Polar retry it, and enough failures in a row disable the whole endpoint.
        if error.status != 404:
            raise
    return None
