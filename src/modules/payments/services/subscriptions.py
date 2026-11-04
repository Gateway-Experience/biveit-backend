# Intentions: acting on a project's subscriptions once they exist:
#   - at the provider it was bought with -- ending one now, refunding its latest payment. Each change comes back to us
#     as webhooks, which update the row.
#   - by hand, from the admin project page -- a project's plan (monthly / yearly / one-time) an admin sets, edits or
#     takes away. "The project's plan" is its latest plan or one-time row that isn't deleted, expired or not. An admin
#     can create one only when there's none; can edit only one an admin created (a bought one mirrors Polar, which
#     would just write it back); and can soft-delete either kind. Deleting a bought one doesn't stop Polar charging --
#     that's done in Polar's dashboard. An admin's row is an ordinary row (provider ADMIN): a plan runs as a cancelled
#     subscription until its end (so it ends with the usual grace period), a one-time one gives edits until its end.

import uuid
from datetime import datetime

import pydash

from src.modules.payments.constants import (
    Billing,
    Plan,
    Product,
    Provider,
    ProviderObject,
    SubscriptionStatus,
    TargetType,
)
from src.modules.payments.repositories import subscriptions as subscriptions_repo
from src.modules.payments.services import polar


class PlanExists(Exception):
    """The project already has a plan (an admin edits or deletes that one instead)."""


class NoPlan(Exception):
    """The project has no plan to edit or delete."""


class PlanNotEditable(Exception):
    """The project's plan was bought, not set by an admin: it can only be deleted."""


class InvalidPlanPeriod(Exception):
    """A plan has to end after it starts."""


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


# --- set by an admin ---


async def get_project_plan(project_id: int) -> dict | None:
    """The project's plan as an admin manages it (see the top of this file), or None. [I/O: reads]"""
    rows = await subscriptions_repo.find({"target_type": TargetType.PROJECT, "target_id": project_id})
    return pydash.find_last(rows, lambda row: row["product"] in {Product.PLAN, Product.ONE_TIME})


async def create_admin_plan(project: dict, plan: Plan, starts_at: datetime, ends_at: datetime, admin_id: int) -> dict:
    """Give a project a plan by hand, from `starts_at` to `ends_at`. `admin_id`: who set it (kept in custom_data).
    Raises PlanExists, InvalidPlanPeriod. [I/O]"""
    if await get_project_plan(project["id"]):
        raise PlanExists(project["id"])
    return await subscriptions_repo.insert(
        {
            "provider": Provider.ADMIN,
            "provider_ref": uuid.uuid4().hex,
            "user_id": project["user_id"],
            "target_type": TargetType.PROJECT,
            "target_id": project["id"],
            "custom_data": {"set_by": admin_id},
            **_to_admin_plan_columns(plan, starts_at, ends_at),
        }
    )


async def update_admin_plan(project_id: int, plan: Plan, starts_at: datetime, ends_at: datetime) -> dict:
    """Change the project's admin-set plan: which plan, and its period. Raises NoPlan, PlanNotEditable,
    InvalidPlanPeriod. [I/O]"""
    current = await get_project_plan(project_id)
    if current is None:
        raise NoPlan(project_id)
    if current["provider"] != Provider.ADMIN:
        raise PlanNotEditable(project_id)
    updated = await subscriptions_repo.update_one(current["id"], _to_admin_plan_columns(plan, starts_at, ends_at))
    assert updated is not None  # it was just read
    return updated


async def delete_project_plan(project_id: int) -> None:
    """Soft-delete the project's plan, whoever made it -- it stops counting right away, no grace period. Raises
    NoPlan. [I/O]"""
    current = await get_project_plan(project_id)
    if current is None:
        raise NoPlan(project_id)
    await subscriptions_repo.delete(current["id"])


def _to_admin_plan_columns(plan: Plan, starts_at: datetime, ends_at: datetime) -> dict:
    """[Pure] The columns that make an admin's row the plan asked for. Raises InvalidPlanPeriod."""
    if ends_at <= starts_at:
        raise InvalidPlanPeriod(starts_at, ends_at)
    if plan == Plan.ONE_TIME:
        kind = {"product": Product.ONE_TIME, "billing": Billing.LIFETIME, "provider_object": ProviderObject.ORDER,
                "status": SubscriptionStatus.ACTIVE}  # fmt: skip
    else:
        kind = {"product": Product.PLAN, "billing": Billing.RECURRING, "provider_object": ProviderObject.SUBSCRIPTION,
                "status": SubscriptionStatus.CANCELLED}  # fmt: skip
    return {**kind, "plan": plan, "created_at": starts_at, "ends_at": ends_at, "renews_at": None}
