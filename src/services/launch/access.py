# Intentions: what a project has bought, as the launch flow sees it -- payments' access, minus a domain subscription
# whose domain couldn't be registered. Cancelling a failed domain's subscription doesn't end it (it runs to the end
# of its period, payments.is_running), so payments still calls it the project's running domain; for the launch flow
# it's gone: the user got their money back and gets to pick another domain. Every launch file reads access through here, so they all agree on that.
# Also: which ways out of the project list a project has because of all that (to_removal_options).

import asyncio
from datetime import UTC, datetime

import pydash

from src.modules import deployments, payments
from src.services.launch import domain_events


async def get_launch_access(project_id: int) -> dict:
    """payments.get_project_access, with domain_subscription None when that subscription's registration failed, plus
    domain_failures (domain_events.get_domain_failures). [I/O: reads]"""
    access, failures = await asyncio.gather(
        payments.get_project_access(project_id), domain_events.get_domain_failures(project_id)
    )
    # A failed domain registration cancels the sub (and refund) but doesn't do anything to invalidate the current sub,
    # so we have to check failed events in conjunction with it.
    # FYI a failed subscription is never retried, user can only create a new sub, so checking for membership like this is enough.
    failed_refs = {failure["subscription_ref"] for failure in failures}
    has_failed = pydash.get(access, "domain_subscription.provider_ref") in failed_refs
    return {
        **access,
        "domain_subscription": None if has_failed else access["domain_subscription"],
        "domain_failures": failures,
    }


def to_removal_options(access: dict, has_active_domain: bool) -> dict:
    """[Pure] What can be done to get a project out of the list, from its launch access and whether it has gone live
    (an active domain):
      can_trash    nothing's been bought that still counts and it never went live. A running plan or domain is being
                   billed, a one-time purchase was paid for its code, and a site that went live may still be served:
                   none of those should be one click from gone.
      can_archive  it went live, and nothing's being billed anymore (its plan stopped, a grace period included)."""
    is_billed = bool(access["plan_subscription"] or access["domain_subscription"])
    return {
        "can_trash": not (is_billed or access["one_time_subscription"] or has_active_domain),
        "can_archive": has_active_domain and not is_billed,
    }


async def get_removal_options(project_ids: list[int]) -> dict[int, dict]:
    """to_removal_options for each project, by id. Read in one go, since it's for the whole project list: only a
    project with a running domain needs its own look, for a failed registration (get_launch_access). [I/O: reads]"""
    subscriptions, live_ids = await asyncio.gather(
        payments.find_subscriptions_by_targets(payments.TargetType.PROJECT, project_ids),
        deployments.find_projects_with_active_domain(project_ids),
    )
    subscriptions_by_project = pydash.group_by(subscriptions, "target_id")
    now = datetime.now(UTC)

    async def get_access(project_id: int) -> dict:
        access = payments.to_access(subscriptions_by_project.get(project_id, []), now)
        return await get_launch_access(project_id) if access["domain_subscription"] else access

    accesses = await asyncio.gather(*[get_access(project_id) for project_id in project_ids])
    return {
        project_id: to_removal_options(access, project_id in live_ids)
        for project_id, access in zip(project_ids, accesses, strict=True)
    }
