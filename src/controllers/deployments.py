# Intentions: putting the caller's project's site in production (modules/deployments).
#   POST /projects/{id}/deployments         (auth)  deploy what's in the workspace now; 202 {run_id, commit},
#                                                   the rest runs in the background (watch the status)
#   GET  /projects/{id}/deployments         (auth)  the deployment history, newest first
#   GET  /projects/{id}/deployments/status  (auth)  what's live, whether a deployment runs, whether there's
#                                                   anything new to deploy
# A site's domains are set through the launch flow (controllers/launch), never here.
# Errors: 404 PROJECT_NOT_FOUND (not the caller's), 402 PLAN_REQUIRED (no hosting plan running), 409
# NOTHING_TO_DEPLOY (no app yet), 409 DEPLOYMENT_IN_PROGRESS, 429 DEPLOYMENT_COOLDOWN (+ until).

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from src.dto.projects import Project
from src.dto.users import User
from src.modules import deployments, payments
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


@router.post("/projects/{project_id}/deployments", status_code=202, response_model=None)
async def deploy_project(
    project_id: int, request: Request, user: User = Depends(get_current_user)
) -> dict | JSONResponse:
    try:
        project = await _get_owned_project(project_id, user)
        if not (await payments.get_project_access(project_id))["can_host"]:
            raise HTTPException(status_code=402, detail="PLAN_REQUIRED")
        conversation = await agent_service.get_current_conversation(project_id, user["id"])
        return await deployments.deploy(project, conversation["id"])
    except HTTPException:
        raise
    except deployments.DeploymentCooldown as cooldown:
        return JSONResponse(
            status_code=429, content={"detail": "DEPLOYMENT_COOLDOWN", "until": cooldown.until.isoformat()}
        )
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
