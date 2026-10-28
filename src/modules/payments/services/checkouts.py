# Intentions: starting a purchase -- a checkout link at the provider for one thing (a plan, the one-time purchase, a
# domain), with our ids attached as custom data so its webhooks say whose and what it was: {user_id, project_id,
# product, plan / domain, ...whatever the caller adds (e.g. a launch id)}.

from datetime import UTC, datetime, timedelta

from src.dto.projects import Project
from src.dto.users import User
from src.modules.payments.constants import CHECKOUT_TTL_SECONDS, PLANS, Plan, Product
from src.modules.payments.services import catalog, lemonsqueezy


async def create_plan_checkout(user: User, project: Project, plan: Plan, extra_data: dict) -> str:
    """A checkout link for a plan (monthly / yearly) or the one-time purchase. Returns its URL. [I/O]"""
    custom_data = {**extra_data, "product": PLANS[plan]["product"], "plan": plan}
    return await _create(user, project, PLANS[plan]["variant_id"], custom_data)


async def create_domain_checkout(
    user: User, project: Project, domain: str, price_per_year: int, free_first_year: bool, extra_data: dict
) -> str:
    """A checkout link for a domain's yearly subscription at one of our prices -- the free-first-year (trial)
    variant when `free_first_year`. Returns its URL. [I/O]"""
    variant_id = catalog.get_domain_variant(price_per_year, free_first_year)
    return await _create(user, project, variant_id, {**extra_data, "product": Product.DOMAIN, "domain": domain})


async def _create(user: User, project: Project, variant_id: str, custom_data: dict) -> str:
    expires_at = (datetime.now(UTC) + timedelta(seconds=CHECKOUT_TTL_SECONDS)).isoformat()
    owner = {"user_id": user["id"], "project_id": project["id"]}
    return await lemonsqueezy.create_checkout(variant_id, user["username"] or "", {**custom_data, **owner}, expires_at)
