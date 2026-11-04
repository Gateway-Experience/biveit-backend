# Intentions: what happens after a purchase is paid -- bring the project's site in line with what's been bought,
# one "ensure" step at a time, each looking at what's already there and only doing what's missing (the domain terms
# -- attached, servable, active -- are docs/launchflow.txt's "Domains"):
#   1. a domain bought through us is registered at the registrar (to the user), its A record points at the server,
#      and it's attached to the project
#   2. the site has an active domain: only when it has none, the most recently attached one gets ONE automatic
#      attempt to be set up and made active (deployments.activate_domain -- the site's first deployment, or a setup
#      on the live commit). A user's own: right away, records ready or not (missing ones fail it at once). One we
#      bought: once public DNS points it at us, for up to PURCHASED_DOMAIN_DNS_WAIT_SECONDS. A failed attempt is
#      never retried from here: the user retries. And an active domain is never switched away from here, even if
#      it stopped being servable: that's the user's call (or ours, for one we bought)
# A user's own domain needs no step of its own: it's attached when the user adds it (status.add_own_domain). A plan
# with no domain at all just waits (the status asks for one).
# Re-run on every payment webhook, every launch-status poll, and every LAUNCH_SWEEP_SECONDS for every project
# that's hosted (fulfil_hosted_projects) -- a domain whose DNS shows up hours later goes live with nobody
# watching -- the only thing those re-runs wait on is a bought domain's DNS. Only with hosting (a plan running, or in
# its grace period).
#
# A domain that can't be registered after all (taken between checkout and payment, or turned down by the
# registry) gets its subscription cancelled and its payment refunded (a free-first-year one charged nothing), and
# the failure recorded: the plan stays, the user picks another domain. Steps that talk to the registrar only run
# until the domain is attached, so polling doesn't hammer it.
# What payments' rows don't say comes from the checkout that started them (payments.get_recorded_checkout): who a bought
# domain is registered to.
# References: checkouts.py (what those checkouts record), status.py (where it's shown).

import re
from datetime import UTC, datetime, timedelta

import pydash

from src.config import settings
from src.constants import PURCHASED_DOMAIN_DNS_WAIT_SECONDS, AnalyticsEvent
from src.dto.projects import Project
from src.modules import deployments, domains, payments
from src.modules.domains import DomainUnavailable, RegistrarError
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services.launch import access as launch_access
from src.services.launch import domain_events
from src.utils import analytics, redis
from src.utils.logging import log_error

# One fulfilment per project at a time; the TTL only covers a backend that died mid-way.
_LOCK_TTL_SECONDS = 10 * 60


async def fulfil(project_id: int) -> None:
    """Run the steps (see the top of this file) for a project, unless a run is already going. Never raises: a step
    that fails is logged and retried on the next run. [I/O]"""
    # Fulfil is built to be safe to call any number of times, so the rule is:
    # call it whenever something might have changed, and let it work out whether anything needs doing.
    # Safe to call for any project at any stage. It's called periodically by the sweep.
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
        await _ensure_active_domain(project)
    except Exception as error:
        log_error(error, f"launch fulfilment of project {project_id} failed", {"project_id": project_id})
    finally:
        await redis.delete(lock_key)


async def fulfil_hosted_projects() -> None:
    """Fulfil every project a plan hosts (running, or in its grace period), one at a time (the periodic sweep, see the
    top of this file) -- the same projects fulfil acts on.
    Cheap for a project that's done: no registrar or DNS lookups once its active domain has been deployed. [I/O]"""
    now = datetime.now(UTC)
    plans = await payments.find_subscriptions({"product": payments.Product.PLAN})
    project_ids = pydash.uniq(
        [
            row["target_id"]
            for row in plans
            if row["target_type"] == payments.TargetType.PROJECT
            and (payments.is_running(row, now) or payments.is_in_grace(row, now))
        ]
    )
    for project_id in project_ids:  # in turn, not all at once: each may start a build
        await fulfil(project_id)


async def handle_payment_webhook(provider: payments.Provider, raw_body: bytes, headers: dict[str, str]) -> None:
    """Take in a payment provider webhook, then fulfil the project it was about. Raises payments.InvalidSignature."""
    row = await payments.handle_webhook(provider, raw_body, headers)
    if row and row["target_type"] == payments.TargetType.PROJECT:
        await fulfil(row["target_id"])


