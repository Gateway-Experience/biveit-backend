# Intentions: finding a domain for a site -- what's free, and what it costs.
#   GET /domains/search?q=        (auth)  the exact name typed, across our TLDs (exact), plus buyable ideas
#                                         around it (suggestions, minus the exact ones); offers priced in USD
#   GET /domains/{domain}/pricing (auth)  registration / renewal / transfer price for a term (?years=, default
#                                         the TLD's minimum term)
# Errors: 400 INVALID_DOMAIN_QUERY when the query (or domain) isn't usable, 429 DOMAIN_SEARCH_RATE_LIMITED when
# the registrar's account-wide limit is hit.
# Auth'd (guests count) so anonymous traffic can't burn the registrar's per-account rate limit.
#
# Plus the admin test bench (the FE's /admin/domain-name-test): every domain operation, raw, so they can be tried
# by hand (the domains module picks the registrar; account-level calls hit the default one). SANDBOX ONLY -- admin_router refuses everything (409 NOT_SANDBOX) while the default registrar points
# at its live API, prod included, so nobody buys a real domain from a test page.
#   GET    /admin/domain-test/account                     where we're pointed, and the balance
#   GET    /admin/domain-test/domains                     the domains our (sandbox) account holds
#   POST   /admin/domain-test/domains                     buy one: {domain, years, registrant}
#   GET    /admin/domain-test/domains/{domain}            one, in the registrar contract's shape
#   PATCH  /admin/domain-test/domains/{domain}            {autorenewEnabled | privacyEnabled | locked}
#   POST   /admin/domain-test/domains/{domain}/renew      {years}
#   PUT    /admin/domain-test/domains/{domain}/nameservers {nameservers}
#   GET    /admin/domain-test/domains/{domain}/auth-code
#   GET    /admin/domain-test/domains/{domain}/records    + POST (add), PUT/DELETE .../records/{id}
#   GET    /admin/domain-test/verifications               contacts still owing ICANN email verification
#   POST   /admin/domain-test/verifications/{id}/resend   send the verification email again
# Search and pricing are the user endpoints above. Registrar refusals come back with the registrar's own status
# (4xx) and message as the detail, so the page shows exactly why.

import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from src.dto.users import User
from src.modules import domains
from src.utils.auth import get_current_user, require_admin
from src.utils.logging import log_http_error


def _require_sandbox() -> None:
    if domains.is_live():
        raise HTTPException(status_code=409, detail="NOT_SANDBOX")


router = APIRouter()
admin_router = APIRouter(prefix="/admin/domain-test", dependencies=[Depends(require_admin), Depends(_require_sandbox)])


@router.get("/domains/search")
async def search_domains(
    request: Request, q: str = Query(min_length=1, max_length=253), user: User = Depends(get_current_user)
) -> dict:
    try:
        return await domains.search_domains(q)
    except Exception as error:
        raise _to_http_error(error, request) from error


@router.get("/domains/{domain_name}/pricing")
async def get_domain_pricing(
    domain_name: str,
    request: Request,
    years: int | None = Query(default=None, ge=1, le=10),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        return await domains.get_domain_pricing(domain_name.strip().lower(), years)
    except Exception as error:
        raise _to_http_error(error, request) from error


def _to_http_error(error: Exception, request: Request) -> HTTPException:
    """Maps what the domains module raises to a response; anything unexpected is logged and becomes a 500."""
    if isinstance(error, domains.InvalidDomainQuery) or (
        isinstance(error, domains.RegistrarError) and error.status == 400
    ):
        return HTTPException(status_code=400, detail="INVALID_DOMAIN_QUERY")
    if isinstance(error, domains.RegistrarError) and error.status == 429:
        return HTTPException(status_code=429, detail="DOMAIN_SEARCH_RATE_LIMITED")
    log_http_error(error, request)
    return HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR")


@admin_router.get("/account")
async def get_test_account(request: Request) -> dict:
    try:
        return {"username": domains.get_account_name(), "balance_usd": await domains.get_balance()}
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.get("/domains")
async def list_test_domains(request: Request, page: int = Query(default=1, ge=1)) -> dict:
    try:
        return await domains.list_domains(page)
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.post("/domains")
async def register_test_domain(
    request: Request, domain: str = Body(), years: int = Body(ge=1, le=10), registrant: dict = Body()
) -> dict:
    try:
        return await domains.register_domain(domain.strip().lower(), years, registrant, str(uuid.uuid4()))
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.get("/domains/{domain_name}")
async def get_test_domain(domain_name: str, request: Request) -> dict:
    try:
        return await domains.get_domain(domain_name)
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.patch("/domains/{domain_name}")
async def update_test_domain(domain_name: str, request: Request, changes: dict = Body()) -> dict:
    try:
        return await domains.update_domain(domain_name, changes)
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.post("/domains/{domain_name}/renew")
async def renew_test_domain(domain_name: str, request: Request, years: int = Body(embed=True, ge=1, le=10)) -> dict:
    try:
        return await domains.renew_domain(domain_name, years)
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.put("/domains/{domain_name}/nameservers")
async def set_test_nameservers(
    domain_name: str, request: Request, nameservers: list[str] = Body(embed=True, min_length=1)
) -> dict:
    try:
        return await domains.set_nameservers(domain_name, nameservers)
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.get("/domains/{domain_name}/auth-code")
async def get_test_auth_code(domain_name: str, request: Request) -> dict:
    try:
        return {"auth_code": await domains.get_auth_code(domain_name)}
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.get("/domains/{domain_name}/records")
async def list_test_records(domain_name: str, request: Request) -> list[dict]:
    try:
        return await domains.list_records(domain_name)
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.post("/domains/{domain_name}/records")
async def create_test_record(domain_name: str, request: Request, record: dict = Body()) -> dict:
    try:
        return await domains.create_record(domain_name, record)
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.put("/domains/{domain_name}/records/{record_id}")
async def update_test_record(domain_name: str, record_id: int, request: Request, record: dict = Body()) -> dict:
    try:
        return await domains.update_record(domain_name, record_id, record)
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.delete("/domains/{domain_name}/records/{record_id}")
async def delete_test_record(domain_name: str, record_id: int, request: Request) -> dict:
    try:
        await domains.delete_record(domain_name, record_id)
        return {}
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.get("/verifications")
async def list_test_verifications(request: Request) -> list[dict]:
    try:
        return await domains.list_unverified_contacts()
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


@admin_router.post("/verifications/{verification_id}/resend")
async def resend_test_verification(verification_id: int, request: Request) -> dict:
    try:
        return await domains.resend_verification(verification_id)
    except Exception as error:
        raise _to_admin_http_error(error, request) from error


def _to_admin_http_error(error: Exception, request: Request) -> HTTPException:
    """Like _to_http_error but raw, for the test bench: a registrar 4xx keeps its status and message, a domain
    that can't be bought is a 409 DOMAIN_UNAVAILABLE; anything else is logged and becomes a 500."""
    if isinstance(error, domains.RegistrarError) and 400 <= error.status < 500:
        return HTTPException(status_code=error.status, detail=error.message)
    if isinstance(error, domains.DomainUnavailable):
        return HTTPException(status_code=409, detail="DOMAIN_UNAVAILABLE")
    log_http_error(error, request)
    return HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR")
