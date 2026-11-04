# Intentions: the Name.com registrar client (the contract every client follows is in registrars.py) -- a thin
# async client over their CORE API (https://docs.name.com, OpenAPI spec at
# https://namedotcom-cdn.name.tools/api-info/namecom.api.yaml) -- one function per call we use, passing their
# fields through as they name them. No product rules here (which TLDs, what we charge); that's domains.py's
# business.
#
# How the API works:
# - JSON over HTTPS, HTTP Basic auth with the API username + token. Prices are USD.
# - Sandbox (OTE): https://api.dev.name.com with "<username>-test" + the separate test token, preloaded credit,
#   no sync with production. Production: https://api.name.com. Anything that BUYS must only ever be tried on the
#   sandbox; reads are free on either.
# - Custom verbs sit after a colon in the path (/domains:checkAvailability). Never encode the colon.
# - Limits: 20 req/s and 3,000 req/hour per account, 429 past them (registration also has a per-domain throttle
#   with Retry-After).
# - Failures: non-2xx with {"message": ..., "details": ...}.
# - Creating a domain without contacts silently uses OUR account's default contacts -- i.e. biveit becomes the
#   registrant, the legal owner. create_domain therefore insists on a registrant.
# - Every call takes `sandbox` first: which of our two accounts it goes to (each with its own credentials,
#   settings.namecom_* / namecom_sandbox_*). domains.py binds it, so its callers pick a registrar, not a flag.
# - Spending calls (create, renew) are refused against the live API unless ENV=prod (_ensure_can_spend), so a
#   dev box holding live credentials can read but never buy.
# References: registrars.py (the contract), src/config (namecom_*).

import asyncio
import base64
from typing import Any
from urllib.parse import quote, urlencode

import pydash as _

from src.config import settings
from src.modules.domains.services.registrars import LiveSpendRefused, RegistrarError
from src.utils import http, rate_limit

_REGISTRAR = "namecom"
# Their two worlds, by `sandbox`: live (real domains, real money) and the sandbox (OTE).
_API_URLS = {False: "https://api.name.com", True: "https://api.dev.name.com"}
# How many calls a second we let start, per account. Their cap is 20; half of it leaves room for bursts landing
# on the same second and for the sandbox's limits, which may be tighter than the documented ones.
_MAX_REQUESTS_PER_SECOND = 10
# The most domains one availability check takes.
_CHECK_MAX_DOMAINS = 50


async def check_availability(sandbox: bool, domain_names: list[str]) -> list[dict]:
    """Whether each exact domain can be bought, and for how much: [{"domainName": "foo.io", "purchasable": True,
    "purchasePrice": 53.99, "renewalPrice": 79.99, "purchaseType": "registration", "premium": ...}, ...]. Taken
    domains come back with purchasable False and no prices. purchasePrice is the total for the TLD's minimum term
    (2 years for .ai). They take at most _CHECK_MAX_DOMAINS a call, so more go in several. [I/O]"""
    bodies = await asyncio.gather(
        *[
            _call_api(sandbox, "POST", "domains:checkAvailability", {"domainNames": chunk})
            for chunk in _.chunk(domain_names, _CHECK_MAX_DOMAINS)
        ]
    )
    return _.flatten([_.get(body, "results", []) for body in bodies])


async def search(
    sandbox: bool, keyword: str, tlds: list[str] | None = None, timeout_ms: int | None = None
) -> list[dict]:
    """Name ideas around a keyword (a word, words, or a whole domain), same result shape as check_availability.
    Only plain registrations: aftermarket / backorder listings have unpredictable prices and slow fulfilment,
    and Name.com itself recommends resellers filter them out. timeout_ms (500-12,000) bounds how long they
    search. [I/O]"""
    body = await _call_api(
        sandbox,
        "POST",
        "domains:search",
        _.omit_by(
            {"keyword": keyword, "tldFilter": tlds, "purchaseType": "registration", "timeout": timeout_ms},
            lambda value: value is None,
        ),
    )
    return _.get(body, "results", [])


async def get_pricing(sandbox: bool, domain_name: str, years: int | None = None) -> dict:
    """Registration / renewal / transfer price of a domain for a term (default: the TLD's minimum term). The
    purchasePrice is the total for the term -- the number to send back when creating a premium domain. [I/O]"""
    query = f"?{urlencode({'years': years})}" if years else ""
    return await _call_api(sandbox, "GET", f"domains/{quote(domain_name)}:getPricing{query}")


