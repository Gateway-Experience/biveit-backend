# Intentions: putting the caller's project's site in production (modules/deployments).
#   POST /projects/{id}/deployments         (auth)  deploy what's in the workspace now; 202 {run_id, commit},
#                                                   the rest runs in the background (watch the status)
#   GET  /projects/{id}/deployments         (auth)  the deployment history, newest first
#   GET  /projects/{id}/deployments/status  (auth)  what's live, whether a deployment runs, whether there's
#                                                   anything new to deploy
#   PUT  /projects/{id}/domains             (auth)  {domains}: where the site is served, from the next deployment
# Errors: 404 PROJECT_NOT_FOUND (not the caller's), 409 NOTHING_TO_DEPLOY (no app yet), 409
# DEPLOYMENT_IN_PROGRESS, 400 INVALID_DOMAIN.
# TODO: deploying isn't gated on anything yet (payment, plan) -- it must be before the FE offers it.

from fastapi import APIRouter, Depends, HTTPException, Request

from src.dto.projects import Project, SetDomainsRequest
from src.dto.users import User
from src.modules import deployments
from src.repositories import projects as projects_repo
from src.services import agent as agent_service
from src.utils.auth import get_current_user
from src.utils.logging import log_http_error

router = APIRouter()


async def _get_owned_project(project_id: int, user: User) -> Project:
    projects = await projects_repo.find({"id": project_id, "user_id": user["id"]})
    if not projects:
        raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
    return projects[0]


@router.post("/projects/{project_id}/deployments", status_code=202)
async def deploy_project(project_id: int, request: Request, user: User = Depends(get_current_user)) -> dict:
    try:
        project = await _get_owned_project(project_id, user)
        conversation = await agent_service.get_current_conversation(project_id, user["id"])
        return await deployments.deploy(project, conversation["id"])
    except HTTPException:
        raise
    except deployments.NothingToDeploy as error:
        raise HTTPException(status_code=409, detail="NOTHING_TO_DEPLOY") from error
    except deployments.DeploymentInProgress as error:
        raise HTTPException(status_code=409, detail="DEPLOYMENT_IN_PROGRESS") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/projects/{project_id}/deployments")
async def list_deployments(project_id: int, request: Request, user: User = Depends(get_current_user)) -> list[dict]:
    """Returned BARE (a plain array)."""
    try:
        return await deployments.list_deployments(await _get_owned_project(project_id, user))
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/projects/{project_id}/deployments/status")
async def get_deployment_status(project_id: int, request: Request, user: User = Depends(get_current_user)) -> dict:
    try:
        project = await _get_owned_project(project_id, user)
        conversation = await agent_service.get_current_conversation(project_id, user["id"])
        return await deployments.get_deployment_status(project, conversation["id"])
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.put("/projects/{project_id}/domains")
async def set_domains(
    project_id: int, body: SetDomainsRequest, request: Request, user: User = Depends(get_current_user)
) -> dict:
    try:
        project = await _get_owned_project(project_id, user)
        return {"domains": await deployments.set_domains(project, body.domains)}
    except HTTPException:
        raise
    except deployments.InvalidDomain as error:
        raise HTTPException(status_code=400, detail="INVALID_DOMAIN") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
