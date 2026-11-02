# Intentions: the launch flow's endpoints (services/launch; docs/plans/launch-flow.md) -- the caller taking their
# approved project's site live: choosing a plan and a domain, paying, and watching it go live.
#   GET  /projects/{id}/launch                    (auth)  where it stands: stage, plans, what's bought, the domain,
#                                                         the deployment. Poll it: it also moves a waiting launch on
#   GET  /projects/{id}/launch/domains?q=         (auth)  domains to buy, with our price -- for an empty q, ideas
#                                                         around the project's name
#   POST /projects/{id}/launch/domains/check      (auth)  {domain}: can the user's own domain be used, and are its
#                                                         DNS records in place
#   POST /projects/{id}/launch/domains            (auth)  {domain}: add the user's own domain (no plan needed) ->
#                                                         the same answer as /check
#   POST /projects/{id}/launch/checkout           (auth)  {plan} or {domain, registrant} (after a plan) ->
#                                                         {product, url}: one thing to pay for
#   POST /projects/{id}/launch/plan/upgrade       (auth)  the running monthly plan -> yearly (no checkout page)
#   POST /projects/{id}/launch/domains/activate   (auth)  {domain}: switch the site to another of its domains (set up
#                                                         in the background; the current one serves until then) ->
#                                                         {domain, operation_id (the switch, to poll; null: none)}
#   GET  /projects/{id}/launch/domains/switches/{operation_id}  (auth)  how that switch went: deployments.
#                                                         get_domain_switch's shape. 404 if it isn't this project's
# Errors: 404 PROJECT_NOT_FOUND (not the caller's); the launch's own refusals as {code} with their status (409
# ALREADY_SUBSCRIBED, PRICE_CHANGED (+ price_per_year), DOMAIN_TAKEN..., see services/launch/checkouts.py); 502
# REGISTRAR_ERROR when the registrar can't be asked.

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from src.dto.launch import ActivateDomainRequest, DomainCheckRequest, LaunchCheckoutRequest
from src.dto.projects import Project
from src.dto.users import User
from src.modules import deployments, domains, payments
from src.repositories import projects as projects_repo
from src.services.launch import checkouts, offers, status
from src.utils.auth import get_current_user
from src.utils.logging import log_http_error

router = APIRouter()


async def _get_owned_project(project_id: int, user: User) -> Project:
    projects = await projects_repo.find({"id": project_id, "user_id": user["id"]})
    if not projects:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
    return projects[0]


def _to_refusal(refusal: checkouts.LaunchRefused) -> JSONResponse:
    """The same {detail} shape as HTTPException's, plus whatever the FE needs to recover."""
    return JSONResponse(status_code=refusal.status, content={"detail": refusal.code, **refusal.details})


@router.get("/projects/{project_id}/launch")
async def get_launch(project_id: int, request: Request, user: User = Depends(get_current_user)) -> dict:
    try:
        return await status.get_launch_status(await _get_owned_project(project_id, user), user)
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/projects/{project_id}/launch/domains")
async def search_domains(
    project_id: int, request: Request, q: str = Query("", max_length=200), user: User = Depends(get_current_user)
) -> list[dict]:
    """Returned BARE (a plain array)."""
    try:
        return await offers.search_offers(await _get_owned_project(project_id, user), q, payments.is_sandbox(user))
    except HTTPException:
        raise
    except domains.InvalidDomainQuery:
        return []
    except domains.RegistrarError as error:
        log_http_error(error, request)
        raise HTTPException(status_code=502, detail="REGISTRAR_ERROR") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/projects/{project_id}/launch/domains/check")
async def check_domain(
    project_id: int, body: DomainCheckRequest, request: Request, user: User = Depends(get_current_user)
) -> dict:
    try:
        return await deployments.check_external_domain(await _get_owned_project(project_id, user), body.domain)
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/projects/{project_id}/launch/domains", response_model=None)
async def add_own_domain(
    project_id: int, body: DomainCheckRequest, request: Request, user: User = Depends(get_current_user)
) -> dict | JSONResponse:
    try:
        return await status.add_own_domain(await _get_owned_project(project_id, user), body.domain)
    except HTTPException:
        raise
    except checkouts.LaunchRefused as refusal:
        return _to_refusal(refusal)
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/projects/{project_id}/launch/checkout", response_model=None)
async def start_checkout(
    project_id: int, body: LaunchCheckoutRequest, request: Request, user: User = Depends(get_current_user)
) -> dict | JSONResponse:
    try:
        return await checkouts.start_checkout(await _get_owned_project(project_id, user), user, body)
    except HTTPException:
        raise
    except checkouts.LaunchRefused as refusal:
        return _to_refusal(refusal)
    except domains.RegistrarError as error:
        log_http_error(error, request)
        raise HTTPException(status_code=502, detail="REGISTRAR_ERROR") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/projects/{project_id}/launch/plan/upgrade", response_model=None)
async def upgrade_plan(
    project_id: int, request: Request, user: User = Depends(get_current_user)
) -> dict | JSONResponse:
    try:
        await checkouts.upgrade_plan(await _get_owned_project(project_id, user))
        return {"plan": "yearly"}
    except HTTPException:
        raise
    except checkouts.LaunchRefused as refusal:
        return _to_refusal(refusal)
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/projects/{project_id}/launch/domains/activate", response_model=None)
async def activate_domain(
    project_id: int, body: ActivateDomainRequest, request: Request, user: User = Depends(get_current_user)
) -> dict | JSONResponse:
    try:
        operation_id = await status.activate_domain(await _get_owned_project(project_id, user), body.domain)
        return {"domain": body.domain, "operation_id": str(operation_id) if operation_id else None}
    except HTTPException:
        raise
    except checkouts.LaunchRefused as refusal:
        return _to_refusal(refusal)
    except deployments.InvalidDomain as error:
        raise HTTPException(status_code=400, detail="DOMAIN_INVALID") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/projects/{project_id}/launch/domains/switches/{operation_id}")
async def get_domain_switch(
    project_id: int, operation_id: UUID, request: Request, user: User = Depends(get_current_user)
) -> dict:
    try:
        switch = await deployments.get_domain_switch(await _get_owned_project(project_id, user), operation_id)
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
    if switch is None:
        raise HTTPException(status_code=404, detail="SWITCH_NOT_FOUND")
    return switch
