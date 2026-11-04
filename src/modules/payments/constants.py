# The payments module's own constants -- kept here rather than in src/constants.py because the module is meant to
# stand on its own. Prices are placeholders to fine-tune (docs/plans/launch-flow.md). Each price also exists at the
# provider as a product: the provider charges ITS price, these are what we show -- keep the two in step.

from enum import StrEnum


class Provider(StrEnum):
    """Which payment provider -- and which of its environments -- a purchase went through (subscriptions.provider).
    Polar has two separate worlds (own accounts, tokens, product ids, webhooks): live money, and the sandbox (test
    cards). A purchase stays in the world it was made in: everything later done to it goes there."""

    POLAR = "polar"  # live
    POLAR_SANDBOX = "polar_sandbox"
    # Set by hand by an admin (the admin project page): no payment and nothing at any provider behind it. Counts for
    # access like any other row; only the code that calls a provider leaves it out.
    ADMIN = "admin"


# The providers that are real payment providers -- the ones with an API to ask about a purchase.
PAYMENT_PROVIDERS = [Provider.POLAR, Provider.POLAR_SANDBOX]


class Product(StrEnum):
    """What a purchase is for (subscriptions.product)."""

    PLAN = "plan"  # hosting + the agent, monthly or yearly
    DOMAIN = "domain"  # one domain bought through us, yearly
    ONE_TIME = "one_time"  # just the code


class TargetType(StrEnum):
    """What a purchase was bought for (subscriptions.target_type, with target_id)."""

    PROJECT = "PROJECT"


class Billing(StrEnum):
    """How a purchase is paid for (subscriptions.billing)."""

    RECURRING = "recurring"  # renews every period until cancelled: plans, domains
    LIFETIME = "lifetime"  # paid once, never renews: the one-time purchase


class SubscriptionStatus(StrEnum):
    """Where a purchase stands (subscriptions.status) -- OURS, whatever the provider: each provider client maps its own
    statuses onto these (e.g. polar.to_subscription_status); its raw object, own status included, stays in the snapshot."""

    TRIALING = "trialing"  # in a free period, nothing charged yet (a domain's free first year)
    ACTIVE = "active"  # paid and current; a lifetime purchase once paid
    PAST_DUE = "past_due"  # a renewal payment failed and the provider is still retrying it
    CANCELLED = "cancelled"  # won't renew; still runs until its ends_at
    PENDING = "pending"  # not paid yet (an order still being processed)
    ENDED = "ended"  # over: ran out after a cancel, retries gave up, paused, or the payment failed
    REFUNDED = "refunded"  # the money went back


class PaymentEvent(StrEnum):
    """events.type of what the payments module records."""

    CHECKOUT_CREATED = "payment.checkout_created"  # a checkout we opened at the provider; its id is the checkout_id
    WEBHOOK = "payment.webhook"  # a provider webhook, raw (as it came, signature checked)


class ProviderObject(StrEnum):
    """Which kind of provider object a purchase is (subscriptions.provider_object) -- separate id spaces there."""

    SUBSCRIPTION = "subscription"
    ORDER = "order"


class Plan(StrEnum):
    MONTHLY = "monthly"
    YEARLY = "yearly"
    ONE_TIME = "one_time"


# What a user can buy for a site. `hosting`: the site runs on our platform. `edit_days`: how long the agent stays
# available after a one-time payment (a plan gives it for as long as it runs).
PLANS = {
    Plan.MONTHLY: {"product": Product.PLAN, "price_usd": 19, "interval": "month", "hosting": True, "product_key": "plan-monthly"},
    Plan.YEARLY: {"product": Product.PLAN, "price_usd": 159, "interval": "year", "hosting": True, "product_key": "plan-yearly"},
    Plan.ONE_TIME: {"product": Product.ONE_TIME, "price_usd": 19, "interval": None, "hosting": False, "edit_days": 30, "product_key": "one-time"},
}  # fmt: skip

# What we charge for a domain, per year: the cheapest point at or above what the registrar charges us for a year
# on average (DOMAIN_BLEND_*). Above the last one, premium, or a TLD that needs more than a year up front: not
# sold. `discountable`: can get the domain discount (with the yearly plan).
# What the domain discount is, as a domain checkout records it (its `discount.terms`). One kind for now: the first
# year free, sold as a trial on the domain's own product (a year's trial set on its checkout -- no charge today,
# then the normal yearly price).
DOMAIN_DISCOUNT_TERMS = "first_year_free"

