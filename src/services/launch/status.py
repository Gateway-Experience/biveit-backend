# Intentions: where a project's launch stands, as the FE's launch dialog and Launch tab show it -- all read live
# from payments (what's bought), deployments (the site, its domains) and the domain events, folded into one
# `stage` the UI can switch on (to_stage). Reading it also nudges fulfilment along when something's still pending
# (a domain's DNS appearing, say), so polling the status is what moves a waiting launch forward.
# Also: the user adding their own domain, and switching the site to another of its domains.
# The domain terms (attached, servable, active) are docs/launchflow.txt's "Domains".

from uuid import UUID

import pydash

from src.config import settings
from src.dto.projects import Project
from src.dto.users import User
from src.modules import deployments, payments
from src.services import agent as agent_service
from src.services.launch import access as launch_access
from src.services.launch import checkouts, fulfilment
from src.services.launch.checkouts import LaunchRefused
from src.utils.common import spawn

# The stages where fulfilment still has something to do -- reading the status then runs it.
_PENDING_STAGES = {"registering_domain", "waiting_dns"}


async def get_launch_status(project: Project, user: User) -> dict:
    """The project's launch, everything the FE needs:
    stage            to_stage's
    plans            what can be bought (payments.get_plans)
    access           {can_download, can_edit, can_host, has_had_plan, edit_until}
    plan             the running plan {plan, status, renews_at, ends_at} | None (none in a grace period: the plan's
                     over, hosting just hasn't stopped yet)
    one_time         the one-time purchase {paid_at} | None
    domain_subscription  the running domain {domain, status, renews_at, trial_ends_at} | None
    domain           the active domain {domain, kind, activated_at, servable, check (a user's own:
                     check_external_domain)} | None
    site_domains     every domain attached [{domain, kind, active, servable}]
    setting_up       the domain being set up right now | None
    setup_failure    the last domain whose first release failed {domain, cause: dns | certificate | release, error,
                     at} | None (deployments.get_activation; a switch's failure is told by its own operation)
    has_domain_discount  whether a domain bought now gets the domain discount (checkouts.has_domain_discount)
    registration_failure  the last bought domain that couldn't be registered {domain, reason, at}, while no domain
                     bought since replaces it (and since the plan started) | None
    deployment       deployments.get_deployment_status
    server_ip        where a user's own domain must point
    payment_provider where this user's checkouts would go: polar | polar_sandbox (test cards -- the FE flags it)"""
    access = await launch_access.get_launch_access(project["id"])
    conversation = await agent_service.get_current_conversation(project["id"], user["id"])
    deployment = await deployments.get_deployment_status(project, conversation["id"])
    active_domain = pydash.find(deployment["site_domains"], lambda row: row["activated_at"] is not None)
    dns_check = (
        await deployments.check_external_domain(project, active_domain["domain"])
        if active_domain and active_domain["kind"] == deployments.SiteDomainKind.EXTERNAL
        else None
    )
    plan_started_at = pydash.get(access, "plan_subscription.created_at")
    registration_failure = (
        pydash.find_last(access["domain_failures"], lambda failure: failure["at"] >= plan_started_at)
        if plan_started_at and not access["domain_subscription"]
        else None
    )
    status = {
        "plans": payments.get_plans(),
        "access": pydash.pick(access, "can_download", "can_edit", "can_host", "has_had_plan", "edit_until"),
        "plan": pydash.pick(access["plan_subscription"], "plan", "status", "renews_at", "ends_at")
        if access["plan_subscription"]
        else None,
        "one_time": {"paid_at": access["one_time_subscription"]["created_at"]}
        if access["one_time_subscription"]
        else None,
        "domain_subscription": {
            "domain": access["domain_subscription"]["product_data"]["domain"],
            **pydash.pick(access["domain_subscription"], "status", "renews_at", "trial_ends_at"),
        }
        if access["domain_subscription"]
        else None,
        "domain": {**pydash.pick(active_domain, "domain", "kind", "activated_at", "servable"), "check": dns_check}
        if active_domain
        else None,
        "site_domains": [
            {**pydash.pick(row, "domain", "kind", "servable"), "active": row["activated_at"] is not None}
            for row in deployment["site_domains"]
        ],
        "setting_up": deployment["activation"]["domain"],
        "setup_failure": deployment["activation"]["failure"],
        "has_domain_discount": await checkouts.has_domain_discount(project, access),
        "registration_failure": pydash.pick(registration_failure, "domain", "reason", "at")
        if registration_failure
        else None,
        "deployment": deployment,
        "server_ip": settings.server_public_ip,
        "payment_provider": payments.get_payment_provider(user),
    }
    stage = to_stage(status)
    if access["can_host"] and stage in _PENDING_STAGES:
        spawn(fulfilment.fulfil(project["id"]))
    return {**status, "stage": stage}


