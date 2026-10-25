# Intentions: the caller's own projects.
#   GET  /projects  (auth)  my live projects (not in Trash), most recently edited first
#   GET  /projects/trash      (auth)  my projects in Trash, most recently trashed first
#   GET  /projects/search?q=  (auth)  my projects where I wrote q while prompting, with the bit that matched
#   GET  /projects/{id}/site-preview?v=  (auth)  the picture of my project's dev site (a JPEG), for its card
#   POST /projects            (auth)  create a project owned by me, from the submitted starting form
#   POST /projects/{id}/open  (auth)  get my project ready to work on: call it right after creating it,
#                                     and whenever the user opens it
#   PATCH  /projects/{id}     (auth)  rename, pin/unpin, or file my project into one of my folders
#   POST /projects/{id}/trash    (auth)  move my project to Trash (what runs for it is turned off)
#   POST /projects/{id}/restore  (auth)  take my project out of Trash, as it was
#   DELETE /projects/{id}     (auth)  delete my project in Trash for good (soft: it just disappears from
#                                     every list), so a live project can't vanish in one click
# Guests count as callers too -- their projects stay theirs after signup (promotion keeps the user id).
# Listing and creating are straight repo calls; the rest goes through services/projects.

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from src.dto.projects import CreateProjectRequest, Project, UpdateProjectRequest
from src.dto.users import User
from src.repositories import projects as projects_repo
from src.services import projects as project_service
from src.services.devenv import previews as site_previews
from src.utils.auth import get_current_user
from src.utils.logging import log_http_error

router = APIRouter()


def _with_site_preview(project: Project) -> Project:
    """[Pure-ish] The project plus when its site's picture was taken (None = none yet), for the FE's cards."""
    return {**project, "site_preview_at": site_previews.get_taken_at(project)}


@router.get("/projects")
async def list_projects(request: Request, user: User = Depends(get_current_user)) -> list[Project]:
    """Returned BARE (a plain array). An empty list is the FE's cue for its "create a project" CTA."""
    try:
        return [_with_site_preview(project) for project in await projects_repo.find({"user_id": user["id"]})]
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/projects/trash")
async def list_trashed_projects(request: Request, user: User = Depends(get_current_user)) -> list[Project]:
    """Returned BARE (a plain array), same as GET /projects."""
    try:
        return await projects_repo.find_trashed({"user_id": user["id"]})
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.get("/projects/history")
async def get_project_history(request: Request, user: User = Depends(get_current_user)) -> dict:
    """{has_created_project}: whether the caller has ever made a project, even one since trashed or deleted
    for good. The FE's first-time intro is only for accounts that haven't."""
    try:
        return {"has_created_project": await projects_repo.has_ever_created(user["id"])}
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


@router.get("/projects/{project_id}/site-preview")
async def get_site_preview(project_id: int, request: Request, user: User = Depends(get_current_user)) -> FileResponse:
    """The picture of the caller's project's dev site. A new picture means a new site_preview_at, which the FE
    puts in the URL (?v=), so any one URL's picture never changes: cached for good. 404 PROJECT_NOT_FOUND /
    SITE_PREVIEW_NOT_FOUND."""
    try:
        projects = await projects_repo.find({"id": project_id, "user_id": user["id"]})
        if not projects:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        if site_previews.get_taken_at(projects[0]) is None:
            raise HTTPException(status_code=404, detail="SITE_PREVIEW_NOT_FOUND")
        return FileResponse(
            site_previews.get_preview_path(projects[0]),
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )
    except HTTPException:
        raise
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
        return _with_site_preview(updated)
    except HTTPException:
        raise
    except project_service.FolderNotFound as error:
        raise HTTPException(status_code=404, detail="FOLDER_NOT_FOUND") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/projects/{project_id}/trash")
async def trash_project(project_id: int, request: Request, user: User = Depends(get_current_user)) -> Project:
    """Move the caller's project to Trash and return it. 404 PROJECT_NOT_FOUND for a project that isn't
    theirs, or is in Trash already."""
    try:
        projects = await projects_repo.find({"id": project_id, "user_id": user["id"]})
        if not projects:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        trashed = await project_service.trash_project(projects[0])
        if not trashed:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        return trashed
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/projects/{project_id}/restore")
async def restore_project(project_id: int, request: Request, user: User = Depends(get_current_user)) -> Project:
    """Take the caller's project out of Trash and return it -- back in the list where it was (restoring
    isn't an edit, so updated_at stays). 404 PROJECT_NOT_FOUND if it isn't in their Trash."""
    try:
        projects = await projects_repo.find_trashed({"id": project_id, "user_id": user["id"]})
        if not projects:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        restored = await projects_repo.update(project_id, {"trashed_at": None})
        if not restored:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        return _with_site_preview(restored)
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: int, request: Request, user: User = Depends(get_current_user)) -> None:
    """Delete the caller's project for good: what runs for it is turned off and it's gone from every list
    (soft delete, files kept). Only from Trash: 404 for a project that isn't in their Trash."""
    try:
        projects = await projects_repo.find_trashed({"id": project_id, "user_id": user["id"]})
        if not projects:
            raise HTTPException(status_code=404, detail="PROJECT_NOT_FOUND")
        await project_service.delete_project(projects[0])
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