# A domain's average yearly cost to us, which picks its price point: with a renewal under DOMAIN_BLEND_MAX_RENEWAL_USD,
# DOMAIN_BLEND_FIRST_TERM_WEIGHT of the first term plus the rest of the renewal -- a promo first year (a $2.99 .site
# renewing at $49.99) lowers it some, as most domains are kept a few years; from that renewal up, the renewal alone.
DOMAIN_BLEND_MAX_RENEWAL_USD = 100
DOMAIN_BLEND_FIRST_TERM_WEIGHT = 0.7

DOMAIN_PRICE_POINTS = [
    {"price_usd": 10, "max_registrar_usd": 10, "discountable": True, "product_key": "domain-10"},
    {"price_usd": 15, "max_registrar_usd": 15, "discountable": True, "product_key": "domain-15"},
    {"price_usd": 20, "max_registrar_usd": 20, "discountable": True, "product_key": "domain-20"},
    {"price_usd": 30, "max_registrar_usd": 30, "discountable": False, "product_key": "domain-30"},
    {"price_usd": 40, "max_registrar_usd": 40, "discountable": False, "product_key": "domain-40"},
]  # fmt: skip

# Each product_key above as a product id at the provider, per environment (each Polar world has its own products;
# the products carry the key too, as their `biveit_key` metadata). The sandbox ones are made by
# scripts/create_polar_products.py.
PRODUCT_IDS = {
    Provider.POLAR: {
        "plan-monthly": "c734d67b-5055-4837-90f8-bf50250856a5",
        "plan-yearly": "0df3c4de-c09f-4df7-b4be-3501e65e8fcd",
        "one-time": "97bce6fb-6df1-41d2-9887-37be60c269a8",
        "domain-10": "c009f369-34ba-4b93-9961-50eae021f8f3",
        "domain-15": "86c5f5c4-e5b1-42da-923f-9445bdee2395",
        "domain-20": "f374ac3f-3065-411f-b9fb-bbe0eab7126e",
        "domain-30": "f84ecaa7-1ca0-4640-9f47-7c483128ab4c",
        "domain-40": "249d51c9-dcbe-478f-8008-3dceea364842",
    },
    # Made by scripts/create_polar_products.py (the "Biveit Sandbox" org).
    Provider.POLAR_SANDBOX: {
        "plan-monthly": "426e457e-5072-47b4-afdd-e7fc8d5cd76e",
        "plan-yearly": "e8196628-5499-4cbd-afbb-171372b432b5",
        "one-time": "f86a1cac-c25f-4365-aeb6-1d3057351226",
        "domain-10": "83cac377-8c58-466d-930e-1e34ebf212e5",
        "domain-15": "8d7b951d-33b2-4f00-be1c-b22270959492",
        "domain-20": "9fd2c792-1a3c-4ebc-b788-830cf5250e6e",
        "domain-30": "f2124040-b10e-42dd-9e88-02c1fe3c9a0d",
        "domain-40": "48231569-2016-4272-8c81-abc8b79412e6",
    },
}

# After a plan ends (cancelled and run out, or its payments failed for good), it's in its grace period this long
# (access.is_in_grace) -- what's still allowed then is each entitlement's call; today, hosting and the agent.
# Heads up: a failed renewal already gets the provider's own retry window first (past_due while Polar retries the
# payment), with everything still working -- so after a failed payment it's that
# PLUS this. Worth revisiting (shorter, or grace only after a cancel runs out) once something actually takes sites
# down after it.
GRACE_DAYS = 7

# The statuses that mean a purchase is running; a cancelled one also runs, until its ends_at. past_due counts: the
# provider is still retrying the payment, so the user isn't cut off mid-retry.
RUNNING_STATUSES = {SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE}
# The statuses of a plan that was in effect and has stopped -- the only ones that get GRACE_DAYS after.
GRACE_STATUSES = {SubscriptionStatus.ENDED, SubscriptionStatus.CANCELLED}

# The catch-up for missed webhooks (services/purchases.catch_up_purchases): an unsettled purchase we haven't read
# from Polar for this long is read again (webhooks keep busy ones fresh, so only the quiet ones are asked about), and
# checkouts this recent are looked up if they never got a row.
PURCHASE_STALE_SECONDS = 24 * 60 * 60
CHECKOUT_CATCH_UP_DAYS = 3

# A webhook whose timestamp is further than this from now is refused (Standard Webhooks' replay protection).
WEBHOOK_TOLERANCE_SECONDS = 5 * 60
