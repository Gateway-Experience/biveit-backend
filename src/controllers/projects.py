# Intentions: the caller's own projects.
#   GET  /projects  (auth)  my live projects, most recently edited first
#   POST /projects            (auth)  create a project owned by me, from the submitted starting form
#   POST /projects/{id}/open  (auth)  get my project ready to work on: call it right after creating it,
#                                     and whenever the user opens it
# Guests count as callers too -- their projects stay theirs after signup (promotion keeps the user id).
# Listing and creating are straight repo calls; opening goes through services/projects.

from fastapi import APIRouter, Depends, HTTPException, Request

from src.dto.projects import CreateProjectRequest, Project
from src.dto.users import User
from src.repositories import projects as projects_repo
from src.services import projects as project_service
from src.utils.auth import get_current_user
from src.utils.logging import log_http_error

router = APIRouter()


@router.get("/projects")
async def list_projects(request: Request, user: User = Depends(get_current_user)) -> list[Project]:
    """Returned BARE (a plain array). An empty list is the FE's cue for its "create a project" CTA."""
    try:
        return await projects_repo.find({"user_id": user["id"]})
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/projects", status_code=201)
async def create_project(
    body: CreateProjectRequest, request: Request, user: User = Depends(get_current_user)
) -> Project:
    try:
        return await projects_repo.insert({**body.model_dump(), "user_id": user["id"]})
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/projects/{project_id}/open")
async def open_project(project_id: int, request: Request, user: User = Depends(get_current_user)) -> dict:
    """Prepare the caller's project -- its workspace and agent -- and answer once it's ready. Safe to call
    on every visit. The agent's port stays internal: the browser never talks to the session directly."""
    try:
        projects = await projects_repo.find({"id": project_id, "user_id": user["id"]})
        if not projects:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        await project_service.open_project(projects[0])
        return {"status": "ready"}
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=503, detail="WORKSPACE_UNAVAILABLE") from error
