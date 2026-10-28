# Intentions: what happens after a launch is paid -- bring the project's site in line with what's been bought, one
# "ensure" step at a time, each looking at what's already there and only doing what's missing:
#   1. a domain bought through us is registered at the registrar (to the user), its A record points at the server,
#      and it's attached to the project -- the active domain if there's none yet
#   2. the user's own domain from the launch is attached (active if there's none yet)
#   3. the site is deployed if it isn't serving its active domain yet (and that domain may be served: a user's own
#      one only once its DNS records check out)
# Re-run on every payment webhook and every launch-status poll, so it gets there eventually -- a domain whose DNS
# shows up hours later goes live on the next poll. Only with hosting (a plan running).
#
# A domain that can't be registered after all (taken between checkout and payment) gets its subscription cancelled
# and its payment refunded (a free-first-year one charged nothing), and the failure recorded: the plan stays, the
# user picks another domain. Steps that talk to the registrar only run until the domain is attached, so polling
# doesn't hammer it.
# References: checkouts.py (the intent this reads), status.py (where it's shown).

import pydash

from src.config import settings
from src.dto.projects import Project
from src.modules import deployments, domains, payments
from src.modules.domains import DomainUnavailable, LiveSpendRefused, RegistrarError
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services.launch import intents
from src.utils import redis
from src.utils.logging import log_error

# One fulfilment per project at a time; the TTL only covers a backend that died mid-way.
_LOCK_TTL_SECONDS = 10 * 60


async def fulfil(project_id: int) -> None:
    """Run the steps (see the top of this file) for a project, unless a run is already going. Never raises: a step
    that fails is logged and retried on the next run. [I/O]"""
    lock_key = f"launch:fulfil:{project_id}"
    if not await redis.set_if_absent(lock_key, _LOCK_TTL_SECONDS):
        return
    try:
        project = pydash.head(await projects_repo.find({"id": project_id}))
        access = await payments.get_project_access(project_id)
        if project is None or not access["can_host"]:
            return
        if access["domain_subscription"]:
            await _ensure_purchased_domain(project, access["domain_subscription"])
        plan_intent = await intents.get_intent(pydash.get(access, "plan_subscription.custom_data.launch_id"))
        if pydash.get(plan_intent, "domain.kind") == "own":
            await _ensure_own_domain(project, plan_intent["domain"]["name"])
        await _ensure_deployed(project)
    except Exception as error:
        log_error(error, f"launch fulfilment of project {project_id} failed", {"project_id": project_id})
    finally:
        await redis.delete(lock_key)


async def handle_payment_webhook(raw_body: bytes, signature: str) -> None:
    """Take in a payment provider webhook, then fulfil the project it was about. Raises payments.InvalidSignature."""
    row = await payments.handle_webhook(raw_body, signature)
    if row and row["target_type"] == payments.TargetType.PROJECT:
        await fulfil(row["target_id"])


async def pay_mock_checkout(checkout_id: str) -> None:
    """Pay a mock checkout (payments_mock): the stand-in provider's webhooks go through the same path as real ones.
    Raises payments.MockCheckoutNotFound."""
    for raw_body, signature in await payments.pay_mock_checkout(checkout_id):
        await handle_payment_webhook(raw_body, signature)


async def _ensure_purchased_domain(project: Project, subscription: dict) -> None:
    """Step 1, for the domain subscription that's running."""
    domain = subscription["product_data"]["domain"]
    attached = await deployments.get_site_domains(project)
    if pydash.find(attached, {"domain": domain}):
        return
    failures = await intents.get_domain_failures(project["id"])
    if pydash.find(failures, {"subscription_ref": subscription["provider_ref"]}):
        return
    launch_id = pydash.get(subscription, "custom_data.launch_id")
    try:
        await _ensure_registered(domain, await intents.get_intent(launch_id), subscription["provider_ref"])
    except DomainUnavailable:
        await payments.cancel_subscription(subscription["provider_ref"])
        await payments.refund_latest_payment(subscription["provider_ref"])
        await intents.record_domain_failure(
            launch_id, project["id"], subscription["provider_ref"], domain, "unavailable"
        )
        return
    await _ensure_a_record(domain)
    has_active = any(row["activated_at"] for row in attached)
    await deployments.attach_domain(project, domain, deployments.SiteDomainKind.PURCHASED, activate=not has_active)


async def _ensure_registered(domain: str, intent: dict | None, subscription_ref: str) -> None:
    """The domain is in our registrar account -- registered now, for a year, to the launch's registrant, unless it
    already is. The subscription id is the idempotency key, so a retry never buys twice. Raises
    DomainUnavailable; LiveSpendRefused when a mock payment would buy a real domain."""
    try:
        await domains.get_domain(domain)
        return
    except RegistrarError as error:
        if error.status != 404:
            raise
    # Intentions: a pretend payment (PAYMENTS_MOCK) must never spend real money -- on prod the registrar is live.
    if settings.payments_mock and domains.is_live():
        raise LiveSpendRefused(f"not registering {domain} on the live registrar for a mock payment")
    registrant = pydash.get(intent, "registrant")
    if not registrant:
        raise ValueError(f"no registrant recorded for {domain}")
    await domains.register_domain(domain, 1, _to_registrar_contact(registrant), f"subscription-{subscription_ref}")


async def _ensure_a_record(domain: str) -> None:
    """The domain's apex A record points at the server (its DNS is at our registrar)."""
    records = await domains.list_records(domain)
    if not pydash.find(records, lambda record: record.get("type") == "A" and not record.get("host")):
        await domains.create_record(domain, {"host": "", "type": "A", "answer": settings.server_public_ip, "ttl": 300})


async def _ensure_own_domain(project: Project, domain: str) -> None:
    """Step 2. A domain that's meanwhile become another project's is left out (the status shows it unattached)."""
    attached = await deployments.get_site_domains(project)
    if pydash.find(attached, {"domain": domain}):
        return
    try:
        has_active = any(row["activated_at"] for row in attached)
        await deployments.attach_domain(project, domain, deployments.SiteDomainKind.EXTERNAL, activate=not has_active)
    except deployments.DomainTaken:
        return


async def _ensure_deployed(project: Project) -> None:
    """Step 3: deploy when the active domain may be served but no deployment since it became active has finished --
    or failed (a failure isn't retried by itself: it shows, and the user redeploys)."""
    active = pydash.find(await deployments.get_site_domains(project), lambda row: row["activated_at"] is not None)
    if active is None or not await deployments.get_served_domains(project):
        return
    history = await deployments.list_deployments(project)
    since_active = [run for run in history if run["started_at"] >= active["activated_at"]]
    if since_active:  # running, done, or failed -- nothing to start
        return
    conversation = await agent_service.get_current_conversation(project["id"], project["user_id"])
    try:
        await deployments.deploy(project, conversation["id"], enforce_cooldown=False)
    except deployments.DeploymentInProgress:
        return


def _to_registrar_contact(registrant: dict) -> dict:
    """[Pure] The launch's registrant (dto/launch.Registrant) in the registrar's contact shape."""
    return pydash.omit_by(
        {
            "firstName": registrant["first_name"],
            "lastName": registrant["last_name"],
            "companyName": registrant.get("company_name") or None,
            "address1": registrant["address1"],
            "city": registrant["city"],
            "state": registrant["state"],
            "zip": registrant["zip"],
            "country": registrant["country"],
            "email": registrant["email"],
            "phone": registrant["phone"],
        },
        pydash.is_none,
    )