def to_stage(status: dict) -> str:
    """[Pure] One word for where the launch is (the order of the checks matters):
    not_paid              nothing bought yet
    code_only             the one-time purchase, never a plan
    ended                 no plan running anymore (a grace period counts: the plan's over) -- code download stays
    registering_domain    a bought domain is paid for, not registered and attached yet
    deploying             no active domain yet, and one's being set up (the site's first release); or, with one,
                          a code deployment running
    failed                no active domain yet, and the attempt to set one up failed (setup_failure says why); the
                          user retries
    waiting_dns           no active domain yet, and the newest one isn't attempted yet: one just bought that public
                          DNS doesn't have yet (or, for a moment, a user's own before its attempt starts)
    choose_domain         a plan, but no domain attached at all (none added or bought yet, or the bought one couldn't
                          be registered)
    not_servable          the active domain stopped being servable (a user's own whose records broke): it stays
                          active, the user fixes it or switches
    live                  the site is served on its active domain (a switch to another one may be under way:
                          setting_up)"""
    access, deployment, active = status["access"], status["deployment"], status["domain"]
    if not status["plan"]:
        if status["one_time"] and not access["has_had_plan"]:
            return "code_only"
        return "ended" if access["can_download"] else "not_paid"
    if status["domain_subscription"] and not pydash.find(
        status["site_domains"], {"domain": status["domain_subscription"]["domain"]}
    ):
        return "registering_domain"
    if active:
        if deployment["deploying"] and not status["setting_up"]:
            return "deploying"
        return "live" if active["servable"] else "not_servable"
    if status["setting_up"]:
        return "deploying"
    if status["setup_failure"]:
        return "failed"
    return "waiting_dns" if status["site_domains"] else "choose_domain"


async def add_own_domain(project: Project, domain: str) -> dict:
    """Attach the user's own domain to the project. Needs no plan, and doesn't make it active: fulfilment does that
    once a plan hosts the site and its records check out, if the site has no active domain yet -- otherwise the
    user switches to it. Returns check_external_domain's answer, to show the records. Raises LaunchRefused
    (NOT_APPROVED, DOMAIN_INVALID / _OURS / _TAKEN)."""
    if not project.get("build_approved_at"):
        raise LaunchRefused("NOT_APPROVED")
    own_domain_check = await deployments.check_external_domain(project, domain)
    if own_domain_check["blocked"]:
        blocked = own_domain_check["blocked"]
        raise LaunchRefused(f"DOMAIN_{blocked.upper()}", 400 if blocked == "invalid" else 409)
    try:
        await deployments.attach_domain(project, own_domain_check["domain"], deployments.SiteDomainKind.EXTERNAL)
    except deployments.DomainTaken as error:  # another project got it since the check
        raise LaunchRefused("DOMAIN_TAKEN") from error
    spawn(fulfilment.fulfil(project["id"]))
    return own_domain_check


async def activate_domain(project: Project, domain: str) -> UUID | None:
    """Switch the site to another of its attached domains (the user's choice): it's set up in the background while
    the current one keeps serving, and only becomes active once that's worked (deployments.activate_domain). Returns
    the switch's operation id, to poll how it went (deployments.get_domain_switch) -- None when there's no site yet
    and it's the first deployment instead (the status follows that), or when it's the active one already. Only a
    servable one: a user's own once its records check out. Raises LaunchRefused (PLAN_REQUIRED, UNKNOWN_DOMAIN,
    DOMAIN_NOT_VERIFIED, BUSY -- a deployment or another switch is running)."""
    access = await launch_access.get_launch_access(project["id"])
    if not access["plan_subscription"]:
        raise LaunchRefused("PLAN_REQUIRED")
    attached = await deployments.get_site_domains(project)
    row = pydash.find(attached, {"domain": deployments.normalize_domain(domain)})
    if row is None:
        raise LaunchRefused("UNKNOWN_DOMAIN", 404)
    if row["activated_at"]:
        return None
    if not await deployments.is_servable(project, row):
        raise LaunchRefused("DOMAIN_NOT_VERIFIED", 409)
    conversation = await agent_service.get_current_conversation(project["id"], project["user_id"])
    try:
        return await deployments.activate_domain(project, conversation["id"], row["domain"])
    except deployments.DeploymentInProgress as error:
        raise LaunchRefused("BUSY") from error
