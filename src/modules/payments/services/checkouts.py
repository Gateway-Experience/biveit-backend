# Intentions: starting a purchase -- a checkout at the provider for one thing (a plan, the one-time purchase, a
# domain). Each one is recorded first as a `payment.checkout_created` event holding what that checkout is for, which
# provider it's at, and whatever the caller needs to remember about it (`details`, e.g. who a domain gets registered
# to). The event's id goes to the provider in the checkout's metadata, with our other ids ({user_id, project_id,
# product, plan / domain}) and `origin` -- the provider copies it onto the subscription / order the checkout makes, so
# those point back at the checkout that started them. `origin` is which deployment of ours opened it: one Polar
# sandbox can serve prod (admins testing) and dev boxes at once, and each must only take in its own purchases.
# Also: which provider a user pays through (get_payment_provider), and changing a running plan.

from src.config import settings
from src.constants import UserRole
from src.dto.projects import Project
from src.dto.users import User
from src.modules.payments.constants import PLANS, PaymentEvent, Plan, Product, Provider
from src.modules.payments.services import catalog, polar
from src.repositories import events as events_repo


def is_sandbox(user: User) -> bool:
    """[Pure-ish] Whether this user's purchases happen in sandbox mode -- test payments, and domains on the
    registrar's sandbox (one switch for both, see settings.sandbox_mode): the server's mode, or an admin who's
    switched their own purchases to sandbox (users.meta.sandbox_mode, set from the admin page). Anyone else's meta
    flag means nothing -- a user can write their own meta, so the role is what counts."""
    is_admin_sandbox = user.get("role") == UserRole.ADMIN and bool((user.get("meta") or {}).get("sandbox_mode"))
    return settings.sandbox_mode or is_admin_sandbox


def get_payment_provider(user: User) -> Provider:
    """[Pure-ish] Where this user pays: Polar's sandbox in sandbox mode (is_sandbox), else live Polar."""
    return Provider.POLAR_SANDBOX if is_sandbox(user) else Provider.POLAR


def get_origin() -> str:
    """[Pure-ish] Which deployment of ours this is, as checkouts record it (see the top of this file)."""
    return settings.frontend_url


async def create_plan_checkout(user: User, project: Project, plan: Plan) -> dict:
    """A checkout for a plan (monthly / yearly) or the one-time purchase: {checkout_id, url}. [I/O]"""
    return await _create(
        user, project, PLANS[plan]["product_key"], {"product": PLANS[plan]["product"], "plan": plan}, {}
    )


async def create_domain_checkout(
    user: User, project: Project, domain: str, price_per_year: int, discounted: bool, details: dict
) -> dict:
    """A checkout for a domain's yearly subscription at one of our prices -- with the domain discount when
    `discounted` (today: a year's trial, so the first year's free): {checkout_id, url}. [I/O]"""
    return await _create(
        user,
        project,
        catalog.get_domain_product_key(price_per_year),
        {"product": Product.DOMAIN, "domain": domain},
        {"price_per_year": price_per_year, "discounted": discounted, **details},
        {"trial_interval": "year", "trial_interval_count": 1} if discounted else {},
    )


async def change_plan(subscription: dict, plan: Plan) -> None:
    """Move a running plan subscription (its row) to another plan (monthly -> yearly): the same subscription, the
    provider charging the prorated difference now -- no checkout page, so no checkout event; its webhook updates the
    row. Raises polar.ProviderError. [I/O]"""
    provider = Provider(subscription["provider"])
    await polar.change_product(
        provider, subscription["provider_ref"], catalog.get_product_id(provider, PLANS[plan]["product_key"])
    )


async def get_recorded_checkout(checkout_id: int | str | None) -> dict | None:
    """A checkout we started, as recorded: {checkout_id, user_id, project_id, provider, product, plan / domain, ...its
    details, created_at}. None for an unknown id. [I/O: reads]"""
    checkout_ref = str(checkout_id or "")
    event = await events_repo.get(int(checkout_ref)) if checkout_ref.isdigit() else None
    if event is None or event["type"] != PaymentEvent.CHECKOUT_CREATED:
        return None
    return {"checkout_id": event["id"], **event["value"], "created_at": event["created_at"]}


async def _create(
    user: User, project: Project, product_key: str, item: dict, details: dict, provider_options: dict | None = None
) -> dict:
    """Record the checkout, then open it at the user's provider with its id attached. `item`: what it sells, also
    sent to the provider; `details`: only kept in the event; `provider_options`: extra checkout fields for the
    provider (a trial...)."""
    provider = get_payment_provider(user)
    product_id = catalog.get_product_id(provider, product_key)
    owner = {"user_id": user["id"], "project_id": project["id"]}
    event = await events_repo.insert(
        {
            "type": PaymentEvent.CHECKOUT_CREATED,
            "subject_type": "user",
            "subject_ref": str(user["id"]),
            "direct_object_type": "project",
            "direct_object_ref": str(project["id"]),
            "value": {**item, **details, **owner, "provider": provider},
        }
    )
    checkout_data = {
        "metadata": {**item, **owner, "checkout_id": str(event["id"]), "origin": get_origin()},
        # Back to the project: the launch dialog there is polling the status, so it moves on by itself.
        "success_url": f"{settings.frontend_url}/projects/{project['id']}",
        **(provider_options or {}),
    }
    try:
        checkout = await polar.create_checkout(
            provider, product_id, {**checkout_data, "customer_email": user["username"] or None}
        )
    except polar.ProviderError as error:
        # Intentions: the email is only a prefill. Polar turns down one it thinks can't receive mail (a domain
        # without mail servers, e.g. a typo) -- then the buyer just types theirs on Polar's page.
        if error.status != 422 or "customer_email" not in str(error):
            raise
        checkout = await polar.create_checkout(provider, product_id, checkout_data)
    return {"checkout_id": event["id"], "url": checkout["url"]}
