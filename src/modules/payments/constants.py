# The payments module's own constants -- kept here rather than in src/constants.py because the module is meant to
# stand on its own. Prices are placeholders to fine-tune (docs/plans/launch-flow.md). Each price also exists at the
# provider as a variant: the provider charges ITS price, these are what we show -- keep the two in step.
# TODO: the variant ids are stand-ins until the Lemon Squeezy store exists.

from enum import StrEnum

PROVIDER = "lemonsqueezy"


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
    statuses onto these (e.g. lemonsqueezy.to_status); its raw object, own status included, stays in the snapshot."""

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
    WEBHOOK = "payment.webhook"  # a provider webhook, raw


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
    Plan.MONTHLY: {"product": Product.PLAN, "price_usd": 19, "interval": "month", "hosting": True, "variant_id": "mock-plan-monthly"},
    Plan.YEARLY: {"product": Product.PLAN, "price_usd": 159, "interval": "year", "hosting": True, "variant_id": "mock-plan-yearly"},
    Plan.ONE_TIME: {"product": Product.ONE_TIME, "price_usd": 19, "interval": None, "hosting": False, "edit_days": 30, "variant_id": "mock-one-time"},
}  # fmt: skip

# What we charge for a domain, per year: the cheapest point at or above what the registrar charges us for a year
# (the higher of its registration and renewal price). Above the last one, premium, or a TLD that needs more than
# a year up front: not sold. `discountable`: can get the domain discount (with the yearly plan). Today that discount
# is the first year free, sold as `discounted_variant_id` -- a trial variant: no charge today, then the normal yearly
# price. How it's applied may change with the real payment provider (a discount code, say).
# What the domain discount is, as a domain checkout records it (its `discount.terms`). One kind for now.
DOMAIN_DISCOUNT_TERMS = "first_year_free"

DOMAIN_PRICE_POINTS = [
    {"price_usd": 10, "max_registrar_usd": 10, "discountable": True, "variant_id": "mock-domain-10", "discounted_variant_id": "mock-domain-10-free"},
    {"price_usd": 15, "max_registrar_usd": 15, "discountable": True, "variant_id": "mock-domain-15", "discounted_variant_id": "mock-domain-15-free"},
    {"price_usd": 20, "max_registrar_usd": 20, "discountable": True, "variant_id": "mock-domain-20", "discounted_variant_id": "mock-domain-20-free"},
    {"price_usd": 30, "max_registrar_usd": 30, "discountable": False, "variant_id": "mock-domain-30", "discounted_variant_id": None},
    {"price_usd": 40, "max_registrar_usd": 40, "discountable": False, "variant_id": "mock-domain-40", "discounted_variant_id": None},
]  # fmt: skip

# After a plan ends (cancelled and run out, or its payments failed for good), it's in its grace period this long
# (access.is_in_grace) -- what's still allowed then is each entitlement's call; today, hosting and the agent.
# Heads up: a failed renewal already gets the provider's own retry window first (past_due, ~2 weeks on Lemon
# Squeezy, configurable on Stripe / Paddle), with everything still working -- so after a failed payment it's that
# PLUS this. Worth revisiting (shorter, or grace only after a cancel runs out) once something actually takes sites
# down after it.
GRACE_DAYS = 7

# The statuses that mean a purchase is running; a cancelled one also runs, until its ends_at. past_due counts: the
# provider is still retrying the payment, so the user isn't cut off mid-retry.
RUNNING_STATUSES = {SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE}
# The statuses of a plan that was in effect and has stopped -- the only ones that get GRACE_DAYS after.
GRACE_STATUSES = {SubscriptionStatus.ENDED, SubscriptionStatus.CANCELLED}

# How long a checkout link stays valid.
CHECKOUT_TTL_SECONDS = 2 * 60 * 60
