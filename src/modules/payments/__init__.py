# The payments module -- what users buy from us, through the payment provider (Lemon Squeezy, merchant of record):
# plans (monthly / yearly: hosting + the agent), the one-time purchase (just the code), and domains (one yearly
# subscription each). It starts purchases (checkout links), takes in the provider's webhooks into our own copy of
# what's been bought (subscriptions, orders), and answers what a user may do with a project because of it (access).
#
# The provider is the source of truth; our rows follow it through webhooks (each re-fetches the object). With
# settings.payments_mock on, a stand-in replaces the provider: fake checkout pages, webhooks sent to ourselves.
# What we sell and for how much: constants.py. The business rules: docs/plans/launch-flow.md.
# Callers import THIS package (`from src.modules import payments`), not the files.

from src.modules.payments.constants import DOMAIN_PRICE_POINTS, PLANS, Plan, Product, TargetType
from src.modules.payments.repositories.orders import find as find_orders
from src.modules.payments.repositories.subscriptions import find as find_subscriptions
from src.modules.payments.services.access import get_project_access, is_running, to_access
from src.modules.payments.services.catalog import describe_variant, get_plans, to_domain_offer
from src.modules.payments.services.checkouts import create_domain_checkout, create_plan_checkout
from src.modules.payments.services.lemonsqueezy import ProviderError, cancel_subscription, refund_latest_payment
from src.modules.payments.services.mock_lemonsqueezy import MockCheckoutNotFound
from src.modules.payments.services.mock_lemonsqueezy import get_checkout as get_mock_checkout
from src.modules.payments.services.mock_lemonsqueezy import pay as pay_mock_checkout
from src.modules.payments.services.webhooks import InvalidSignature, handle_webhook

__all__ = [
    "DOMAIN_PRICE_POINTS",
    "PLANS",
    "InvalidSignature",
    "MockCheckoutNotFound",
    "Plan",
    "Product",
    "ProviderError",
    "TargetType",
    "cancel_subscription",
    "create_domain_checkout",
    "create_plan_checkout",
    "describe_variant",
    "find_orders",
    "find_subscriptions",
    "get_mock_checkout",
    "get_plans",
    "get_project_access",
    "handle_webhook",
    "is_running",
    "pay_mock_checkout",
    "refund_latest_payment",
    "to_access",
    "to_domain_offer",
]