async def get_min_years(sandbox: bool, domain_name: str) -> int:
    """The shortest term `domain_name`'s TLD can be registered for (1 for most, 2 for .ai). They don't list it
    reliably -- their TLD price list says 1 for everything, .ai included -- but get_pricing refuses a term that's
    too short, so this asks for 1 year, then 2, ... (max 10) until it's accepted. Up to one call per year tried;
    cache the answer. [I/O]"""
    return await _probe_min_years(sandbox, domain_name, 1)


async def _probe_min_years(sandbox: bool, domain_name: str, years: int) -> int:
    try:
        await get_pricing(sandbox, domain_name, years)
        return years
    except RegistrarError as error:
        if years >= 10 or "Invalid value for years" not in error.message:
            raise
        return await _probe_min_years(sandbox, domain_name, years + 1)


async def get_domain(sandbox: bool, domain_name: str) -> dict:
    """A domain in OUR account (nameservers, expiry, locks, auto-renew...). 404 when we don't hold it. [I/O]"""
    return await _call_api(sandbox, "GET", f"domains/{quote(domain_name)}")


async def list_domains(sandbox: bool, page: int = 1, per_page: int = 1000) -> dict:
    """One page of the domains in our account: {"domains": [...same shape as get_domain], "nextPage": 2, ...};
    no nextPage on the last page. [I/O]"""
    return await _call_api(sandbox, "GET", f"domains?{urlencode({'page': page, 'perPage': per_page})}")


