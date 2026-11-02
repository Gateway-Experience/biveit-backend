# Intentions: acting on a purchase we already have (its subscriptions row) at the provider it was made with -- ending
# one now, refunding its latest payment. Each change comes back to us as webhooks, which update the row.

import pydash

from src.modules.payments.constants import Provider
from src.modules.payments.services import polar


async def revoke_subscription(subscription: dict) -> None:
    """End a subscription now -- not at the end of its period. Refunds nothing (refund_latest_payment). [I/O]"""
    await polar.revoke_subscription(Provider(subscription["provider"]), subscription["provider_ref"])


async def refund_latest_payment(subscription: dict, reason: str = "service_disruption") -> None:
    """Refund what's left of a subscription's most recent paid payment (a trial has none: nothing happens).
    `reason`: Polar's refund reason. [I/O]"""
    provider = Provider(subscription["provider"])
    orders = await polar.find_subscription_orders(provider, subscription["provider_ref"])
    latest_paid = pydash.find(orders, lambda order: order.get("paid") and order.get("refundable_amount"))
    if latest_paid:
        await polar.refund_order(provider, latest_paid, reason)