async def _ensure_purchased_domain(project: Project, subscription: dict) -> None:
    """Step 1: the domain the user paid for (its running domain subscription) ends up registered to them, pointing
    at our server, and attached to the project. In order:
      1. already attached -> done (this is also what keeps later runs from calling the registrar at all)
      2. register it at the registrar, with the registrant from the checkout that bought it (recorded then)
      3. its A record -> our server
      4. attach it
    If it can't be registered (someone took it between checkout and payment, or the registry turned it down), the
    user gets their money back for the domain only: the subscription is ended right away, its payment refunded, and
    the failure recorded -- the status then asks for another domain, and the launch flow's access (access.py) no longer
    counts this subscription, so it never comes back here. The plan stays."""
    domain = subscription["product_data"]["domain"]
    attached = await deployments.get_site_domains(project)
    # This doesn't handle registered but expired domain yet.
    if pydash.find(attached, {"domain": domain}):
        return
    subscription_ref = subscription["provider_ref"]
    checkout = await payments.get_recorded_checkout(pydash.get(subscription, "custom_data.checkout_id"))
    if checkout is None:
        raise ValueError(f"no checkout recorded for domain subscription {subscription_ref}")
    # The domain lives in the same world as the money that paid for it: a sandbox payment, the registrar's sandbox.
    sandbox = subscription["provider"] == payments.Provider.POLAR_SANDBOX
    try:
        await _ensure_registered(project, domain, sandbox, checkout, subscription_ref)
    except DomainUnavailable:
        await payments.revoke_subscription(subscription)
        await payments.refund_latest_payment(subscription)
        await domain_events.record_domain_failure(
            project["id"], checkout["checkout_id"], subscription_ref, domain, "unavailable"
        )
        analytics.capture(
            project["user_id"],
            AnalyticsEvent.LAUNCH_FAILED,
            {"project_id": project["id"], "stage": "registration", "error": "unavailable", "is_rolled_back": False},
        )
        return
    await _ensure_a_record(domain, domains.get_registrar(domain, sandbox))
    # Attached, not active: step 2 makes it the active one only if the site has none yet. One bought to add to a
    # live site waits for the user to switch to it.
    await deployments.attach_domain(project, domain, deployments.SiteDomainKind.PURCHASED)


async def _ensure_registered(
    project: Project, domain: str, sandbox: bool, checkout: dict, subscription_ref: str
) -> None:
    """The domain is in our registrar account (its sandbox one when `sandbox`) -- registered now, for a year, to the
    registrant its `checkout` recorded (the user), unless it already is; a registration is recorded
    (domain.registered, with the registrar). The subscription id is the idempotency key, so a retry never buys
    twice. Raises DomainUnavailable."""
    try:
        await domains.get_domain(domain, domains.get_registrar(domain, sandbox))
        return
    except RegistrarError as error:
        if error.status != 404:
            raise
    registrant = checkout.get("registrant")
    if not registrant:
        raise ValueError(f"no registrant recorded for {domain}")
    registration = await domains.register_domain(
        domain,
        1,
        _to_registrar_contact(registrant),
        f"subscription-{subscription_ref}",
        sandbox=sandbox,
    )
    await domain_events.record_domain_registered(
        project["id"], checkout["checkout_id"], subscription_ref, registration, registrant
    )


async def _ensure_a_record(domain: str, registrar: str) -> None:
    """The domain's apex A record points at the server (its DNS is at `registrar`, where we registered it)."""
    records = await domains.list_records(domain, registrar)
    if not pydash.find(records, lambda record: record.get("type") == "A" and not record.get("host")):
        record = {"host": "", "type": "A", "answer": settings.server_public_ip, "ttl": 300}
        await domains.create_record(domain, record, registrar)


async def _ensure_active_domain(project: Project) -> None:
    """Step 2: a site with no active domain gets its most recently attached one set up and made active
    (deployments.activate_domain) -- one automatic attempt per domain. Nothing when it has an active domain already
    (never switched from here, even if it stopped being servable), when one's being set up, when the newest is a
    bought one public DNS doesn't have yet (_is_dns_wait_over), or when that domain's attempt already failed (the
    user retries: "Use this domain" in the Launch tab)."""
    # Ensure here means try to do what can be done now, not ensuring an domain is activated when the function returns.
    attached = await deployments.get_site_domains(project)
    if any(row["activated_at"] for row in attached):
        return
    activation = await deployments.get_activation(project)
    newest = max(attached, key=lambda row: row["created_at"], default=None)  # pydash.max_by raises on an empty list
    if activation["domain"] or newest is None or not await _is_dns_wait_over(newest):
        return
    failure = activation["failure"]
    if failure and failure["domain"] == newest["domain"] and failure["at"] >= newest["created_at"]:
        return
    conversation = await agent_service.get_current_conversation(project["id"], project["user_id"])
    try:
        await deployments.activate_domain(project, conversation["id"], newest["domain"])
    except deployments.DeploymentInProgress:
        return


async def _is_dns_wait_over(site_domain: dict) -> bool:
    """Whether a domain can be attempted now. One we bought: once public DNS points it at us -- Let's Encrypt can't
    issue its certificate before -- or PURCHASED_DOMAIN_DNS_WAIT_SECONDS after it's attached (then it's attempted
    anyway, and fails visibly). Anything else: right away. [I/O: reads]"""
    if site_domain["kind"] != deployments.SiteDomainKind.PURCHASED:
        return True
    deadline = site_domain["created_at"] + timedelta(seconds=PURCHASED_DOMAIN_DNS_WAIT_SECONDS)
    return datetime.now(UTC) >= deadline or await deployments.is_pointed_at_server(site_domain["domain"])


def _to_registrar_contact(registrant: dict) -> dict:
    """[Pure] A checkout's registrant (dto/launch.Registrant) in the registrar's contact shape."""
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
            # Checkouts recorded before the phone was stored as plain E.164 have it as +84.901234567.
            "phone": re.sub(r"[^\d+]", "", registrant["phone"]),
        },
        pydash.is_none,
    )
