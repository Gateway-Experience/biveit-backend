# Intentions: our copy of what's been bought, kept in step with Polar. Polar is the source of truth: a purchase is
# always (re)read from Polar and our row overwritten with what it says now -- never patched from a webhook's payload --
# so reading late, twice or out of order can't leave a stale row.
# Two ways in, both through refresh_subscription / refresh_order:
# - webhooks.py, as Polar tells us something changed (the fast path)
# - catch_up_purchases, every PURCHASE_CATCH_UP_SECONDS, for whatever the webhooks missed: Polar retries a failed
#   delivery for a while, but an endpoint it's disabled (or a long outage) loses events for good. It re-reads
#   running purchases we haven't heard about in a while, and looks for paid checkouts that never got a row.
# Both kinds of Polar object land in `subscriptions`: a Polar subscription as a recurring row, an order as a lifetime
# one -- but only an order for the one-time purchase. Polar also makes orders for a subscription's payments (its first
# one, renewals, plan changes); those just refresh their subscription's row.
# Whose a purchase is comes from the metadata our checkout attached, which Polar copies onto the object. Without it
# (bought some other way, e.g. a checkout link from the dashboard), or opened by another deployment of ours (its
# `origin` -- one Polar sandbox can feed prod and dev boxes), there's nothing for us to keep.
# References: migrations/0011_subscriptions.sql (the table), services/polar.py (the objects' shapes).

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import pydash

from src.constants import AnalyticsEvent
from src.modules.payments.constants import (
    CHECKOUT_CATCH_UP_DAYS,
    PURCHASE_STALE_SECONDS,
    RUNNING_STATUSES,
    Billing,
    PaymentEvent,
    Product,
    Provider,
    ProviderObject,
    SubscriptionStatus,
    TargetType,
)
from src.modules.payments.repositories import subscriptions as subscriptions_repo
from src.modules.payments.services import catalog, polar
from src.modules.payments.services.checkouts import get_origin
from src.repositories import events as events_repo
from src.utils import analytics
from src.utils.logging import log_error

# What still needs Polar's word now and then: running ones (a cancelled one runs until its ends_at), and orders not
# paid yet.
_UNSETTLED_STATUSES = [*RUNNING_STATUSES, SubscriptionStatus.CANCELLED, SubscriptionStatus.PENDING]
# Bought: paid, or in a free first period (see _save_purchase).
_PAID_STATUSES = {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING}


async def refresh_subscription(provider: Provider, subscription_ref: str) -> dict | None:
    """Read a Polar subscription and upsert our recurring row from it. None when it isn't ours. [I/O]"""
    fresh = await polar.get_subscription(provider, subscription_ref)
    owner = fresh.get("metadata") or {}
    if not _is_ours(owner):
        return None
    item = catalog.describe_product(provider, fresh.get("product_id") or "") or {}
    status = polar.to_subscription_status(fresh)
    return await _save_purchase(
        {
            "provider": provider,
            "provider_object": ProviderObject.SUBSCRIPTION,
            "provider_ref": subscription_ref,
            "billing": Billing.RECURRING,
            "user_id": int(owner["user_id"]),
            "target_type": TargetType.PROJECT,
            "target_id": int(owner["project_id"]),
            "product": item.get("product") or owner.get("product"),
            "plan": item.get("plan"),
            "product_data": {"domain": owner.get("domain")} if item.get("product") == Product.DOMAIN else {},
            "status": status,
            # It renews at the end of the period only while it's running and not set to cancel.
            "renews_at": _to_datetime(fresh.get("current_period_end")) if status in RUNNING_STATUSES else None,
            "ends_at": _to_datetime(fresh.get("ends_at") or fresh.get("ended_at")),
            "trial_ends_at": _to_datetime(fresh.get("trial_end")),
            "custom_data": owner,
            "snapshot": fresh,
        }
    )


async def refresh_order(provider: Provider, order_ref: str) -> dict | None:
    """Read a Polar order and upsert the row it's about: its subscription's, for one of a subscription's payments; a
    lifetime row, for the one-time purchase. None when it isn't ours (or isn't a purchase of its own). [I/O]"""
    fresh = await polar.get_order(provider, order_ref)
    if fresh.get("subscription_id"):
        return await refresh_subscription(provider, fresh["subscription_id"])
    owner = fresh.get("metadata") or {}
    item = catalog.describe_product(provider, fresh.get("product_id") or "") or {}
    if not _is_ours(owner) or item.get("product") != Product.ONE_TIME:
        return None
    return await _save_purchase(
        {
            "provider": provider,
            "provider_object": ProviderObject.ORDER,
            "provider_ref": order_ref,
            "billing": Billing.LIFETIME,
            "user_id": int(owner["user_id"]),
            "target_type": TargetType.PROJECT,
            "target_id": int(owner["project_id"]),
            "product": Product.ONE_TIME,
            "plan": item["plan"],
            "product_data": {},
            "status": polar.to_order_status(fresh),
            "custom_data": owner,
            "snapshot": fresh,
            # When it was paid; the column's default (now) if Polar didn't say.
            **pydash.omit_by({"created_at": _to_datetime(fresh.get("created_at"))}, pydash.is_none),
        }
    )


