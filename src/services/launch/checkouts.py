# Intentions: starting a purchase from the launch dialog -- checking the user's choice is allowed for this project
# right now (never trusting what the dialog showed: prices and availability are looked up again), then getting the
# provider's checkout link. One thing per checkout, since the provider has no cart: a plan (or the one-time
# purchase), or a domain to buy. Each is recorded by payments (payment.checkout_created) with only its own part:
# the plan's just the plan, the domain's also who it's registered to (fulfilment reads that back once it's paid)
# and, when discounted, the discount: its terms and the paid yearly plan that earned it (its checkout and
# subscription) -- so the domain's checkout explains itself.
# A user's own domain isn't bought, so it never comes through here (status.add_own_domain).
#
# Also: moving a running monthly plan to yearly (a plan change on the same subscription, no checkout page).
#
# The rules (docs/launchflow.txt):
# - only an approved site (build_approved_at) can be launched
# - a plan doesn't need a domain: one without just waits for one (the status asks for it)
# - a domain is only bought for a project that's had a recurring plan -- the dialog buys the plan first, then the
#   domain, so the domain's checkout never exists before a paid plan does
# - the domain discount (today the first year free, if its price point allows) needs a running yearly plan, and a
#   project only ever gets it once -- a domain that failed to register doesn't use it up. Decided from what's PAID,
#   never from what a checkout meant to buy. Upgrading monthly -> yearly doesn't come with it.
# - at most one domain bought through us per project at a time
# References: fulfilment.py (what happens once paid, reading these checkouts back).

import pydash

from src.dto.launch import LaunchCheckoutRequest
from src.dto.projects import Project
from src.dto.users import User
from src.modules import deployments, payments
from src.services.launch import access as launch_access
from src.services.launch import offers


class LaunchRefused(Exception):
    """A launch request that isn't allowed (now). `code`: what the API answers (NOT_APPROVED, ALREADY_SUBSCRIBED,
    PRICE_CHANGED...); `status`: its HTTP status; `details`: anything the FE needs to recover (a new price...)."""

    def __init__(self, code: str, status: int = 409, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


async def start_checkout(project: Project, user: User, request: LaunchCheckoutRequest) -> dict:
    """Validate the choice and return its checkout: {product: plan | domain, url}. Raises LaunchRefused."""
    if not project.get("build_approved_at"):
        raise LaunchRefused("NOT_APPROVED")
    access = await launch_access.get_launch_access(project["id"])
    if request.plan:
        _check_plan_choice(request, access)
        plan_checkout = await payments.create_plan_checkout(user, project, payments.Plan(request.plan))
        return {"product": "plan", "url": plan_checkout["url"]}
    domain = await _check_domain_choice(project, request, access, payments.is_sandbox(user))
    domain_checkout = await payments.create_domain_checkout(
        user,
        project,
        domain["name"],
        domain["price_per_year"],
        bool(domain["discount"]),
        {"registrant": request.registrant.model_dump() if request.registrant else None, "discount": domain["discount"]},
    )
    return {"product": "domain", "url": domain_checkout["url"]}


def _check_plan_choice(request: LaunchCheckoutRequest, access: dict) -> None:
    """[Pure] Whether the plan fits what's already been bought. Raises LaunchRefused."""
    if request.plan == payments.Plan.ONE_TIME and access["can_download"]:
        raise LaunchRefused("ALREADY_PURCHASED")
    if request.plan in {payments.Plan.MONTHLY, payments.Plan.YEARLY} and access["plan_subscription"]:
        raise LaunchRefused("ALREADY_SUBSCRIBED")


async def _check_domain_choice(project: Project, request: LaunchCheckoutRequest, access: dict, sandbox: bool) -> dict:
    """The domain to buy, checked live: {name, price_per_year, discount: {terms, plan_checkout_id,
    plan_subscription_ref} | None -- the yearly plan that earns it}. Raises LaunchRefused."""
    if not access["has_had_plan"]:
        raise LaunchRefused("PLAN_REQUIRED")
    if access["domain_subscription"]:
        raise LaunchRefused("DOMAIN_ALREADY_BOUGHT")
    try:
        name = deployments.normalize_domain(pydash.get(request, "domain.name"))
    except deployments.InvalidDomain as error:
        raise LaunchRefused("DOMAIN_INVALID", 400) from error
    offer = await offers.quote_domain(name, sandbox)
    if not offer["available"]:
        raise LaunchRefused("DOMAIN_UNAVAILABLE")
    if not offer["sold"]:
        raise LaunchRefused("DOMAIN_NOT_SOLD")
    shown_price = pydash.get(request, "domain.price_per_year")
    if shown_price is not None and shown_price != offer["price_per_year"]:
        raise LaunchRefused("PRICE_CHANGED", details={"price_per_year": offer["price_per_year"]})
    is_discounted = offer["discountable"] and await has_domain_discount(project, access)
    plan_checkout_ref = str(pydash.get(access, "plan_subscription.custom_data.checkout_id") or "")
    discount = {
        "terms": payments.DOMAIN_DISCOUNT_TERMS,
        "plan_checkout_id": int(plan_checkout_ref) if plan_checkout_ref.isdigit() else None,
        "plan_subscription_ref": pydash.get(access, "plan_subscription.provider_ref"),
    }
    return {"name": name, "price_per_year": offer["price_per_year"], "discount": discount if is_discounted else None}


async def has_domain_discount(project: Project, access: dict) -> bool:
    """Whether a domain bought now gets the domain discount: a yearly plan is running, and the project has never had
    a domain with it -- one that failed to register doesn't count. Today the discount is a trial, so a domain that
    had it is one with trial_ends_at. [I/O: reads]"""
    if pydash.get(access, "plan_subscription.plan") != payments.Plan.YEARLY:
        return False
    domains = await payments.find_subscriptions(
        {"target_type": payments.TargetType.PROJECT, "target_id": project["id"], "product": payments.Product.DOMAIN}
    )
    failed_refs = {failure["subscription_ref"] for failure in access["domain_failures"]}
    return not any(row["trial_ends_at"] and row["provider_ref"] not in failed_refs for row in domains)


async def upgrade_plan(project: Project) -> None:
    """Move the running monthly plan to yearly: the same subscription, the provider charging the difference
    (payments.change_plan); its webhook updates the row. Raises LaunchRefused (NOT_UPGRADABLE: no running monthly
    plan, or it's been cancelled -- it only runs out its period -- or the provider turned the change down, e.g. the
    payment failed)."""
    plan = (await launch_access.get_launch_access(project["id"]))["plan_subscription"]
    if not plan or plan["plan"] != payments.Plan.MONTHLY or plan["status"] == payments.SubscriptionStatus.CANCELLED:
        raise LaunchRefused("NOT_UPGRADABLE")
    try:
        await payments.change_plan(plan, payments.Plan.YEARLY)
    except payments.ProviderError as error:
        if not 400 <= error.status < 500:
            raise
        raise LaunchRefused("NOT_UPGRADABLE") from error
