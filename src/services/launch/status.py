# Intentions: where a project's launch stands, as the FE's launch dialog and Launch tab show it -- all read live
# from payments (what's bought), deployments (the site, its domains) and the launch's own events, folded into one
# `stage` the UI can switch on (to_stage). Reading it also nudges fulfilment along when something's still pending
# (a domain's DNS appearing, say), so polling the status is what moves a waiting launch forward.
# Also: switching the site to another of its domains.

import pydash

from src.config import settings
from src.dto.projects import Project
from src.dto.users import User
from src.modules import deployments, payments
from src.services import agent as agent_service
from src.services.launch import fulfilment, intents
from src.services.launch.checkouts import LaunchRefused
from src.utils.common import spawn

# The stages where fulfilment still has something to do -- reading the status then runs it.
_PENDING_STAGES = {"registering_domain", "waiting_dns", "deploying"}


async def get_launch_status(project: Project, user: User) -> dict:
    """The project's launch, everything the FE needs:
    stage            to_stage's
    plans            what can be bought (payments.get_plans)
    access           {can_download, can_edit, can_host, edit_until}
    plan             the running plan {plan, status, renews_at, ends_at} | None
    one_time         the one-time purchase {paid_at} | None
    domain_subscription  the running domain {domain, status, renews_at, trial_ends_at} | None
    domain           the active domain {domain, kind, activated_at, servable (may be served now), check (a user's
                     own: check_external_domain)} | None
    site_domains     every domain attached [{domain, kind, active}]
    launch           the plan's launch {launch_id, plan, domain (no registrant), domain_failure} | None
    deployment       deployments.get_deployment_status
    server_ip        where a user's own domain must point"""
    access = await payments.get_project_access(project["id"])
    conversation = await agent_service.get_current_conversation(project["id"], user["id"])
    deployment = await deployments.get_deployment_status(project, conversation["id"])
    active = pydash.find(deployment["site_domains"], lambda row: row["activated_at"] is not None)
    check = (
        await deployments.check_external_domain(project, active["domain"])
        if active and active["kind"] == deployments.SiteDomainKind.EXTERNAL
        else None
    )
    launch_id = pydash.get(access, "plan_subscription.custom_data.launch_id")
    intent = await intents.get_intent(launch_id)
    failures = await intents.get_domain_failures(project["id"])
    domain_failure = (
        pydash.find_last(failures, lambda failure: failure["launch_id"] == launch_id) if launch_id else None
    )
    status = {
        "plans": payments.get_plans(),
        "access": pydash.pick(access, "can_download", "can_edit", "can_host", "edit_until"),
        "plan": pydash.pick(access["plan_subscription"], "plan", "status", "renews_at", "ends_at")
        if access["plan_subscription"]
        else None,
        "one_time": {"paid_at": access["one_time_order"]["created_at"]} if access["one_time_order"] else None,
        "domain_subscription": {
            "domain": access["domain_subscription"]["product_data"]["domain"],
            **pydash.pick(access["domain_subscription"], "status", "renews_at", "trial_ends_at"),
        }
        if access["domain_subscription"]
        else None,
        "domain": {
            "domain": active["domain"],
            "kind": active["kind"],
            "activated_at": active["activated_at"],
            "servable": active["domain"] in deployment["domains"],
            "check": check,
        }
        if active
        else None,
        "site_domains": [
            {"domain": row["domain"], "kind": row["kind"], "active": row["activated_at"] is not None}
            for row in deployment["site_domains"]
        ],
        "launch": {
            "launch_id": launch_id,
            "plan": intent["plan"],
            "domain": intent["domain"],
            "domain_failure": domain_failure,
        }
        if intent
        else None,
        "deployment": deployment,
        "server_ip": settings.server_public_ip,
    }
    stage = to_stage(status)
    if access["can_host"] and stage in _PENDING_STAGES:
        spawn(fulfilment.fulfil(project["id"]))
    return {**status, "stage": stage}


def to_stage(status: dict) -> str:
    """[Pure] One word for where the launch is (the order of the checks matters):
    not_paid              nothing bought yet
    code_only             the one-time purchase, no hosting
    ended                 a plan ran out (code download still there)
    finish_domain         paid the plan, but the launch's domain checkout wasn't completed
    choose_domain         hosting, but no domain to serve on (none chosen, or the bought one failed)
    registering_domain    a bought domain is paid for, not set up yet
    waiting_dns           the active domain is the user's own and its DNS records aren't in place yet
    deploying             a deployment is running, or about to start
    failed                the last deployment failed
    live                  the site is served on its active domain"""
    access, deployment = status["access"], status["deployment"]
    domain, launch = status["domain"], status["launch"] or {}
    if not access["can_host"]:
        if status["one_time"]:
            return "code_only"
        return "ended" if access["can_download"] else "not_paid"
    wants_bought_domain = pydash.get(launch, "domain.kind") == "buy"
    if wants_bought_domain and not status["domain_subscription"] and not launch.get("domain_failure") and not domain:
        return "finish_domain"
    if status["domain_subscription"] and not pydash.find(
        status["site_domains"], {"domain": status["domain_subscription"]["domain"]}
    ):
        return "registering_domain"
    if domain is None:
        return "choose_domain"
    if not domain["servable"]:
        return "waiting_dns"
    # Only a deployment started since the domain became active counts; before it, one is about to start.
    last = deployment["last_deployment"]
    current = last if last and last["started_at"] >= domain["activated_at"] else None
    if deployment["deploying"] or current is None:
        return "deploying"
    return "live" if current["outcome"] == "succeeded" else "failed"


async def activate_domain(project: Project, domain: str) -> None:
    """Switch the site to another of its attached domains: it becomes the active one now, and is served from the
    deployment fulfilment starts for it. A user's own domain only once its DNS records check out, so a live site
    never moves to a domain it can't be served on. Raises LaunchRefused (PLAN_REQUIRED, UNKNOWN_DOMAIN,
    DOMAIN_NOT_VERIFIED)."""
    access = await payments.get_project_access(project["id"])
    if not access["can_host"]:
        raise LaunchRefused("PLAN_REQUIRED")
    attached = await deployments.get_site_domains(project)
    row = pydash.find(attached, {"domain": deployments.normalize_domain(domain)})
    if row is None:
        raise LaunchRefused("UNKNOWN_DOMAIN", 404)
    if (
        row["kind"] == deployments.SiteDomainKind.EXTERNAL
        and not (await deployments.check_external_domain(project, row["domain"]))["verified"]
    ):
        raise LaunchRefused("DOMAIN_NOT_VERIFIED", 409)
    await deployments.attach_domain(project, row["domain"], row["kind"], activate=True)
    spawn(fulfilment.fulfil(project["id"]))