async def _save_purchase(row: dict) -> dict:
    """Upsert a purchase row, and tell analytics when this is the moment it got paid for: it's new, or was only
    pending, and now it's active (paid) or trialing (a domain's free first year -- bought, nothing charged yet). Later
    changes (renewals, cancels) aren't purchases. Polar sends several webhooks per purchase and the catch-up re-reads
    too, so the event carries a fixed uuid: should two racing reads both see it as new, PostHog keeps one."""
    key = pydash.pick(row, "provider", "provider_object", "provider_ref")
    previous = pydash.head(await subscriptions_repo.find(key))
    saved = await subscriptions_repo.upsert(row)
    is_just_paid = saved["status"] in _PAID_STATUSES and (
        not previous or previous["status"] == SubscriptionStatus.PENDING
    )
    if is_just_paid:
        analytics.capture(
            saved["user_id"],
            AnalyticsEvent.PAYMENT_SUCCEEDED,
            {
                "project_id": saved["target_id"],
                "product": saved["product"],
                "plan": saved["plan"],
                "status": saved["status"],
                "sandbox": saved["provider"] == Provider.POLAR_SANDBOX,
            },
            {"uuid": str(uuid5(NAMESPACE_URL, f"payment_succeeded:{saved['provider']}:{saved['provider_ref']}"))},
        )
    return saved


async def catch_up_purchases(now: datetime | None = None) -> list[dict]:
    """Bring in what the webhooks missed (see the top of this file), in two parts:
      1. every unsettled row whose updated_at (when we last read it from Polar) is older than PURCHASE_STALE_SECONDS
         is read again -- a webhook refreshes a row whenever it changes, so only the quiet ones are asked about
      2. every checkout of the last CHECKOUT_CATCH_UP_DAYS without a row is looked up by its orders (they carry its
         checkout_id): a paid one brings its subscription / order in
    One that fails is logged and left for the next round; the rest still go. Returns the rows refreshed. `now`: for
    tests. [I/O]"""
    current = now or datetime.now(UTC)
    stale_rows = await subscriptions_repo.find_stale(
        _UNSETTLED_STATUSES, current - timedelta(seconds=PURCHASE_STALE_SECONDS)
    )
    checkouts = await events_repo.find(
        {"type": PaymentEvent.CHECKOUT_CREATED}, created_after=current - timedelta(days=CHECKOUT_CATCH_UP_DAYS)
    )
    known_checkout_ids = {
        str(pydash.get(row, "custom_data.checkout_id"))
        for row in await subscriptions_repo.find_by_checkout_ids([str(checkout["id"]) for checkout in checkouts])
    }
    missing_checkouts = [checkout for checkout in checkouts if str(checkout["id"]) not in known_checkout_ids]
    refreshed_stale = [await _refresh_quietly(_refresh_row, row) for row in stale_rows]
    refreshed_missing = [await _refresh_quietly(_refresh_checkout, checkout) for checkout in missing_checkouts]
    return pydash.compact([*refreshed_stale, *pydash.flatten(refreshed_missing)])


async def _refresh_row(row: dict) -> dict | None:
    """Read one of our rows again from the Polar it was made in."""
    refresh = refresh_subscription if row["provider_object"] == ProviderObject.SUBSCRIPTION else refresh_order
    return await refresh(Provider(row["provider"]), row["provider_ref"])


async def _refresh_checkout(checkout: dict) -> list[dict | None]:
    """Bring in whatever a checkout of ours bought (nothing, if it was never paid)."""
    provider = Provider(checkout["value"].get("provider") or Provider.POLAR)
    orders = await polar.find_checkout_orders(provider, str(checkout["id"]))
    return [await refresh_order(provider, order["id"]) for order in orders]


async def _refresh_quietly(refresh, item: dict):
    """Run one refresh of the catch-up; a failure is logged (the round goes on, the next one tries it again)."""
    try:
        return await refresh(item)
    except Exception as error:
        log_error(error, "a purchase couldn't be caught up; the next round tries again", {"item_id": item.get("id")})
        return None


def _is_ours(metadata: dict) -> bool:
    """[Pure-ish] Whether a purchase was started by one of THIS deployment's checkouts (see the top of this file)."""
    return bool(metadata.get("project_id") and metadata.get("user_id")) and metadata.get("origin") == get_origin()


def _to_datetime(value: str | None) -> datetime | None:
    """[Pure] Polar's ISO-8601 timestamps (Python 3.11+ reads a trailing Z) as aware datetimes."""
    return datetime.fromisoformat(value) if value else None
