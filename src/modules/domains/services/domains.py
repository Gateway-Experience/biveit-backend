# Intentions: everything the app does with domains, registrar-agnostic: turning what a user typed into domains
# they could buy, priced; buying them; and managing the ones we hold. Which registrar handles a domain is decided
# here (the routing table, or the caller's `registrar=` -- for a domain we hold, the one it was registered with),
# so callers never deal with registrars. The registrars' APIs live in their own client modules (namecom.py), all
# following registrars.py's contract.

import asyncio
import re
from types import ModuleType

import pydash as _

from src.modules.domains.constants import DEFAULT_REGISTRAR, MIN_TERM_CACHE_SECONDS, REGISTRAR_BY_TLD, SEARCH_TLDS
from src.modules.domains.services import namecom
from src.modules.domains.services.registrars import RegistrarError
from src.utils import cache

# Every registrar client we have, by the name domains and the routing table refer to them with.
_REGISTRARS: dict[str, ModuleType] = {"namecom": namecom}


class InvalidDomainQuery(Exception):
    """What the user typed has no usable name in it (e.g. only punctuation)."""


class DomainUnavailable(Exception):
    """The domain can't be bought as a plain registration right now (taken, or only aftermarket/backorder)."""


async def search_domains(query: str) -> dict:
    """What a domain search shows: {"exact": the name as typed across our TLDs (check_domains), "suggestions":
    buyable ideas around it (suggest_domains)}. The registrar's ideas often include the exact domains again, so
    those are dropped from suggestions. Available offers also get `min_years`, the term their price is for
    (None on taken ones). [I/O]"""
    exact, suggestions = await asyncio.gather(check_domains(query), suggest_domains(query))
    exact_domains = {offer["domain"] for offer in exact}
    new_suggestions = [offer for offer in suggestions if offer["domain"] not in exact_domains]
    available_domains = [offer["domain"] for offer in exact + new_suggestions if offer["available"]]
    min_years_by_domain = dict(
        zip(available_domains, await asyncio.gather(*map(get_min_years, available_domains)), strict=True)
    )
    return {
        "exact": [{**offer, "min_years": min_years_by_domain.get(offer["domain"])} for offer in exact],
        "suggestions": [{**offer, "min_years": min_years_by_domain.get(offer["domain"])} for offer in new_suggestions],
    }


async def get_min_years(domain_name: str) -> int:
    """The shortest term `domain_name`'s TLD can be registered for -- 1 for most, 2 for .ai. Asking can be
    expensive (Name.com has to be probed), so it's cached per TLD (MIN_TERM_CACHE_SECONDS). [I/O]"""
    tld = domain_name.partition(".")[2]
    return await cache.with_cache(
        f"domains:min_years:{tld}", MIN_TERM_CACHE_SECONDS, lambda: _get_client(domain_name).get_min_years(domain_name)
    )


async def check_domains(query: str, tlds: list[str] = SEARCH_TLDS) -> list[dict]:
    """The exact name the user typed, across `tlds` in that order, as offers (_to_offer). A full domain ("Foo.io",
    "https://www.foo.io/about") puts its own TLD first. [I/O]"""
    name, typed_tld = _split_domain(query)
    domain_names = [f"{name}.{tld}" for tld in _.uniq(_.compact([typed_tld, *tlds]))]
    # Each registrar checks the TLDs it sells; the answers are put back in the order asked.
    names_by_registrar = _.group_by(domain_names, _route_registrar)
    results = await asyncio.gather(
        *[_REGISTRARS[registrar].check_availability(names) for registrar, names in names_by_registrar.items()]
    )
    result_by_domain = {result["domainName"]: result for result in _.flatten(results)}
    return [_to_offer(result_by_domain.get(domain_name, {"domainName": domain_name})) for domain_name in domain_names]


async def suggest_domains(query: str, tlds: list[str] | None = None) -> list[dict]:
    """Name ideas around what the user typed, as offers (_to_offer), only ones that can be bought. The ideas come
    from the default registrar. [I/O]"""
    if not _split_domain(query)[0]:
        raise InvalidDomainQuery(query)
    results = await _REGISTRARS[DEFAULT_REGISTRAR].search(query.strip(), tlds)
    return [offer for offer in map(_to_offer, results) if offer["available"]]


