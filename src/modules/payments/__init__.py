# The payments module -- what users buy from us, through the payment provider (Polar, merchant of record): plans
# (monthly / yearly: hosting + the agent), the one-time purchase (just the code), and domains (one yearly
# subscription each). It starts purchases (checkouts, each recorded as a payment.checkout_created event), takes in
# the provider's webhooks into our own copy of what's been bought (subscriptions -- recurring, and lifetime for the
# one-time purchase), and answers what a user may do with a project because of it (access).
#
# The provider is the source of truth; our rows follow it through webhooks (each re-fetches the object). Polar has two
# worlds, live and the sandbox (test cards): everyone pays on the server's default one, except admins who switch
# their own payments to the sandbox; each purchase stays in the world it was made in (constants.Provider).
# What we sell and for how much: constants.py. The business rules: docs/launchflow.txt.
# Callers import THIS package (`from src.modules import payments`), not the files.

from src.modules.payments.constants import (
    DOMAIN_DISCOUNT_TERMS,
    DOMAIN_PRICE_POINTS,
    PLANS,
    Billing,
    Plan,
    Product,
    Provider,
    ProviderObject,
    SubscriptionStatus,
    TargetType,
)
from src.modules.payments.repositories.subscriptions import find as find_subscriptions
from src.modules.payments.services.access import get_project_access, is_in_grace, is_running, to_access
from src.modules.payments.services.catalog import describe_product, get_plans, to_domain_offer
from src.modules.payments.services.checkouts import (
    change_plan,
    create_domain_checkout,
    create_plan_checkout,
    get_billing_portal_url,
    get_payment_provider,
    get_recorded_checkout,
    is_sandbox,
)
from src.modules.payments.services.polar import ProviderError
from src.modules.payments.services.purchases import catch_up_purchases
from src.modules.payments.services.subscriptions import refund_latest_payment, revoke_subscription
from src.modules.payments.services.webhooks import InvalidSignature, handle_webhook

__all__ = [
    "DOMAIN_DISCOUNT_TERMS",
    "DOMAIN_PRICE_POINTS",
    "PLANS",
    "Billing",
    "InvalidSignature",
    "Plan",
    "Product",
    "Provider",
    "ProviderError",
    "ProviderObject",
    "SubscriptionStatus",
    "TargetType",
    "catch_up_purchases",
    "change_plan",
    "create_domain_checkout",
    "create_plan_checkout",
    "describe_product",
    "find_subscriptions",
    "get_billing_portal_url",
    "get_payment_provider",
    "get_plans",
    "get_project_access",
    "get_recorded_checkout",
    "handle_webhook",
    "is_in_grace",
    "is_running",
    "is_sandbox",
    "refund_latest_payment",
    "revoke_subscription",
    "to_access",
    "to_domain_offer",
]
