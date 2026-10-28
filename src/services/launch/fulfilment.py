# Intentions: what happens after a launch is paid -- bring the project's site in line with what's been bought, one
# "ensure" step at a time, each looking at what's already there and only doing what's missing:
#   1. a domain bought through us is registered at the registrar (to the user), its A record points at the server,
#      and it's attached to the project -- the active domain if there's none yet
#   2. the user's own domain from the launch is attached, and made the active one -- right away if there's none
#      yet, else once its DNS checks out (a site already served somewhere stays there until then)
#   3. the site is deployed if it isn't serving its active domain yet (and that domain may be served: a user's own
#      one only once its DNS records check out)
# Re-run on every payment webhook, every launch-status poll, and every LAUNCH_SWEEP_SECONDS for every project
# with a running plan (fulfil_running_plans) -- a domain whose DNS shows up hours later goes live with nobody
# watching. Only with hosting (a plan running).
#
# A domain that can't be registered after all (taken between checkout and payment) gets its subscription cancelled
# and its payment refunded (a free-first-year one charged nothing), and the failure recorded: the plan stays, the
# user picks another domain. Steps that talk to the registrar only run until the domain is attached, so polling
# doesn't hammer it.
# References: checkouts.py (the intent this reads), status.py (where it's shown).

from datetime import UTC, datetime

import pydash

from src.config import settings
from src.dto.projects import Project
from src.modules import deployments, domains, payments
from src.modules.domains import DomainUnavailable, LiveSpendRefused, RegistrarError
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services.launch import access as launch_access
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
        access = await launch_access.get_launch_access(project_id)
        # Nothing to host without a running plan (or its grace period): a one-time purchase never gets a site.
        if project is None or not access["can_host"]:
            return
        if access["domain_subscription"]:
            await _ensure_purchased_domain(project, access["domain_subscription"])
        # Intentions: what the launch that bought the plan set out to do (its checkout recorded it, keyed by the
        # launch id the plan's checkout carried). A user's own domain isn't paid for, so this is the only place
        # it's known from.
        plan_intent = await intents.get_intent(pydash.get(access, "plan_subscription.custom_data.launch_id"))
        if pydash.get(plan_intent, "domain.kind") == "own":
            await _ensure_own_domain(project, pydash.get(plan_intent, "domain.name"))
        await _ensure_deployed(project)
    except Exception as error:
        log_error(error, f"launch fulfilment of project {project_id} failed", {"project_id": project_id})
    finally:
        await redis.delete(lock_key)


async def fulfil_running_plans() -> None:
    """Fulfil every project whose plan is running, one at a time (the periodic sweep, see the top of this file).
    Cheap for a launch that's done: no registrar or DNS lookups once its active domain has been deployed. [I/O]"""
    now = datetime.now(UTC)
    plans = await payments.find_subscriptions({"product": payments.Product.PLAN})
    project_ids = pydash.uniq(
        [
            row["target_id"]
            for row in plans
            if row["target_type"] == payments.TargetType.PROJECT and payments.is_running(row, now)
        ]
    )
    for project_id in project_ids:  # in turn, not all at once: each may start a build
        await fulfil(project_id)


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
    """Step 1: the domain the user paid for (its running domain subscription) ends up registered to them, pointing
    at our server, and attached to the project -- the active domain if there's none yet. In order:
      1. already attached -> done (this is also what keeps later runs from calling the registrar at all)
      2. register it at the registrar, with the registrant from the launch that bought it
      3. its A record -> our server
      4. attach it
    If it can't be registered (someone took it between checkout and payment), the user gets their money back for
    the domain only: the subscription is cancelled, its payment refunded, and the failure recorded -- the status
    then asks for another domain, and the launch's access (access.py) no longer counts this subscription, so it
    never comes back here. The plan stays."""
    domain = subscription["product_data"]["domain"]
    attached = await deployments.get_site_domains(project)
    if pydash.find(attached, {"domain": domain}):
        return
    # The launch that bought it -- its checkout recorded who the domain is registered to.
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
    # Active only if the site has no domain yet: one bought later (to add to a live site) waits for the user to
    # switch to it.
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
    """Step 2: the user's own domain chosen in the launch ends up attached to the project, and the site moves to
    it -- right away if the site has no domain yet, else once its DNS records check out (a site already served on
    another domain stays there until then). Never moves it back if the user has switched domains since. A domain
    that's meanwhile become another project's is left out (the status shows it unattached)."""
    attached = await deployments.get_site_domains(project)
    row = pydash.find(attached, {"domain": domain})
    active = pydash.find(attached, lambda other: other["activated_at"] is not None)
    if row and row["activated_at"]:
        return
    # Intentions: the launch chose this domain, so the site moves to it -- unless the user has switched domains
    # since it was attached (then that choice wins and it stays put), and only once its DNS checks out.
    switched_since = bool(row and active and active["activated_at"] > row["created_at"])
    if row and switched_since:
        return
    activate = active is None or (await deployments.check_external_domain(project, domain))["verified"]
    if row and not activate:
        return
    try:
        await deployments.attach_domain(project, domain, deployments.SiteDomainKind.EXTERNAL, activate=activate)
    except deployments.DomainTaken:
        return


async def _ensure_deployed(project: Project) -> None:
    """Step 3: the site gets deployed on its active domain, once that domain may be served. Deploys at most once
    per activation: a deployment that's running, done or failed since then means there's nothing to start (a
    failure isn't retried by itself -- it shows, and the user redeploys)."""
    active = pydash.find(await deployments.get_site_domains(project), lambda row: row["activated_at"] is not None)
    if active is None:
        return
    # Intentions: the domain a site serves is read when a deployment rolls out, so one started before the domain
    # became active doesn't serve it -- only a later one counts.
    history = await deployments.list_deployments(project)
    if any(run["started_at"] >= active["activated_at"] for run in history):
        return
    # After the history on purpose: for a user's own domain this is a DNS lookup, and the sweep runs it for every
    # live site.
    if not await deployments.get_served_domains(project):
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
