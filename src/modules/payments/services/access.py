# Intentions: what a user may do with a project, derived from what's been bought for it -- never stored. The rules
# (docs/plans/launch-flow.md):
#   can_download  anything was ever paid for the project (a plan, or the one-time purchase), even if it's over
#   can_edit      a plan is running, or a one-time purchase is less than its edit_days old
#   can_host      a plan is running, or ended less than HOSTING_GRACE_DAYS ago
# A plan "runs" while the provider calls it on_trial / active / past_due (still retrying a failed payment), or
# cancelled but not yet at its ends_at.

from datetime import UTC, datetime, timedelta

import pydash

from src.modules.payments.constants import HOSTING_GRACE_DAYS, PLANS, RUNNING_STATUSES, Plan, Product, TargetType
from src.modules.payments.repositories import orders as orders_repo
from src.modules.payments.repositories import subscriptions as subscriptions_repo


async def get_project_access(project_id: int) -> dict:
    """The project's access (to_access), as of now. [I/O: reads]"""
    target = {"target_type": TargetType.PROJECT, "target_id": project_id}
    subscriptions = await subscriptions_repo.find(target)
    orders = await orders_repo.find(target)
    return to_access(subscriptions, orders, datetime.now(UTC))


def to_access(subscriptions: list[dict], orders: list[dict], now: datetime) -> dict:
    """[Pure] From a project's subscriptions and orders (oldest first): {can_download, can_edit, can_host, plan
    (monthly / yearly / None), plan_subscription (the running one), edit_until (a one-time purchase's agent access,
    None with a plan), one_time_order (the latest paid one), domain_subscription (the running one)}."""
    plans = [row for row in subscriptions if row["product"] == Product.PLAN]
    running_plan = pydash.find_last(plans, lambda row: is_running(row, now))
    hosting_plan = running_plan or pydash.find_last(
        plans, lambda row: (_get_ended_at(row) or now) + timedelta(days=HOSTING_GRACE_DAYS) > now
    )
    paid_orders = [row for row in orders if row["product"] == Product.ONE_TIME and row["status"] == "paid"]
    one_time_order = pydash.last(paid_orders)
    edit_until = (
        one_time_order["created_at"] + timedelta(days=PLANS[Plan.ONE_TIME]["edit_days"]) if one_time_order else None
    )
    domains = [row for row in subscriptions if row["product"] == Product.DOMAIN]
    return {
        "can_download": bool(plans or paid_orders),
        "can_edit": bool(running_plan) or bool(edit_until and now < edit_until),
        "can_host": bool(hosting_plan),
        "plan": running_plan["plan"] if running_plan else None,
        "plan_subscription": running_plan,
        "edit_until": None if running_plan else edit_until,
        "one_time_order": one_time_order,
        "domain_subscription": pydash.find_last(domains, lambda row: is_running(row, now)),
    }


def is_running(subscription: dict, now: datetime) -> bool:
    """[Pure] Whether a subscription is in effect right now (see the top of this file)."""
    if subscription["status"] in RUNNING_STATUSES:
        return True
    return subscription["status"] == "cancelled" and bool(subscription["ends_at"]) and subscription["ends_at"] > now


def _get_ended_at(subscription: dict) -> datetime | None:
    """[Pure] When a subscription that's over stopped: its ends_at, else (payments failed for good) when it last
    changed. None while it's still running."""
    if (
        subscription["status"] in RUNNING_STATUSES
        or subscription["status"] == "cancelled"
        and not subscription["ends_at"]
    ):
        return None
    return subscription["ends_at"] or subscription["updated_at"]
