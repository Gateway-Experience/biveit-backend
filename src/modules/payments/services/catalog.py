# Intentions: what we sell, read off the constants -- the plans as the FE shows them, what a domain costs us to sell
# (its price point), and which of our things a provider product is. All pure: no prices are ever looked up anywhere
# else.

import pydash

from src.modules.payments.constants import DOMAIN_PRICE_POINTS, PLANS, PRODUCT_IDS, Plan, Product, Provider


def get_plans() -> list[dict]:
    """[Pure] The plans as the FE shows them: [{key, product, price_usd, interval, hosting, edit_days}]."""
    return [{"key": key, **pydash.omit(plan, "product_key")} for key, plan in PLANS.items()]


def to_domain_offer(registrar_quote: dict) -> dict:
    """[Pure] What we'd charge for a domain, from the registrar's quote ({price_usd, renewal_price_usd, min_years,
    premium, available} -- domains.check_domains' shape): {price_per_year, discountable, sold}. Not sold
    when it's taken, premium, needs more than a year up front, or costs more than our last price point."""
    yearly_cost = max(registrar_quote.get("price_usd") or 0, registrar_quote.get("renewal_price_usd") or 0)
    price_point = _get_price_point(yearly_cost)
    sold = bool(
        registrar_quote.get("available")
        and not registrar_quote.get("premium")
        and registrar_quote.get("min_years") == 1
        and price_point
    )
    return {
        "price_per_year": price_point["price_usd"] if sold and price_point else None,
        "discountable": bool(sold and price_point and price_point["discountable"]),
        "sold": sold,
    }


def get_product_id(provider: Provider, product_key: str) -> str:
    """[Pure] Our product (a PLANS / DOMAIN_PRICE_POINTS product_key) as that provider's product id."""
    product_id = PRODUCT_IDS[provider].get(product_key)
    if not product_id:
        raise ValueError(f"no {provider} product id for {product_key} (constants.PRODUCT_IDS)")
    return product_id


def get_domain_product_key(price_per_year: int) -> str:
    """[Pure] Our product for a domain at one of our prices."""
    price_point = pydash.find(DOMAIN_PRICE_POINTS, {"price_usd": price_per_year})
    if not price_point:
        raise ValueError(f"no domain product for ${price_per_year}/yr")
    return price_point["product_key"]


def describe_product(provider: Provider, product_id: str) -> dict | None:
    """[Pure] What a provider product sells: {product, plan} for a plan / the one-time purchase, {product: domain,
    price_per_year} for a domain. None for a product that isn't ours."""
    product_key = pydash.find_key(PRODUCT_IDS[provider], lambda known_id: known_id and known_id == product_id)
    plan_key = pydash.find_key(PLANS, lambda plan: plan["product_key"] == product_key) if product_key else None
    if plan_key:
        return {"product": PLANS[plan_key]["product"], "plan": Plan(plan_key)}
    price_point = pydash.find(DOMAIN_PRICE_POINTS, {"product_key": product_key}) if product_key else None
    if price_point:
        return {"product": Product.DOMAIN, "price_per_year": price_point["price_usd"]}
    return None


def _get_price_point(yearly_cost: float) -> dict | None:
    """[Pure] The cheapest price point covering a yearly registrar cost; None above the last one (or no cost)."""
    return pydash.find(DOMAIN_PRICE_POINTS, lambda point: 0 < yearly_cost <= point["max_registrar_usd"])
