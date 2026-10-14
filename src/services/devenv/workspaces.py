# Intentions: where a project's files live on disk, and getting a workspace ready to work in. The one
# place that knows the layout:
#
#   <STORAGE_DIR>/<user hash>/<project hash>/                    the project
#   <STORAGE_DIR>/<user hash>/<project hash>/<conversation_id>/  a workspace, where its agent session works.
#       CLAUDE.md, .mcp.json, .claude/   the agent's files, from TEMPLATE_DIR (from the start)
#       backend/, frontend/              the app, from BOILERPLATE_DIR (once implementing starts), each
#                                        with its own CLAUDE.md
#       uploads/, artifacts/             the chat's files (services/agent/storage)
#
# The user is the project's OWNER, so every conversation of a project shares its tree. Hashes rather than
# ids, so a path gives nothing away (rows from before hashes existed have hash = id, see the migrations).
# The workspace is per conversation, not per project; a project has one conversation for now.

import asyncio
import os
import shutil
import uuid

from src.config import settings
from src.constants import DEVENV_APP_DIRS, DEVENV_COPY_SKIPPED_NAMES
from src.dto.projects import Project


def get_project_dir(project: Project) -> str:
    """[Pure-ish] <storage_dir>/<owner's hash>/<project hash>, absolute."""
    return os.path.join(os.path.realpath(settings.storage_dir), project["user_hash"], project["hash"])


def get_conversation_dir(project: Project, conversation_id: int) -> str:
    """[Pure-ish] The conversation's folder inside its project's -- its workspace."""
    return os.path.join(get_project_dir(project), str(conversation_id))


async def ensure_workspace(project: Project, conversation_id: int) -> None:
    """Create the conversation's workspace if needed and copy in any template files it's missing.
    Idempotent, and never touches files the workspace already has."""
    await asyncio.to_thread(
        _copy_missing_files,
        os.path.realpath(settings.template_dir),
        get_conversation_dir(project, conversation_id),
    )


def _copy_missing_files(source_dir: str, destination_dir: str) -> None:
    """Copy every file under source_dir into destination_dir unless it already exists there, creating
    directories as needed. Existing files are the project's own (likely edited) versions, so they're
    never overwritten. A template file added later still reaches older workspaces on their next visit."""
    shutil.copytree(
        source_dir,
        destination_dir,
        dirs_exist_ok=True,
        copy_function=lambda src, dst: None if os.path.exists(dst) else shutil.copy2(src, dst),
    )


def has_app(project: Project, conversation_id: int) -> bool:
    """[Pure-ish] Whether the boilerplate's apps are in the workspace, i.e. there's an app to run
    (copy_boilerplate only ever puts a complete app folder in place)."""
    workspace_dir = get_conversation_dir(project, conversation_id)
    return all(os.path.isdir(os.path.join(workspace_dir, app_dir)) for app_dir in DEVENV_APP_DIRS)


async def copy_boilerplate(project: Project, conversation_id: int) -> None:
    """Copy each of the boilerplate's apps (DEVENV_APP_DIRS) the workspace doesn't have yet. One that's
    already there is the project's own and is left alone. Skips DEVENV_COPY_SKIPPED_NAMES."""
    boilerplate_dir = os.path.realpath(os.path.expanduser(settings.boilerplate_dir))
    workspace_dir = get_conversation_dir(project, conversation_id)
    missing_app_dirs = [
        app_dir for app_dir in DEVENV_APP_DIRS if not os.path.isdir(os.path.join(workspace_dir, app_dir))
    ]
    # Temp folders left by a copy that died halfway (the backend was killed mid-copy). Safe to remove:
    # callers hold the devenv lock, so no copy is running now.
    stale_temp_dirs = (
        [entry.path for entry in os.scandir(workspace_dir) if ".copying-" in entry.name]
        if os.path.isdir(workspace_dir)
        else []
    )
    for stale_temp_dir in stale_temp_dirs:
        await asyncio.to_thread(shutil.rmtree, stale_temp_dir, ignore_errors=True)
    for app_dir in missing_app_dirs:
        await asyncio.to_thread(_copy_dir_atomically, os.path.join(boilerplate_dir, app_dir), workspace_dir, app_dir)


def _copy_dir_atomically(source_dir: str, parent_dir: str, name: str) -> None:
    """Copy source_dir to parent_dir/name via a temporary sibling that's renamed into place at the end.
    A copy that dies halfway leaves only the temp folder (removed if anything fails), never a
    half-filled `name` that would look finished to has_app."""
    temp_dir = os.path.join(parent_dir, f".{name}.copying-{uuid.uuid4().hex[:8]}")
    try:
        shutil.copytree(source_dir, temp_dir, symlinks=True, ignore=shutil.ignore_patterns(*DEVENV_COPY_SKIPPED_NAMES))
        os.rename(temp_dir, os.path.join(parent_dir, name))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
