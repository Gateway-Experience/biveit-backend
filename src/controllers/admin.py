# Intentions: the admin dashboard's endpoints (the FE's /admin pages) -- the platform at a glance, any
# project's details, turning a project off. Admins only (users.role ADMIN, see utils/auth.require_admin):
# 401 when nobody's logged in, 403 for everyone else.
#   GET  /admin/server-usage          the machine's CPU, memory and disk (the dashboard polls it)
#   GET  /admin/platform              {sections, projects}: services/platform_status's platform sections, and
#                                     every live project with its owner and whether its container runs
#   GET  /admin/projects/{id}         {project, sections}: one project's sections (?conversation=<id>, else
#                                     its newest conversation)
#   POST /admin/projects/{id}/stop    turn the project off: its runners, dev servers, container
#                                     (services/projects.close_project). Nothing on disk is touched.
#   GET  /admin/projects/{id}/deployments  {status, history, default_domain} (modules/deployments)
#   POST /admin/projects/{id}/deployments  {domains}: set the site's domains -- none given = the default one,
#                                     <hash>.<PROD_DOMAIN> -- then deploy, as its owner would; 202 {run_id, commit}.
#                                     409 NOTHING_TO_DEPLOY / DEPLOYMENT_IN_PROGRESS / DOMAIN_TAKEN, 400
#                                     INVALID_DOMAIN / NO_DOMAIN
#   GET    /admin/projects/{id}/plan  {plan}: the project's plan (payments.get_project_plan: its latest plan or
#                                     one-time row that isn't deleted, expired or not), or null
#   POST   /admin/projects/{id}/plan  {plan, starts_at, ends_at}: give it one by hand. 409 PLAN_EXISTS
#   PUT    /admin/projects/{id}/plan  the same body: change the admin-set one. 404 NO_PLAN, 409 PLAN_NOT_EDITABLE (a
#                                     bought one: Polar's, delete only)
#   DELETE /admin/projects/{id}/plan  soft-delete it, bought or not (a bought one keeps billing: cancel it in Polar).
#                                     404 NO_PLAN
#     POST/PUT: 400 INVALID_PERIOD when ends_at isn't after starts_at. Each change kicks off the launch fulfilment, so
#     hosting follows right away rather than at the next sweep.
#   GET  /admin/sandbox-mode          the calling admin's own sandbox mode {sandbox, effective, server_sandbox}
#   PUT  /admin/sandbox-mode          {sandbox}: switch their own purchases (payments + domains) to sandbox or back
#   GET  /admin/ai-usage?since=&until=   what the agents cost in [since, until) (ISO datetimes): {total_usd, users,
#                                     projects} (services/agent/usage.get_cost_report)
#   GET  /admin/ai-usage/projects/{id}?since=&until=  that project's cost per hour / day: {bucket, points}
#                                     (usage.get_project_cost_series). Both 400 INVALID_RANGE for a backwards
#                                     range or one longer than a year
# The same sections scripts/manage.py's `status` prints.

import asyncio
from datetime import datetime, timedelta

import pydash
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from src.config import settings
from src.dto.projects import Project, ProjectPlanRequest, SetDomainsRequest
from src.dto.users import SetSandboxModeRequest, User
from src.modules import deployments, payments
from src.repositories import projects as projects_repo
from src.repositories import users as users_repo
from src.services import agent as agent_service
from src.services import platform_status
from src.services import projects as projects_service
from src.services.launch import fulfilment
from src.utils.auth import require_admin
from src.utils.logging import log_http_error

router = APIRouter(dependencies=[Depends(require_admin)])


async def _get_project(project_id: int) -> Project:
    project = pydash.head(await projects_repo.find({"id": project_id}))
    if project is None:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
    return project


@router.get("/admin/server-usage")
async def get_server_usage(request: Request) -> dict:
    try:
        return await asyncio.to_thread(platform_status.get_server_usage)
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/admin/platform")
async def get_platform(request: Request) -> dict:
    try:
        sections, projects = await asyncio.gather(
            platform_status.get_platform_sections(), platform_status.list_projects()
        )
        return {"sections": sections, "projects": projects}
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/admin/projects/{project_id}")
async def get_project(project_id: int, request: Request, conversation: int | None = None) -> dict:
    project = await _get_project(project_id)
    try:
        return {
            "project": {"id": project["id"], "name": project["name"], "stage": project["stage"]},
            "sections": await platform_status.get_project_sections(project, conversation),
        }
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/admin/projects/{project_id}/stop")
async def stop_project(project_id: int, request: Request) -> dict:
    """What was stopped: {sessions: {conversation id: {runner}}, dev_servers, container}."""
    project = await _get_project(project_id)
    try:
        return await projects_service.close_project(project)
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/admin/projects/{project_id}/deployments")
async def get_project_deployments(project_id: int, request: Request) -> dict:
    project = await _get_project(project_id)
    try:
        conversation = await agent_service.get_current_conversation(project_id, project["user_id"])
        status, history = await asyncio.gather(
            deployments.get_deployment_status(project, conversation["id"]), deployments.list_deployments(project)
        )
        return {"status": status, "history": history, "default_domain": deployments.get_default_domain(project)}
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/admin/projects/{project_id}/deployments", status_code=202)
async def deploy_project(project_id: int, body: SetDomainsRequest, request: Request) -> dict:
    """The domains REPLACE the site's current ones. They're saved even when the deploy is then refused (409):
    they'd apply at the next one anyway."""
    project = await _get_project(project_id)
    domains = body.domains or pydash.compact([deployments.get_default_domain(project)])
    if not domains:
        raise HTTPException(status_code=400, detail="NO_DOMAIN")
    try:
        await deployments.set_domains(project, domains)
        conversation = await agent_service.get_current_conversation(project_id, project["user_id"])
        return await deployments.deploy(project, conversation["id"], enforce_cooldown=False)
    except deployments.InvalidDomain as error:
        raise HTTPException(status_code=400, detail="INVALID_DOMAIN") from error
    except deployments.DomainTaken as error:
        raise HTTPException(status_code=409, detail="DOMAIN_TAKEN") from error
    except deployments.NothingToDeploy as error:
        raise HTTPException(status_code=409, detail="NOTHING_TO_DEPLOY") from error
    except deployments.DeploymentInProgress as error:
        raise HTTPException(status_code=409, detail="DEPLOYMENT_IN_PROGRESS") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


