# Intentions: domains as we sell them -- the registrar's results with our price on them (payments' price points),
# for the launch flow's domain step: search results (or ideas around the project's name when nothing's typed yet),
# and a live quote for one exact domain right before paying.

import asyncio

import pydash

from src.dto.projects import Project
from src.modules import domains, payments


async def search_offers(project: Project, query: str, sandbox: bool) -> list[dict]:
    """What the domain step lists: the exact name across our TLDs, then the registrar's ideas -- or, for an empty
    query, its ideas around the project's name. [{domain, available, sold, price_per_year,
    discountable}], sellable ones first (the order otherwise kept). `sandbox`: ask the registrar's sandbox (the
    user's in sandbox mode, payments.is_sandbox). [I/O]"""
    found = await domains.search_domains(query.strip() or project["name"], sandbox)
    offers = [_to_offer(quote) for quote in [*found["exact"], *found["suggestions"]]]
    return pydash.sort_by(offers, lambda offer: not offer["sold"])


async def check_offers(names: list[str], sandbox: bool) -> list[dict]:
    """Many names at once (domains.check_domain_names: a full domain as is, a bare name across our TLDs), in the
    same shape as search_offers' entries and in the order asked -- what the agent uses to check its own name ideas
    in one go. [I/O]"""
    return [_to_offer(quote) for quote in await domains.check_domain_names(names, sandbox)]


async def quote_domain(domain_name: str, sandbox: bool) -> dict:
    """One exact domain, priced live (the same shape as search_offers' entries), at the registrar or its sandbox.
    [I/O]"""
    quote, min_years = await asyncio.gather(
        domains.check_domains(domain_name, [], sandbox=sandbox), domains.get_min_years(domain_name, sandbox)
    )
    return _to_offer({**pydash.head(quote), "min_years": min_years})


def _to_offer(registrar_quote: dict) -> dict:
    """[Pure] A registrar quote as the domain step shows it."""
    return {
        "domain": registrar_quote["domain"],
        "available": registrar_quote["available"],
        **payments.to_domain_offer(registrar_quote),
    }