async def get_domain_pricing(domain_name: str, years: int | None = None) -> dict:
    """What a domain costs for a term (default: the TLD's minimum, 2 years for .ai), in USD: the totals for
    registering, renewing and transferring it for that term. [I/O]"""
    term_years = years or await get_min_years(domain_name)
    pricing = await _get_client(domain_name).get_pricing(domain_name, term_years)
    return {
        "domain": domain_name,
        "years": term_years,
        "premium": bool(pricing.get("premium")),
        "price_usd": pricing.get("purchasePrice"),
        "renewal_price_usd": pricing.get("renewalPrice"),
        "transfer_price_usd": pricing.get("transferPrice"),
    }


async def register_domain(domain_name: str, years: int, registrant: dict, idempotency_key: str) -> dict:
    """Buys `domain_name` for `years` with the user as `registrant` (the contact shape in registrars.py; its
    optional companyName, if filled, makes that company the legal owner), at the registrar the routing table
    picks. SPENDS from our balance there.

    Availability and price are re-checked live first: a search result is only advisory, and premium prices must
    be echoed back exactly. The user fills every contact role, so none of them fall back to our account's
    defaults. Pass the same `idempotency_key` (e.g. our order id) on a retry and the registrar replays the first
    result instead of buying twice. Returns which registrar holds it now -- store that, it's where every later
    call for this domain goes. Raises DomainUnavailable -- also when the registry turns it down at purchase time
    (a reserved name the availability check still calls available). [I/O]"""
    registrar_name = _route_registrar(domain_name)
    registrar = _REGISTRARS[registrar_name]
    check = _.head(await registrar.check_availability([domain_name])) or {}
    if not _to_offer({"domainName": domain_name, **check})["available"]:
        raise DomainUnavailable(domain_name)
    purchase_price = (
        (await registrar.get_pricing(domain_name, years))["purchasePrice"] if check.get("premium") else None
    )
    try:
        created = await registrar.create_domain(
            domain_name,
            years,
            {role: registrant for role in ["registrant", "admin", "tech", "billing"]},
            purchase_price,
            idempotency_key,
        )
    except RegistrarError as error:
        # Intentions: the registry said no to this name (Name.com words it "Domain registration rejected by
        # registry") -- as unbuyable as a taken one. Other 400s (a bad contact field) are our problem, not the
        # domain's, so they stay errors.
        if error.status == 400 and "rejected by registry" in error.message.lower():
            raise DomainUnavailable(domain_name) from error
        raise
    return {
        "domain": created["domain"]["domainName"],
        "expires_at": created["domain"].get("expireDate"),
        "registrar": registrar_name,
        "registrar_order_id": created["order"],
        "paid_usd": created["totalPaid"],
    }


# --- Managing what we hold -------------------------------------------------------------------------------------
# Each takes an optional `registrar`: for a domain, pass the one it was registered with (register_domain returns
# it) -- that's where it lives, whatever the routing table says today; left out, the domain's TLD picks. Account-
# level calls default to the default registrar. Shapes are registrars.py's (Name.com's field names).


async def get_domain(domain_name: str, registrar: str | None = None) -> dict:
    """A domain we hold: expireDate, locked, transferLockExpiresAt, autorenewEnabled, nameservers, contacts (with
    isVerified)... 404 (RegistrarError) when we don't hold it. [I/O]"""
    return await _get_client(domain_name, registrar).get_domain(domain_name)


async def update_domain(domain_name: str, changes: dict, registrar: str | None = None) -> dict:
    """Flips any of {"autorenewEnabled", "privacyEnabled", "locked"}. Unlocking is refused (409) for 60 days after
    a registration or transfer -- ICANN's transfer lock. Returns the domain. [I/O]"""
    return await _get_client(domain_name, registrar).update_domain(domain_name, changes)


async def renew_domain(
    domain_name: str, years: int, purchase_price: float | None = None, registrar: str | None = None
) -> dict:
    """Extends a domain by `years`. SPENDS. `purchase_price` only for premium domains (must match exactly). [I/O]"""
    return await _get_client(domain_name, registrar).renew_domain(domain_name, years, purchase_price)


async def set_nameservers(domain_name: str, nameservers: list[str], registrar: str | None = None) -> dict:
    """Hands the domain's DNS to these nameservers (they must exist). Records set through us stop mattering once
    it's off the registrar's own nameservers. [I/O]"""
    return await _get_client(domain_name, registrar).set_nameservers(domain_name, nameservers)


async def get_auth_code(domain_name: str, registrar: str | None = None) -> str:
    """The transfer code, for the owner to move the domain to another registrar. A lock can still block the
    transfer itself. [I/O]"""
    return await _get_client(domain_name, registrar).get_auth_code(domain_name)


