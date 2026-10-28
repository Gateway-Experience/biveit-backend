# Intentions: what we sell, read off the constants -- the plans as the FE shows them, what a domain costs us to sell
# (its price point), and which product a provider variant is. All pure: no prices are ever looked up anywhere else.

import pydash

from src.modules.payments.constants import DOMAIN_PRICE_POINTS, PLANS, Plan, Product


def get_plans() -> list[dict]:
    """[Pure] The plans as the FE shows them: [{key, product, price_usd, interval, hosting, edit_days}]."""
    return [{"key": key, **pydash.omit(plan, "variant_id")} for key, plan in PLANS.items()]


def to_domain_offer(registrar_quote: dict) -> dict:
    """[Pure] What we'd charge for a domain, from the registrar's quote ({price_usd, renewal_price_usd, min_years,
    premium, available} -- domains.check_domains' shape): {price_per_year, free_first_year_eligible, sold}. Not sold
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
        "free_first_year_eligible": bool(sold and price_point and price_point["free_first_year"]),
        "sold": sold,
    }


def get_domain_variant(price_per_year: int, free_first_year: bool) -> str:
    """[Pure] The provider variant for a domain at one of our prices: the trial one for a free first year."""
    price_point = pydash.find(DOMAIN_PRICE_POINTS, {"price_usd": price_per_year})
    if not price_point or (free_first_year and not price_point["trial_variant_id"]):
        raise ValueError(f"no domain variant for ${price_per_year}/yr (free first year: {free_first_year})")
    return price_point["trial_variant_id"] if free_first_year else price_point["variant_id"]


def describe_variant(variant_id: str) -> dict | None:
    """[Pure] What a provider variant sells: {product, plan} for a plan / the one-time purchase, {product: domain,
    price_per_year, free_first_year} for a domain. None for a variant that isn't ours."""
    plan_key = pydash.find_key(PLANS, lambda plan: plan["variant_id"] == variant_id)
    if plan_key:
        return {"product": PLANS[plan_key]["product"], "plan": Plan(plan_key)}
    for price_point in DOMAIN_PRICE_POINTS:
        if variant_id in {price_point["variant_id"], price_point["trial_variant_id"]}:
            return {
                "product": Product.DOMAIN,
                "price_per_year": price_point["price_usd"],
                "free_first_year": variant_id == price_point["trial_variant_id"],
            }
    return None


def _get_price_point(yearly_cost: float) -> dict | None:
    """[Pure] The cheapest price point covering a yearly registrar cost; None above the last one (or no cost)."""
    return pydash.find(DOMAIN_PRICE_POINTS, lambda point: 0 < yearly_cost <= point["max_registrar_usd"])
