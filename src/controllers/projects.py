# Intentions: the caller's own projects.
#   GET  /projects  (auth)  my live projects, most recently edited first
#   GET  /projects/search?q=  (auth)  my projects where I wrote q while prompting, with the bit that matched
#   POST /projects            (auth)  create a project owned by me, from the submitted starting form
#   POST /projects/{id}/open  (auth)  get my project ready to work on: call it right after creating it,
#                                     and whenever the user opens it
#   PATCH  /projects/{id}     (auth)  rename, pin/unpin, or file my project into one of my folders
#   DELETE /projects/{id}     (auth)  delete my project (soft: it just disappears from every list)
# Guests count as callers too -- their projects stay theirs after signup (promotion keeps the user id).
# Listing and creating are straight repo calls; the rest goes through services/projects.

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.dto.projects import CreateProjectRequest, Project, UpdateProjectRequest
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


@router.get("/projects/search")
async def search_projects(
    request: Request, q: str = Query(max_length=200), user: User = Depends(get_current_user)
) -> list[dict]:
    """Bare array of {project_id, excerpt}, one per matching project. Name matches aren't included: the
    FE filters the list it already has for those."""
    try:
        return await project_service.search_prompts(user["id"], q)
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


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: int, body: UpdateProjectRequest, request: Request, user: User = Depends(get_current_user)
) -> Project:
    """Edit the caller's project -- whichever of name / is_pinned / folder_id the body has -- and return
    it updated. 404 PROJECT_NOT_FOUND for a project that isn't theirs, FOLDER_NOT_FOUND for a folder
    that isn't."""
    try:
        projects = await projects_repo.find({"id": project_id, "user_id": user["id"]})
        if not projects:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        updated = await project_service.update_project(projects[0], body.model_dump(exclude_unset=True))
        if not updated:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        return updated
    except HTTPException:
        raise
    except project_service.FolderNotFound as error:
        raise HTTPException(status_code=404, detail="FOLDER_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: int, request: Request, user: User = Depends(get_current_user)) -> None:
    """Delete the caller's project: what runs for it is turned off and it's gone from every list (soft
    delete, files kept). 404 for a project that isn't theirs (or is already deleted)."""
    try:
        projects = await projects_repo.find({"id": project_id, "user_id": user["id"]})
        if not projects:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        await project_service.delete_project(projects[0])
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
