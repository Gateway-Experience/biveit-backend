# Intentions: what a user may do with a project, derived from what's been bought for it -- never stored. The rules
# (docs/plans/launch-flow.md):
#   can_download  anything was ever paid for the project (a plan, or the one-time purchase), even if it's over
#   can_edit      a plan is running or in its grace period, or a one-time purchase is less than its edit_days old
#   can_host      a plan is running, or in its grace period (is_in_grace: ended less than GRACE_DAYS ago)
#   has_had_plan  a plan was ever paid for (running or not) -- what a domain purchase needs
# A purchase "runs" while it's trialing / active / past_due (the provider still retrying a failed payment), or
# cancelled but not yet at its ends_at -- the same for every provider and billing (constants.SubscriptionStatus).

from datetime import UTC, datetime, timedelta

import pydash

from src.modules.payments.constants import (
    GRACE_DAYS,
    GRACE_STATUSES,
    PLANS,
    RUNNING_STATUSES,
    Plan,
    Product,
    SubscriptionStatus,
    TargetType,
)
from src.modules.payments.repositories import subscriptions as subscriptions_repo


async def get_project_access(project_id: int) -> dict:
    """The project's access (to_access), as of now. [I/O: reads]"""
    subscriptions = await subscriptions_repo.find({"target_type": TargetType.PROJECT, "target_id": project_id})
    return to_access(subscriptions, datetime.now(UTC))


def to_access(subscriptions: list[dict], now: datetime) -> dict:
    """[Pure] From a project's subscriptions (any order): {can_download, can_edit, can_host, has_had_plan,
    plan_subscription (the running plan, None without one -- its `plan` says monthly / yearly), edit_until (a
    one-time purchase's agent access, None with a plan), one_time_subscription (the latest paid one),
    domain_subscription (the running one)}. Where several qualify, the latest bought wins."""
    oldest_first = sorted(subscriptions, key=lambda row: (row["created_at"], row["id"]))
    plans = [row for row in oldest_first if row["product"] == Product.PLAN]
    running_plan = pydash.find_last(plans, lambda row: is_running(row, now))
    has_plan_or_grace = any(is_running(row, now) or is_in_grace(row, now) for row in plans)
    paid_one_times = [row for row in oldest_first if row["product"] == Product.ONE_TIME and is_running(row, now)]
    one_time = pydash.last(paid_one_times)
    edit_until = one_time["created_at"] + timedelta(days=PLANS[Plan.ONE_TIME]["edit_days"]) if one_time else None
    domain_subs = [row for row in oldest_first if row["product"] == Product.DOMAIN]
    return {
        "can_download": bool(plans or paid_one_times),
        # Plan or One-time.
        "can_edit": has_plan_or_grace or bool(edit_until and now < edit_until),
        "can_host": has_plan_or_grace,
        "has_had_plan": bool(plans),
        "plan_subscription": running_plan,
        "edit_until": None if running_plan else edit_until,
        "one_time_subscription": one_time,
        # The data model supports many bought dodmains per project, but for now let's assume users can only buy 1 for each proj.
        # The domain subscription existing doesn't mean it's effective. It could have been refunded
        # (the subscription is only cancelled and refunded then, but the subscription is still in place).
        # There is another combined check with domain failed event in the upper layer.
        "domain_subscription": pydash.find_last(domain_subs, lambda row: is_running(row, now)),
    }


def is_running(subscription: dict, now: datetime) -> bool:
    """[Pure] Whether a subscription is in effect right now (see the top of this file)."""
    if subscription["status"] in RUNNING_STATUSES:
        return True
    return (
        subscription["status"] == SubscriptionStatus.CANCELLED
        and bool(subscription["ends_at"])
        and subscription["ends_at"] > now
    )


def is_in_grace(subscription: dict, now: datetime) -> bool:
    """[Pure] Whether a subscription has stopped but is still within GRACE_DAYS of it -- false while it runs. Only one
    that was in effect and stopped (ended, or cancelled and run out) gets a grace period: never paid (pending) or
    money back (refunded) gets none. It stopped at its ends_at, or, with none (payments failed for good), when the
    provider last changed it. Which entitlements carry on through grace is the caller's call (see the top of this
    file)."""
    if is_running(subscription, now) or subscription["status"] not in GRACE_STATUSES:
        return False
    ended_at = subscription["ends_at"] or subscription["updated_at"]
    return ended_at + timedelta(days=GRACE_DAYS) > now
