# Intentions: the rules for a user's folder tree that the DB doesn't enforce -- a folder's parent must be
# one of the same user's live folders, and a move must never put a folder inside its own subtree (that'd
# cut the branch off from the root, looping forever). Names unique per parent IS the DB's job
# (repositories/project_folders raises DuplicateFolderName). Listing and deleting are plain repo calls.
# References: migrations/0008_project_folders.sql (the tree's rules), controllers/project_folders.

from typing import Any

import pydash

from src.dto.projects import ProjectFolder
from src.repositories import project_folders as project_folders_repo


class ParentFolderNotFound(Exception):
    """The folder to create in / move into isn't one of the user's live folders. 404."""


class FolderIntoOwnSubtree(Exception):
    """A folder was being moved into itself or one of its own descendants. 409."""


def get_ancestor_ids(folders_by_id: dict[int, ProjectFolder], folder_id: int, path: tuple = ()) -> tuple:
    """[Pure] The ids above `folder_id`, nearest first, up to the root. `path` is the ancestors found so
    far (the recursion's accumulator; callers leave it out). Stops at a parent it's already seen too, so a
    looped tree (which shouldn't exist) can't recurse forever."""
    parent_id = pydash.get(folders_by_id, [folder_id, "parent_id"])
    if parent_id is None or parent_id in path:
        return path
    return get_ancestor_ids(folders_by_id, parent_id, (*path, parent_id))


async def create_folder(user_id: int, name: str, parent_id: int | None) -> ProjectFolder:
    """Make a folder for the user, in `parent_id` (None = the root). Raises ParentFolderNotFound for a
    parent that isn't theirs, DuplicateFolderName if the parent already has that name."""
    if parent_id is not None and not await project_folders_repo.find({"id": parent_id, "user_id": user_id}):
        raise ParentFolderNotFound("no such parent folder", {"parent_id": parent_id, "user_id": user_id})
    return await project_folders_repo.insert({"user_id": user_id, "name": name, "parent_id": parent_id})


async def update_folder(folder: ProjectFolder, changes: dict[str, Any]) -> ProjectFolder | None:
    """Rename and/or move the user's folder; returns it updated (None if it vanished meanwhile). `changes`
    holds only what the user sent, out of: name, parent_id (None = to the root).

    A move loads all the user's folders to walk up from the new parent -- a user has a handful, so that's
    cheaper than a recursive query. Raises ParentFolderNotFound / FolderIntoOwnSubtree for a bad move,
    DuplicateFolderName if the name's taken where it lands."""
    parent_id = changes.get("parent_id")
    if parent_id is not None:
        folders_by_id = pydash.key_by(await project_folders_repo.find({"user_id": folder["user_id"]}), "id")
        if parent_id not in folders_by_id:
            raise ParentFolderNotFound("no such parent folder", {"parent_id": parent_id, "folder_id": folder["id"]})
        if folder["id"] in (parent_id, *get_ancestor_ids(folders_by_id, parent_id)):
            raise FolderIntoOwnSubtree(
                "can't move a folder into itself", {"parent_id": parent_id, "folder_id": folder["id"]}
            )
    updates = {
        **({"name": changes["name"]} if changes.get("name") else {}),
        **({"parent_id": parent_id} if "parent_id" in changes else {}),
    }
    return await project_folders_repo.update(folder["id"], updates) if updates else folder
