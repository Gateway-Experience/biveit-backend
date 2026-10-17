# Intentions: the caller's own project folders -- what they group their projects into in the sidebar.
#   GET    /project-folders       (auth)  my folders, in the order I made them
#   POST   /project-folders       (auth)  make a folder
#   PATCH  /project-folders/{id}  (auth)  rename my folder
#   DELETE /project-folders/{id}  (auth)  delete my folder; its projects stay, just no longer in a folder
# Filing a project into a folder is PATCH /projects/{id} (controllers/projects). All plain CRUD, so these
# are straight repo calls.

from fastapi import APIRouter, Depends, HTTPException, Request

from src.dto.projects import ProjectFolder, SaveProjectFolderRequest
from src.dto.users import User
from src.repositories import project_folders as project_folders_repo
from src.utils.auth import get_current_user
from src.utils.common import utc_now
from src.utils.logging import log_http_error

router = APIRouter()


@router.get("/project-folders")
async def list_project_folders(request: Request, user: User = Depends(get_current_user)) -> list[ProjectFolder]:
    try:
        return await project_folders_repo.find({"user_id": user["id"]})
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.post("/project-folders", status_code=201)
async def create_project_folder(
    body: SaveProjectFolderRequest, request: Request, user: User = Depends(get_current_user)
) -> ProjectFolder:
    try:
        return await project_folders_repo.insert({"name": body.name, "user_id": user["id"]})
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


async def _get_own_folder(folder_id: int, user: User) -> ProjectFolder:
    """The caller's live folder, or a 404 for one that isn't theirs (or is gone)."""
    folders = await project_folders_repo.find({"id": folder_id, "user_id": user["id"]})
    if not folders:
        raise HTTPException(status_code=404, detail="FOLDER_NOT_FOUND")
    return folders[0]


@router.patch("/project-folders/{folder_id}")
async def update_project_folder(
    folder_id: int, body: SaveProjectFolderRequest, request: Request, user: User = Depends(get_current_user)
) -> ProjectFolder:
    try:
        await _get_own_folder(folder_id, user)
        updated = await project_folders_repo.update(folder_id, {"name": body.name})
        if not updated:
            raise HTTPException(status_code=404, detail="FOLDER_NOT_FOUND")
        return updated
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.delete("/project-folders/{folder_id}", status_code=204)
async def delete_project_folder(folder_id: int, request: Request, user: User = Depends(get_current_user)) -> None:
    """Soft-delete the folder. Its projects aren't touched: pointing at a deleted folder means "in no
    folder" (see migrations/0008_project_folders.sql), so they show up as unfiled straight away."""
    try:
        await _get_own_folder(folder_id, user)
        await project_folders_repo.update(folder_id, {"deleted_at": utc_now()})
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