_PLAN_FIELDS = ["id", "provider", "product", "plan", "status", "created_at", "ends_at", "renews_at"]


@router.get("/admin/projects/{project_id}/plan")
async def get_project_plan(project_id: int, request: Request) -> dict:
    await _get_project(project_id)
    try:
        plan = await payments.get_project_plan(project_id)
        return {"plan": pydash.pick(plan, *_PLAN_FIELDS) if plan else None}
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/admin/projects/{project_id}/plan", status_code=201)
async def create_project_plan(
    project_id: int,
    body: ProjectPlanRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_admin),
) -> dict:
    project = await _get_project(project_id)
    try:
        plan = await payments.create_admin_plan(
            project, payments.Plan(body.plan), body.starts_at, body.ends_at, user["id"]
        )
        background_tasks.add_task(fulfilment.fulfil, project_id)
        return {"plan": pydash.pick(plan, *_PLAN_FIELDS)}
    except payments.PlanExists as error:
        raise HTTPException(status_code=409, detail="PLAN_EXISTS") from error
    except payments.InvalidPlanPeriod as error:
        raise HTTPException(status_code=400, detail="INVALID_PERIOD") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.put("/admin/projects/{project_id}/plan")
async def update_project_plan(
    project_id: int, body: ProjectPlanRequest, request: Request, background_tasks: BackgroundTasks
) -> dict:
    await _get_project(project_id)
    try:
        plan = await payments.update_admin_plan(project_id, payments.Plan(body.plan), body.starts_at, body.ends_at)
        background_tasks.add_task(fulfilment.fulfil, project_id)
        return {"plan": pydash.pick(plan, *_PLAN_FIELDS)}
    except payments.NoPlan as error:
        raise HTTPException(status_code=404, detail="NO_PLAN") from error
    except payments.PlanNotEditable as error:
        raise HTTPException(status_code=409, detail="PLAN_NOT_EDITABLE") from error
    except payments.InvalidPlanPeriod as error:
        raise HTTPException(status_code=400, detail="INVALID_PERIOD") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.delete("/admin/projects/{project_id}/plan")
async def delete_project_plan(project_id: int, request: Request, background_tasks: BackgroundTasks) -> dict:
    await _get_project(project_id)
    try:
        await payments.delete_project_plan(project_id)
        background_tasks.add_task(fulfilment.fulfil, project_id)
        return {"plan": None}
    except payments.NoPlan as error:
        raise HTTPException(status_code=404, detail="NO_PLAN") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/admin/sandbox-mode")
async def get_sandbox_mode(request: Request, user: User = Depends(require_admin)) -> dict:
    """The calling admin's own sandbox mode: {sandbox (their own choice: true / false, null = the server's default),
    effective (whether their purchases are in sandbox mode now: test payments, sandbox domains), server_sandbox (the
    server's default, which everyone without a choice of their own gets)}."""
    try:
        return _to_sandbox_mode(user)
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.put("/admin/sandbox-mode")
async def set_sandbox_mode(body: SetSandboxModeRequest, request: Request, user: User = Depends(require_admin)) -> dict:
    """Set the calling admin's own mode (users.meta.sandbox_mode) -- sandbox, live, or null to follow the server's
    default again -- payments and domains together. Only their purchases from now on: ones already made stay where
    they were made. Returns GET's shape."""
    try:
        updated = await users_repo.update(user["id"], {"meta": {"sandbox_mode": body.sandbox}})
        return _to_sandbox_mode(updated or user)
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


def _to_sandbox_mode(user: User) -> dict:
    own_choice = pydash.get(user, "meta.sandbox_mode")
    return {
        "sandbox": own_choice if isinstance(own_choice, bool) else None,
        "effective": payments.is_sandbox(user),
        "server_sandbox": settings.sandbox_mode,
    }


def _check_range(since: datetime, until: datetime) -> None:
    if not since < until <= since + timedelta(days=366):
        raise HTTPException(status_code=400, detail="INVALID_RANGE")


@router.get("/admin/ai-usage")
async def get_ai_usage(request: Request, since: datetime = Query(), until: datetime = Query()) -> dict:
    _check_range(since, until)
    try:
        return await agent_service.get_cost_report(since, until)
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/admin/ai-usage/projects/{project_id}")
async def get_project_ai_usage(
    project_id: int, request: Request, since: datetime = Query(), until: datetime = Query()
) -> dict:
    _check_range(since, until)
    try:
        return await agent_service.get_project_cost_series(project_id, since, until)
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
