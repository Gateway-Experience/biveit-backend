# Intentions: what a user may do with a project, derived from what's been bought for it -- never stored. The rules
# (docs/plans/launch-flow.md):
#   can_download  anything was ever paid for the project (a plan, or the one-time purchase), even if it's over
#   can_edit      a plan is running or in its grace period, or a one-time purchase is less than its edit_days old
#   can_host      a plan is running, or in its grace period (is_in_grace: ended less than GRACE_DAYS ago)
#   has_had_plan  a plan was ever paid for (running or not) -- what a domain purchase needs
# A purchase "runs" while it's trialing / active / past_due (the provider still retrying a failed payment), or
# cancelled but not yet at its ends_at -- the same for every provider and billing (constants.SubscriptionStatus).
# Nothing counts before its created_at: an admin-set plan can start later (its created_at is its start). Soft-deleted
# rows never get here (the repository leaves them out).

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
    started = [row for row in subscriptions if _has_started(row, now)]
    oldest_first = sorted(started, key=lambda row: (row["created_at"], row["id"]))
    plans = [row for row in oldest_first if row["product"] == Product.PLAN]
    running_plan = pydash.find_last(plans, lambda row: is_running(row, now))
    has_plan_or_grace = any(is_running(row, now) or is_in_grace(row, now) for row in plans)
    paid_one_times = [row for row in oldest_first if row["product"] == Product.ONE_TIME and is_running(row, now)]
    one_time = pydash.last(paid_one_times)
    # An admin-set one-time purchase says how long it gives edits (ends_at); a bought one, edit_days from paying.
    edit_until = (
        (one_time.get("ends_at") or one_time["created_at"] + timedelta(days=PLANS[Plan.ONE_TIME]["edit_days"]))
        if one_time
        else None
    )
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
        # A domain that failed to register has its subscription revoked (ended now) and refunded, so it stops
        # running here once its webhook lands; the upper layer also leaves out ones with a domain.failed event, which
        # covers the moment before that.
        "domain_subscription": pydash.find_last(domain_subs, lambda row: is_running(row, now)),
    }


def is_running(subscription: dict, now: datetime) -> bool:
    """[Pure] Whether a subscription is in effect right now (see the top of this file)."""
    if not _has_started(subscription, now):
        return False
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
    if (
        is_running(subscription, now)
        or subscription["status"] not in GRACE_STATUSES
        or not _has_started(subscription, now)
    ):
        return False
    ended_at = subscription["ends_at"] or subscription["updated_at"]
    return ended_at + timedelta(days=GRACE_DAYS) > now


def _has_started(subscription: dict, now: datetime) -> bool:
    """[Pure] Whether its created_at (an admin-set plan's start) has come; one without counts as started."""
    return not subscription.get("created_at") or subscription["created_at"] <= now
