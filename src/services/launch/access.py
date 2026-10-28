# Intentions: what a project has bought, as the launch flow sees it -- payments' access, minus a domain
# subscription whose domain couldn't be registered. Cancelling that subscription doesn't end it (it runs to the
# end of its period, payments.is_running), so payments still calls it the project's running domain. For the launch
# it's gone: the user got their money back and gets to pick another domain. Every launch file reads access through
# here, so they all agree on that.

import asyncio

import pydash

from src.modules import payments
from src.services.launch import intents


async def get_launch_access(project_id: int) -> dict:
    """payments.get_project_access, with domain_subscription None when that subscription's registration failed,
    plus domain_failures (intents.get_domain_failures). [I/O: reads]"""
    access, failures = await asyncio.gather(
        payments.get_project_access(project_id), intents.get_domain_failures(project_id)
    )
    failed_refs = {failure["subscription_ref"] for failure in failures}
    has_failed = pydash.get(access, "domain_subscription.provider_ref") in failed_refs
    return {
        **access,
        "domain_subscription": None if has_failed else access["domain_subscription"],
        "domain_failures": failures,
    }
