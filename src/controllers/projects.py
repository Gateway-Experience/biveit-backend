# Intentions: the caller's own projects.
#   GET  /projects  (auth)  my live projects, most recently edited first
#   POST /projects  (auth)  create a project owned by me
# Guests count as callers too -- their projects stay theirs after signup (promotion keeps the user id).
# No service hop: both are straight repo calls with nothing to orchestrate yet.

from fastapi import APIRouter, Depends, HTTPException, Request

from src.dto.projects import CreateProjectRequest, Project
from src.dto.users import User
from src.repositories import projects as projects_repo
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
