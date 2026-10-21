# Intentions: where a project's files live on disk, and getting a workspace ready to work in. The one
# place that knows the layout:
#
#   <STORAGE_DIR>/<user hash>/<project hash>/                    the project
#   <STORAGE_DIR>/<user hash>/<project hash>/<conversation_id>/  a workspace, where its agent session works.
#       CLAUDE.md, .claude/              the agent's files, from TEMPLATE_DIR (from the start)
#       backend/, frontend/              the app, from BOILERPLATE_DIR (once implementing starts), each
#                                        with its own CLAUDE.md
#       uploads/, artifacts/             the chat's files (services/agent/storage)
#
# The user is the project's OWNER, so every conversation of a project shares its tree. Hashes rather than
# ids, so a path gives nothing away (rows from before hashes existed have hash = id, see the migrations).
# The workspace is per conversation, not per project; a project has one conversation for now.

import asyncio
import os
import stat
import uuid

from src.config import settings
from src.constants import DEVENV_APP_DIRS, DEVENV_COPY_SKIPPED_NAMES
from src.dto.projects import Project
from src.utils import paths


def get_project_dir(project: Project) -> str:
    """[Pure-ish] <storage_dir>/<owner's hash>/<project hash>, absolute."""
    return os.path.join(os.path.realpath(settings.storage_dir), project["user_hash"], project["hash"])


def get_conversation_dir(project: Project, conversation_id: int) -> str:
    """[Pure-ish] The conversation's folder inside its project's -- its workspace."""
    return os.path.join(get_project_dir(project), str(conversation_id))


async def ensure_workspace(project: Project, conversation_id: int) -> None:
    """Create the conversation's workspace if needed and copy in any template files it's missing.
    Idempotent, and never touches what the workspace already has (the project's own, likely edited,
    versions) -- while a template file added later still reaches older workspaces on their next visit."""
    project_dir = get_project_dir(project)
    # The project folder itself is ours to create: it's below our storage root, and nothing of the
    # agent's is above it. Everything inside goes through utils/paths.
    await asyncio.to_thread(os.makedirs, project_dir, exist_ok=True)
    await asyncio.to_thread(
        paths.copy_tree_within,
        os.path.realpath(settings.template_dir),
        project_dir,
        str(conversation_id),
        skip_existing=True,
    )


def has_app(project: Project, conversation_id: int) -> bool:
    """[Pure-ish] Whether the boilerplate's apps are in the workspace, i.e. there's an app to run
    (copy_boilerplate only ever puts a complete app folder in place). A symlink isn't an app folder."""
    return all(_is_dir_in_project(project, f"{conversation_id}/{app_dir}") for app_dir in DEVENV_APP_DIRS)


def _is_dir_in_project(project: Project, project_relative_path: str) -> bool:
    try:
        entry = paths.lstat_within(get_project_dir(project), project_relative_path)
    except paths.UnsafePath:
        return False
    return entry is not None and stat.S_ISDIR(entry.st_mode)


async def copy_boilerplate(project: Project, conversation_id: int) -> None:
    """Copy each of the boilerplate's apps (DEVENV_APP_DIRS) the workspace doesn't have yet. One that's
    already there is the project's own and is left alone. Skips DEVENV_COPY_SKIPPED_NAMES."""
    boilerplate_dir = os.path.realpath(os.path.expanduser(settings.boilerplate_dir))
    project_dir = get_project_dir(project)
    workspace = str(conversation_id)
    missing_app_dirs = [
        app_dir for app_dir in DEVENV_APP_DIRS if not _is_dir_in_project(project, f"{workspace}/{app_dir}")
    ]
    # Temp folders left by a copy that died halfway (the backend was killed mid-copy). Safe to remove:
    # callers hold the devenv lock, so no copy is running now.
    entries = await asyncio.to_thread(paths.list_within, project_dir, workspace)
    stale_temp_dirs = [name for name, _ in entries if ".copying-" in name]
    for stale_temp_dir in stale_temp_dirs:
        await asyncio.to_thread(paths.remove_tree_within, project_dir, f"{workspace}/{stale_temp_dir}")
    for app_dir in missing_app_dirs:
        await asyncio.to_thread(
            _copy_dir_atomically, os.path.join(boilerplate_dir, app_dir), project_dir, workspace, app_dir
        )


def _copy_dir_atomically(source_dir: str, project_dir: str, workspace: str, name: str) -> None:
    """Copy source_dir to <workspace>/<name> (workspace relative to project_dir) via a temporary sibling
    that's renamed into place at the end. A copy that dies halfway leaves only the temp folder (removed
    if anything fails), never a half-filled `name` that would look finished to has_app."""
    temp_name = f".{name}.copying-{uuid.uuid4().hex[:8]}"
    try:
        paths.copy_tree_within(
            source_dir,
            project_dir,
            f"{workspace}/{temp_name}",
            skip_existing=False,
            ignored_names=tuple(DEVENV_COPY_SKIPPED_NAMES),
        )
        paths.rename_within(project_dir, workspace, temp_name, name)
    finally:
        paths.remove_tree_within(project_dir, f"{workspace}/{temp_name}")
