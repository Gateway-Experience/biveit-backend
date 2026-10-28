# The payments module's own constants -- kept here rather than in src/constants.py because the module is meant to
# stand on its own. Prices are placeholders to fine-tune (docs/plans/launch-flow.md). Each price also exists at the
# provider as a variant: the provider charges ITS price, these are what we show -- keep the two in step.
# TODO: the variant ids are stand-ins until the Lemon Squeezy store exists.

from enum import StrEnum

PROVIDER = "lemonsqueezy"


class Product(StrEnum):
    """What a purchase is for (subscriptions.product / orders.product)."""

    PLAN = "plan"  # hosting + the agent, monthly or yearly
    DOMAIN = "domain"  # one domain bought through us, yearly
    ONE_TIME = "one_time"  # just the code


class TargetType(StrEnum):
    """What a purchase was bought for (subscriptions / orders .target_type, with target_id)."""

    PROJECT = "PROJECT"


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
# a year up front: not sold. `free_first_year`: free for its first year when bought with the yearly plan -- that's
# the trial variant (no charge today, then the normal yearly price).
DOMAIN_PRICE_POINTS = [
    {"price_usd": 10, "max_registrar_usd": 10, "free_first_year": True, "variant_id": "mock-domain-10", "trial_variant_id": "mock-domain-10-free"},
    {"price_usd": 15, "max_registrar_usd": 15, "free_first_year": True, "variant_id": "mock-domain-15", "trial_variant_id": "mock-domain-15-free"},
    {"price_usd": 20, "max_registrar_usd": 20, "free_first_year": True, "variant_id": "mock-domain-20", "trial_variant_id": "mock-domain-20-free"},
    {"price_usd": 30, "max_registrar_usd": 30, "free_first_year": False, "variant_id": "mock-domain-30", "trial_variant_id": None},
    {"price_usd": 40, "max_registrar_usd": 40, "free_first_year": False, "variant_id": "mock-domain-40", "trial_variant_id": None},
]  # fmt: skip

# After a plan ends (cancelled and run out, or its payments failed for good), the site stays up this long.
HOSTING_GRACE_DAYS = 7

# The provider's subscription statuses that mean it's running. A cancelled one keeps running until its ends_at.
RUNNING_STATUSES = {"on_trial", "active", "past_due"}

# How long a checkout link stays valid.
CHECKOUT_TTL_SECONDS = 2 * 60 * 60
