# Intentions: starting a launch -- checking the user's choice is allowed for this project right now (never trusting
# what the dialog showed: prices and availability are looked up again), recording it as a `launch.checkout_created`
# event (the launch's intent: plan, domain, the registrant for a domain we'll buy), and getting the provider's
# checkout links -- one per thing bought, since the provider has no cart: the plan, then the domain.
#
# The rules (docs/plans/launch-flow.md):
# - only an approved site (build_approved_at) can be launched
# - a plan (monthly / yearly) needs a domain: bought through us, or the user's own; the one-time purchase none
# - a domain bought together with the yearly plan -- in the same launch -- is free for its first year (if its price
#   point allows); finishing that launch later (an abandoned or failed domain checkout) keeps that
# - at most one domain bought through us per project at a time
# References: fulfilment.py (what happens once paid, reading the intent recorded here).

import uuid

import pydash

from src.dto.launch import LaunchCheckoutRequest
from src.dto.projects import Project
from src.dto.users import User
from src.modules import deployments, payments
from src.services.launch import fulfilment, intents, offers
from src.utils.common import spawn


class LaunchRefused(Exception):
    """A launch request that isn't allowed (now). `code`: what the API answers (NOT_APPROVED, ALREADY_SUBSCRIBED,
    PRICE_CHANGED...); `status`: its HTTP status; `details`: anything the FE needs to recover (a new price...)."""

    def __init__(self, code: str, status: int = 409, details: dict | None = None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.details = details or {}


async def start_checkout(project: Project, user: User, request: LaunchCheckoutRequest) -> dict:
    """Validate and record a launch, and return {launch_id, checkouts: [{product, url}]} -- in the order to pay them.
    Switching a running plan's site to the user's own domain needs no payment: it's attached and made active right
    away (no checkouts). Raises LaunchRefused."""
    if not project.get("build_approved_at"):
        raise LaunchRefused("NOT_APPROVED")
    access = await payments.get_project_access(project["id"])
    previous_intent = await intents.get_intent(request.launch_id)
    if previous_intent and previous_intent.get("project_id") != project["id"]:
        raise LaunchRefused("UNKNOWN_LAUNCH", 404)
    _check_plan_choice(request, access)
    domain = await _check_domain_choice(project, request, access, previous_intent)
    launch_id = request.launch_id or uuid.uuid4().hex
    intent = {
        "project_id": project["id"],
        "plan": request.plan or (previous_intent or {}).get("plan"),
        "domain": domain,
        "registrant": request.registrant.model_dump() if request.registrant else None,
    }
    await intents.record_intent(launch_id, user, project, intent)
    extra_data = {"launch_id": launch_id}
    plan_checkouts = (
        [{"product": "plan", "url": await payments.create_plan_checkout(user, project, payments.Plan(request.plan), extra_data)}]
        if request.plan
        else []
    )  # fmt: skip
    domain_checkouts = (
        [{"product": "domain", "url": await payments.create_domain_checkout(
            user, project, domain["name"], domain["price_per_year"], domain["free_first_year"], extra_data
        )}]
        if domain and domain["kind"] == "buy"
        else []
    )  # fmt: skip
    if domain and domain["kind"] == "own" and request.plan is None:
        # Plan already running: switch the site to the user's own domain now. Fulfilment serves it once its DNS
        # checks out.
        await deployments.attach_domain(project, domain["name"], deployments.SiteDomainKind.EXTERNAL, activate=True)
        spawn(fulfilment.fulfil(project["id"]))
    return {"launch_id": launch_id, "checkouts": [*plan_checkouts, *domain_checkouts]}


def _check_plan_choice(request: LaunchCheckoutRequest, access: dict) -> None:
    """[Pure] Whether the plan part fits what's already been bought. Raises LaunchRefused."""
    if request.plan == payments.Plan.ONE_TIME and access["can_download"]:
        raise LaunchRefused("ALREADY_PURCHASED")
    if request.plan in {payments.Plan.MONTHLY, payments.Plan.YEARLY}:
        if access["plan"]:
            raise LaunchRefused("ALREADY_SUBSCRIBED")
        if request.domain is None:
            raise LaunchRefused("DOMAIN_REQUIRED", 400)
    if request.plan is None and not access["plan"]:
        raise LaunchRefused("PLAN_REQUIRED")


async def _check_domain_choice(
    project: Project, request: LaunchCheckoutRequest, access: dict, previous_intent: dict | None
) -> dict | None:
    """The domain part, checked live: {kind, name, price_per_year, free_first_year} (None without one). Raises
    LaunchRefused."""
    if request.domain is None:
        return None
    if request.domain.kind == "own":
        check = await deployments.check_external_domain(project, request.domain.name)
        if check["blocked"]:
            raise LaunchRefused(f"DOMAIN_{check['blocked'].upper()}", 400 if check["blocked"] == "invalid" else 409)
        return {"kind": "own", "name": check["domain"], "price_per_year": None, "free_first_year": False}
    if access["domain_subscription"]:
        raise LaunchRefused("DOMAIN_ALREADY_BOUGHT")
    try:
        name = deployments.normalize_domain(request.domain.name)
    except deployments.InvalidDomain as error:
        raise LaunchRefused("DOMAIN_INVALID", 400) from error
    offer = await offers.quote_domain(name)
    if not offer["available"]:
        raise LaunchRefused("DOMAIN_UNAVAILABLE")
    if not offer["sold"]:
        raise LaunchRefused("DOMAIN_NOT_SOLD")
    if request.domain.price_per_year is not None and request.domain.price_per_year != offer["price_per_year"]:
        raise LaunchRefused("PRICE_CHANGED", details={"price_per_year": offer["price_per_year"]})
    launch_plan = request.plan or (previous_intent or {}).get("plan")
    bought_with_yearly = launch_plan == payments.Plan.YEARLY and (
        request.plan or pydash.get(previous_intent, "domain.kind") == "buy"
    )
    return {
        "kind": "buy",
        "name": name,
        "price_per_year": offer["price_per_year"],
        "free_first_year": bool(offer["free_first_year_eligible"] and bought_with_yearly),
    }
