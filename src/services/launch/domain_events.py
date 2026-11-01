# Intentions: what happened to the domains we buy for users, as `events` -- history the status reads back, never what
# fulfilment decides from (that's the registrar and site_domains):
#   domain.registered  we bought it at the registrar: who to (the owner, as the checkout recorded them), which
#                      registrar, its order there, what we paid, when it expires
#   domain.failed      it couldn't be bought after all (taken since the checkout, or the registry said no); its
#                      subscription was cancelled and refunded
# Both are the project's (subject) and point back at the checkout that bought the domain (direct object), with its
# subscription alongside (indirect object). A user's own domain gets no events: its site_domains row is all there is.

from src.constants import DomainEvent
from src.repositories import events as events_repo


async def record_domain_registered(
    project_id: int, checkout_id: int, subscription_ref: str, registration: dict, registrant: dict
) -> None:
    """Record a domain we just bought: `registration` is what domains.register_domain returned, `registrant` who it's
    registered to."""
    await events_repo.insert(
        {
            "type": DomainEvent.REGISTERED,
            **_to_participants(project_id, checkout_id, subscription_ref),
            "value": {**registration, "registrant": registrant},
        }
    )


async def record_domain_failure(
    project_id: int, checkout_id: int, subscription_ref: str, domain: str, reason: str
) -> None:
    """Record that a domain paid for couldn't be registered (its subscription is then cancelled)."""
    await events_repo.insert(
        {
            "type": DomainEvent.FAILED,
            **_to_participants(project_id, checkout_id, subscription_ref),
            "value": {"domain": domain, "reason": reason},
        }
    )


async def get_domain_failures(project_id: int) -> list[dict]:
    """The project's failed domain registrations, oldest first: [{checkout_id, subscription_ref, domain, reason,
    at}]."""
    events = await events_repo.find(
        {"type": DomainEvent.FAILED, "subject_type": "project", "subject_ref": str(project_id)}
    )
    return [
        {
            "checkout_id": int(event["direct_object_ref"]),
            "subscription_ref": event["indirect_object_ref"],
            **event["value"],
            "at": event["created_at"],
        }
        for event in events
    ]


def _to_participants(project_id: int, checkout_id: int, subscription_ref: str) -> dict:
    """[Pure]"""
    return {
        "subject_type": "project",
        "subject_ref": str(project_id),
        "direct_object_type": "checkout",
        "direct_object_ref": str(checkout_id),
        "indirect_object_type": "subscription",
        "indirect_object_ref": subscription_ref,
    }
