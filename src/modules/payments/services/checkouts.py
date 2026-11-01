# Intentions: starting a purchase -- a checkout link at the provider for one thing (a plan, the one-time purchase, a
# domain). Each one is recorded first as a `payment.checkout_created` event holding what that checkout is for and
# whatever the caller needs to remember about it (`details`, e.g. who a domain gets registered to). The event's id
# goes to the provider as custom_data.checkout_id, with our other ids ({user_id, project_id, product, plan /
# domain}), so its webhooks -- and the subscription row they make -- point back at the checkout that started it.

from datetime import UTC, datetime, timedelta

from src.dto.projects import Project
from src.dto.users import User
from src.modules.payments.constants import CHECKOUT_TTL_SECONDS, PLANS, PaymentEvent, Plan, Product
from src.modules.payments.services import catalog, lemonsqueezy
from src.repositories import events as events_repo


async def create_plan_checkout(user: User, project: Project, plan: Plan) -> dict:
    """A checkout for a plan (monthly / yearly) or the one-time purchase: {checkout_id, url}. [I/O]"""
    return await _create(
        user, project, PLANS[plan]["variant_id"], {"product": PLANS[plan]["product"], "plan": plan}, {}
    )


async def create_domain_checkout(
    user: User, project: Project, domain: str, price_per_year: int, discounted: bool, details: dict
) -> dict:
    """A checkout for a domain's yearly subscription at one of our prices -- with the domain discount when
    `discounted` (its discounted variant): {checkout_id, url}. [I/O]"""
    return await _create(
        user,
        project,
        catalog.get_domain_variant(price_per_year, discounted),
        {"product": Product.DOMAIN, "domain": domain},
        {"price_per_year": price_per_year, "discounted": discounted, **details},
    )


async def change_plan(subscription_ref: str, plan: Plan) -> list[tuple[bytes, str]]:
    """Move a running plan subscription to another plan (monthly -> yearly): the same subscription, the provider
    charging the difference, prorated -- no checkout page, so no checkout event; its webhook updates the row. Returns
    the stand-in's webhooks to feed through (lemonsqueezy.change_variant). [I/O]"""
    return await lemonsqueezy.change_variant(subscription_ref, PLANS[plan]["variant_id"])


async def get_checkout(checkout_id: int | str | None) -> dict | None:
    """A checkout we started, as recorded: {checkout_id, user_id, project_id, product, plan / domain, ...its details,
    created_at}. None for an unknown id. [I/O: reads]"""
    checkout_ref = str(checkout_id or "")
    event = await events_repo.get(int(checkout_ref)) if checkout_ref.isdigit() else None
    if event is None or event["type"] != PaymentEvent.CHECKOUT_CREATED:
        return None
    return {"checkout_id": event["id"], **event["value"], "created_at": event["created_at"]}


async def _create(user: User, project: Project, variant_id: str, item: dict, details: dict) -> dict:
    """Record the checkout, then open it at the provider with its id attached. `item`: what it sells, also sent to
    the provider; `details`: only kept in the event."""
    owner = {"user_id": user["id"], "project_id": project["id"]}
    event = await events_repo.insert(
        {
            "type": PaymentEvent.CHECKOUT_CREATED,
            "subject_type": "user",
            "subject_ref": str(user["id"]),
            "direct_object_type": "project",
            "direct_object_ref": str(project["id"]),
            "value": {**item, **details, **owner},
        }
    )
    expires_at = (datetime.now(UTC) + timedelta(seconds=CHECKOUT_TTL_SECONDS)).isoformat()
    custom_data = {**item, **owner, "checkout_id": str(event["id"])}
    url = await lemonsqueezy.create_checkout(variant_id, user["username"] or "", custom_data, expires_at)
    return {"checkout_id": event["id"], "url": url}
