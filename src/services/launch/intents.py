# Intentions: a launch's own record -- its `launch.*` events, grouped by launch id: what it set out to buy
# (launch.checkout_created, the latest one counts) and what went wrong on the way (launch.domain_failed). The rest of
# a launch's state is what payments and deployments hold.

from src.dto.projects import Project
from src.dto.users import User
from src.repositories import events as events_repo


async def record_intent(launch_id: str, user: User, project: Project, intent: dict) -> None:
    """Record what a launch sets out to buy: {project_id, plan, domain: {kind, name, price_per_year,
    free_first_year} | None, registrant}."""
    await events_repo.insert(
        {
            "type": "launch.checkout_created",
            "subject_type": "user",
            "subject_ref": str(user["id"]),
            "direct_object_type": "project",
            "direct_object_ref": str(project["id"]),
            "grouping_type": "launch",
            "grouping_ref": launch_id,
            "value": intent,
        }
    )


async def get_intent(launch_id: str | None) -> dict | None:
    """What a launch set out to buy (record_intent's shape), or None for an unknown launch."""
    if not launch_id:
        return None
    events = await events_repo.find(
        {"type": "launch.checkout_created", "grouping_type": "launch", "grouping_ref": launch_id}
    )
    return events[-1]["value"] if events else None


async def record_domain_failure(
    launch_id: str | None, project_id: int, subscription_ref: str, domain: str, reason: str
) -> None:
    """Record that a domain bought in a launch couldn't be registered (its subscription is then cancelled)."""
    await events_repo.insert(
        {
            "type": "launch.domain_failed",
            "subject_type": "project",
            "subject_ref": str(project_id),
            "direct_object_type": "subscription",
            "direct_object_ref": subscription_ref,
            "grouping_type": "launch",
            "grouping_ref": launch_id or "",
            "value": {"domain": domain, "reason": reason},
        }
    )


async def get_domain_failures(project_id: int) -> list[dict]:
    """The project's failed domain registrations, oldest first: [{subscription_ref, launch_id, domain, reason, at}]."""
    events = await events_repo.find(
        {"type": "launch.domain_failed", "subject_type": "project", "subject_ref": str(project_id)}
    )
    return [
        {"subscription_ref": event["direct_object_ref"], "launch_id": event["grouping_ref"] or None, **event["value"], "at": event["created_at"]}
        for event in events
    ]  # fmt: skip
