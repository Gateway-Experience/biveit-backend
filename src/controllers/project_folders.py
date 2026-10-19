# Intentions: the caller's own project folders -- a tree they group their projects into in the sidebar.
#   GET    /project-folders       (auth)  my folders (all levels, flat -- parent_id says where each sits),
#                                         in the order I made them
#   POST   /project-folders       (auth)  make a folder, at the root or inside one of mine (parent_id)
#   PATCH  /project-folders/{id}  (auth)  rename and/or move my folder (parent_id; null = to the root)
#   DELETE /project-folders/{id}  (auth)  delete my folder AND its subfolders; their projects stay, moved
#                                         back to the root
# Errors: 404 FOLDER_NOT_FOUND / PARENT_FOLDER_NOT_FOUND for a folder that isn't mine, 409
# FOLDER_NAME_TAKEN when the parent already has that name, 409 FOLDER_INTO_OWN_SUBTREE for a move into
# itself or below itself.
# Filing a project into a folder is PATCH /projects/{id} (controllers/projects). Listing and deleting are
# straight repo calls; creating and updating go through services/project_folders.

from fastapi import APIRouter, Depends, HTTPException, Request

from src.dto.projects import CreateProjectFolderRequest, ProjectFolder, UpdateProjectFolderRequest
from src.dto.users import User
from src.repositories import project_folders as project_folders_repo
from src.services import project_folders as project_folder_service
from src.utils.auth import get_current_user
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
    body: CreateProjectFolderRequest, request: Request, user: User = Depends(get_current_user)
) -> ProjectFolder:
    try:
        return await project_folder_service.create_folder(user["id"], body.name, body.parent_id)
    except project_folder_service.ParentFolderNotFound as error:
        raise HTTPException(status_code=404, detail="PARENT_FOLDER_NOT_FOUND") from error
    except project_folders_repo.DuplicateFolderName as error:
        raise HTTPException(status_code=409, detail="FOLDER_NAME_TAKEN") from error
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
    folder_id: int, body: UpdateProjectFolderRequest, request: Request, user: User = Depends(get_current_user)
) -> ProjectFolder:
    try:
        folder = await _get_own_folder(folder_id, user)
        updated = await project_folder_service.update_folder(folder, body.model_dump(exclude_unset=True))
        if not updated:
            raise HTTPException(status_code=404, detail="FOLDER_NOT_FOUND")
        return updated
    except HTTPException:
        raise
    except project_folder_service.ParentFolderNotFound as error:
        raise HTTPException(status_code=404, detail="PARENT_FOLDER_NOT_FOUND") from error
    except project_folder_service.FolderIntoOwnSubtree as error:
        raise HTTPException(status_code=409, detail="FOLDER_INTO_OWN_SUBTREE") from error
    except project_folders_repo.DuplicateFolderName as error:
        raise HTTPException(status_code=409, detail="FOLDER_NAME_TAKEN") from error
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error


@router.delete("/project-folders/{folder_id}", status_code=204)
async def delete_project_folder(folder_id: int, request: Request, user: User = Depends(get_current_user)) -> None:
    """Soft-delete the folder and its whole subtree; the projects in them go back to the root (their
    folder_id is cleared). See migrations/0008_project_folders.sql."""
    try:
        await _get_own_folder(folder_id, user)
        await project_folders_repo.delete(folder_id)
    except HTTPException:
        raise
    except Exception as error:
        log_http_error(error, request)
        raise HTTPException(status_code=500, detail="INTERNAL_SERVER_ERROR") from error
