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
#                                     409 NOTHING_TO_DEPLOY / DEPLOYMENT_IN_PROGRESS, 400 INVALID_DOMAIN / NO_DOMAIN
# The same sections scripts/manage.py's `status` prints.

import asyncio

import pydash
from fastapi import APIRouter, Depends, HTTPException, Request

from src.dto.projects import Project, SetDomainsRequest
from src.modules import deployments
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.services import platform_status
from src.services import projects as projects_service
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
        # Read again: the deployment works off the project as it's passed, new domains included.
        return await deployments.deploy(await _get_project(project_id), conversation["id"])
    except deployments.InvalidDomain as error:
        raise HTTPException(status_code=400, detail="INVALID_DOMAIN") from error
    except deployments.NothingToDeploy as error:
        raise HTTPException(status_code=409, detail="NOTHING_TO_DEPLOY") from error
    except deployments.DeploymentInProgress as error:
        raise HTTPException(status_code=409, detail="DEPLOYMENT_IN_PROGRESS") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