async def create_domain(
    sandbox: bool,
    domain_name: str,
    years: int,
    contacts: dict,
    purchase_price: float | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Registers a domain. SPENDS from our account. Returns {"domain": {...}, "order": 123, "totalPaid": 12.99}.
    `contacts` is {"registrant": {...}, "admin": ..., "tech": ..., "billing": ...} in their contact shape
    (firstName, lastName, companyName, address1, city, state, zip, country, email, phone as E.164 "+84901234567"); registrant is
    required -- see the module note. `purchase_price` must be sent, and match exactly, for premium domains (from
    get_pricing with the same years); leave it out for plain ones. A replay with the same `idempotency_key`
    returns the first result instead of buying twice. [I/O]"""
    if not _.get(contacts, "registrant"):
        raise ValueError("create_domain needs a registrant contact, or Name.com makes us the owner")
    _ensure_can_spend(sandbox)
    return await _call_api(
        sandbox,
        "POST",
        "domains",
        _.omit_by(
            {
                "domain": {"domainName": domain_name, "contacts": contacts},
                "years": years,
                "purchaseType": "registration",
                "purchasePrice": purchase_price,
            },
            lambda value: value is None,
        ),
        headers={"X-Idempotency-Key": idempotency_key} if idempotency_key else {},
    )


async def renew_domain(sandbox: bool, domain_name: str, years: int, purchase_price: float | None = None) -> dict:
    """Extends a domain we hold by `years`. SPENDS from our account. `purchase_price` as in create_domain
    (premium only). Returns {"domain": {...}, "order": ..., "totalPaid": ...}. [I/O]"""
    _ensure_can_spend(sandbox)
    return await _call_api(
        sandbox,
        "POST",
        f"domains/{quote(domain_name)}:renew",
        _.omit_by({"years": years, "purchasePrice": purchase_price}, lambda value: value is None),
    )


async def update_domain(sandbox: bool, domain_name: str, changes: dict) -> dict:
    """Flips a domain's switches: any of {"autorenewEnabled", "privacyEnabled", "locked"}. Returns the domain.
    [I/O]"""
    return await _call_api(sandbox, "PATCH", f"domains/{quote(domain_name)}", changes)


async def set_nameservers(sandbox: bool, domain_name: str, nameservers: list[str]) -> dict:
    """Points the domain at these nameservers. Name.com's own DNS (and so the record calls below) only answers
    while it's on their nameservers. Returns the domain. [I/O]"""
    return await _call_api(
        sandbox, "POST", f"domains/{quote(domain_name)}:setNameservers", {"nameservers": nameservers}
    )


async def get_auth_code(sandbox: bool, domain_name: str) -> str:
    """The transfer (EPP) code, for the owner to move the domain elsewhere. A lock can still block the
    transfer even when a code comes back. [I/O]"""
    body = await _call_api(sandbox, "GET", f"domains/{quote(domain_name)}:getAuthCode")
    return body["authCode"]


async def list_records(sandbox: bool, domain_name: str) -> list[dict]:
    """The domain's DNS records on Name.com's DNS: [{"id": 1, "host": "www", "fqdn": "www.foo.com.", "type":
    "CNAME", "answer": "...", "ttl": 300}, ...]. host "" is the apex. [I/O]"""
    body = await _call_api(sandbox, "GET", f"domains/{quote(domain_name)}/records")
    return _.get(body, "records", [])


async def create_record(sandbox: bool, domain_name: str, record: dict) -> dict:
    """Adds a DNS record: {"host": "" for the apex / "www", "type": "A" | "AAAA" | "ANAME" | "CNAME" | "MX" |
    "NS" | "SRV" | "TXT", "answer": ..., "ttl": 300 (optional), "priority": (MX/SRV)}. Returns it with its id.
    [I/O]"""
    return await _call_api(sandbox, "POST", f"domains/{quote(domain_name)}/records", record)


async def update_record(sandbox: bool, domain_name: str, record_id: int, record: dict) -> dict:
    """Replaces record `record_id` with `record` (same shape as create_record). [I/O]"""
    return await _call_api(sandbox, "PUT", f"domains/{quote(domain_name)}/records/{record_id}", record)


async def delete_record(sandbox: bool, domain_name: str, record_id: int) -> None:
    """[I/O]"""
    await _call_api(sandbox, "DELETE", f"domains/{quote(domain_name)}/records/{record_id}")


async def list_unverified_contacts(sandbox: bool) -> list[dict]:
    """Contacts still owing ICANN email verification, across our account: [{"verificationId": 1, "email": ...,
    "domains": [...], "verifyBy": "<UTC>", "createDate": ...}]. Unverified past verifyBy (15 days) -> the domains
    get suspended. New domains only show up here ~10 minutes after purchase (a scheduled job on their side).
    [I/O]"""
    body = await _call_api(sandbox, "GET", "contacts/unverified")
    return _.get(body, "unverifiedContacts", [])


async def resend_verification(sandbox: bool, verification_id: int) -> dict:
    """Sends the verification email again. Throttled: once per 15 minutes per verification (429 otherwise, with
    the earliest retry in the details). Returns {"nextEligibleAt": ...}. [I/O]"""
    return await _call_api(sandbox, "POST", f"contacts/verify/{verification_id}:resend")


def get_account_name(sandbox: bool) -> str:
    """Which of our accounts the client acts as (the API username -- "<username>-test" on the sandbox). [Pure-ish]"""
    return _get_credentials(sandbox)["username"]


async def get_balance(sandbox: bool) -> float:
    """Our account's prepaid balance in USD -- what registrations and renewals draw on. [I/O]"""
    body = await _call_api(sandbox, "GET", "accountinfo/balance")
    return body["balance"]


def _ensure_can_spend(sandbox: bool) -> None:
    """The live-spend guard (see the module note). [Pure-ish]"""
    if not sandbox and settings.env != "prod":
        raise LiveSpendRefused(f"refusing to spend on live Name.com with ENV={settings.env}; use the sandbox")


def _get_credentials(sandbox: bool) -> dict:
    if sandbox:
        return {"username": settings.namecom_sandbox_username, "token": settings.namecom_sandbox_api_token}
    return {"username": settings.namecom_username, "token": settings.namecom_api_token}


async def _call_api(
    sandbox: bool, method: str, path: str, body: dict | None = None, headers: dict | None = None
) -> Any:
    account = _get_credentials(sandbox)
    if not account["username"] or not account["token"]:
        raise RegistrarError(_REGISTRAR, 401, f"no Name.com {'sandbox' if sandbox else 'live'} credentials configured")
    credentials = base64.b64encode(f"{account['username']}:{account['token']}".encode()).decode()
    await rate_limit.wait_for_turn(f"{_REGISTRAR}:{'sandbox' if sandbox else 'live'}", _MAX_REQUESTS_PER_SECOND)
    status, response, _headers = await http.request(
        method,
        f"{_API_URLS[sandbox]}/core/v1/{path}",
        json=body,
        headers={"Authorization": f"Basic {credentials}", **(headers or {})},
    )
    if not 200 <= status < 300:
        raise RegistrarError(_REGISTRAR, status, str(_.get(response, "message") or "no error message"))
    return response
