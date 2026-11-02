# Intentions: make our catalogue (payments constants: PLANS, DOMAIN_PRICE_POINTS) as products in a Polar world that
# doesn't have them yet -- in practice the sandbox, since the live products were made by hand -- and print the
# product ids to paste into constants.PRODUCT_IDS. A product already there (matched by its `biveit_key` metadata) is
# left alone and just printed, so it's safe to run again.
#
# Run from the backend folder, with that world's token in the env (POLAR_SANDBOX_ACCESS_TOKEN / POLAR_ACCESS_TOKEN):
#   uv run python -m scripts.create_polar_products                      # sandbox, dry run: what it would make
#   uv run python -m scripts.create_polar_products --apply              # sandbox, for real
#   uv run python -m scripts.create_polar_products --live --apply       # live (needs --live; normally not needed)

import asyncio
import sys

import pydash

from src.modules.payments.constants import DOMAIN_PRICE_POINTS, PLANS, Plan, Provider
from src.modules.payments.services import polar
from src.utils import http

_PLAN_NAMES = {Plan.MONTHLY: "Monthly plan", Plan.YEARLY: "Yearly plan", Plan.ONE_TIME: "Code only"}


def to_product_specs() -> list[dict]:
    """[Pure] Every product we sell, as Polar's product-create body (private: only reachable through our checkouts)."""
    plans = [
        {
            "name": _PLAN_NAMES[plan_key],
            "recurring_interval": plan["interval"],
            "prices": [{"amount_type": "fixed", "price_amount": plan["price_usd"] * 100, "price_currency": "usd"}],
            "metadata": {"biveit_key": plan["product_key"]},
            "visibility": "private",
        }
        for plan_key, plan in PLANS.items()
    ]
    domains = [
        {
            "name": f"Domain (${point['price_usd']}/year)",
            "recurring_interval": "year",
            "prices": [{"amount_type": "fixed", "price_amount": point["price_usd"] * 100, "price_currency": "usd"}],
            "metadata": {"biveit_key": point["product_key"]},
            "visibility": "private",
        }
        for point in DOMAIN_PRICE_POINTS
    ]
    return [*plans, *domains]


async def main() -> None:
    await http.connect()
    try:
        await _create_products(sys.argv[1:])
    finally:
        await http.close()


async def _create_products(args: list[str]) -> None:
    provider = Provider.POLAR if "--live" in args else Provider.POLAR_SANDBOX
    apply = "--apply" in args
    existing = await polar.find_products(provider)
    existing_by_key = {pydash.get(product, "metadata.biveit_key"): product for product in existing}
    print(f"{provider}: {len(existing)} products there now")
    for spec in to_product_specs():
        key = spec["metadata"]["biveit_key"]
        known = existing_by_key.get(key)
        if known:
            print(f'    "{key}": "{known["id"]}",  # already there')
        elif apply:
            created = await polar.create_product(provider, spec)
            print(f'    "{key}": "{created["id"]}",  # created')
        else:
            print(f"    {key}: would create {spec['name']}")
    if not apply:
        print("dry run -- nothing created (add --apply)")


if __name__ == "__main__":
    asyncio.run(main())