async def list_records(domain_name: str, registrar: str | None = None) -> list[dict]:
    """The domain's DNS records ({"id", "host" ("" = apex), "type", "answer", "ttl"}) on the registrar's DNS. [I/O]"""
    return await _get_client(domain_name, registrar).list_records(domain_name)


async def create_record(domain_name: str, record: dict, registrar: str | None = None) -> dict:
    """Adds a DNS record ({"host", "type", "answer", "ttl"?, "priority"?}). Returns it with its id. [I/O]"""
    return await _get_client(domain_name, registrar).create_record(domain_name, record)


async def update_record(domain_name: str, record_id: int, record: dict, registrar: str | None = None) -> dict:
    """[I/O]"""
    return await _get_client(domain_name, registrar).update_record(domain_name, record_id, record)


async def delete_record(domain_name: str, record_id: int, registrar: str | None = None) -> None:
    """[I/O]"""
    await _get_client(domain_name, registrar).delete_record(domain_name, record_id)


async def list_domains(page: int = 1, registrar: str = DEFAULT_REGISTRAR) -> dict:
    """One page of the domains our account at `registrar` holds: {"domains": [...], "nextPage": ...}. [I/O]"""
    return await _REGISTRARS[registrar].list_domains(page)


async def list_unverified_contacts(registrar: str = DEFAULT_REGISTRAR) -> list[dict]:
    """Owners still owing ICANN email verification at `registrar` (their domains get suspended past verifyBy).
    [I/O]"""
    return await _REGISTRARS[registrar].list_unverified_contacts()


async def resend_verification(verification_id: int, registrar: str = DEFAULT_REGISTRAR) -> dict:
    """Sends a pending verification email again (throttled: once per 15 minutes per verification). [I/O]"""
    return await _REGISTRARS[registrar].resend_verification(verification_id)


async def get_balance(registrar: str = DEFAULT_REGISTRAR) -> float:
    """Our prepaid USD balance at `registrar` -- what registrations and renewals draw on. [I/O]"""
    return await _REGISTRARS[registrar].get_balance()


def get_account_name(registrar: str = DEFAULT_REGISTRAR) -> str:
    """Which of our accounts at `registrar` we act as. [Pure-ish]"""
    return _REGISTRARS[registrar].get_account_name()


def is_live(registrar: str = DEFAULT_REGISTRAR) -> bool:
    """Whether `registrar` is configured against production (real domains, real money) rather than a sandbox.
    [Pure-ish]"""
    return _REGISTRARS[registrar].is_live()


def _route_registrar(domain_name: str) -> str:
    """[Pure] Which registrar a NEW registration of `domain_name` goes to: constants.REGISTRAR_BY_TLD,
    matched on the full TLD ("com.vn"), then its last label ("vn"), else the default."""
    tld = domain_name.partition(".")[2]
    routed_tld = _.find([tld, tld.rpartition(".")[2]], lambda candidate: candidate in REGISTRAR_BY_TLD)
    return REGISTRAR_BY_TLD.get(routed_tld, DEFAULT_REGISTRAR)


def _get_client(domain_name: str, registrar: str | None = None) -> ModuleType:
    """[Pure] The client for a domain: the named registrar, else the one its TLD routes to."""
    return _REGISTRARS[registrar or _route_registrar(domain_name)]


def _to_offer(result: dict) -> dict:
    """[Pure] A registrar result in our shape. Only a plain registration counts as available: aftermarket and
    backorder listings aren't something we can sell in one click. Prices are the registrar's, in USD, for the
    TLD's minimum term (2 years for .ai) -- what we charge on top is a product decision still to make."""
    available = bool(result.get("purchasable")) and result.get("purchaseType") == "registration"
    return {
        "domain": result["domainName"],
        "available": available,
        "premium": bool(result.get("premium")),
        "price_usd": result.get("purchasePrice") if available else None,
        "renewal_price_usd": result.get("renewalPrice") if available else None,
    }


def _split_domain(query: str) -> tuple[str, str | None]:
    """[Pure] "https://www.My Shop.co.uk/x" -> ("myshop", "co.uk"); "foo" -> ("foo", None). Anything that can't be
    in a (non-IDN) label is dropped. Raises InvalidDomainQuery when no name is left."""
    host = query.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
    raw_name, _dot, tld = host.partition(".")
    name = re.sub(r"[^a-z0-9-]", "", raw_name).strip("-")
    if not name:
        raise InvalidDomainQuery(query)
    return name, re.sub(r"[^a-z0-9.]", "", tld) or None
