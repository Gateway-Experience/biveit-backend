# Intentions: where a project's files live on disk, and getting a workspace ready to work in. The one
# place that knows the layout:
#
#   <STORAGE_DIR>/<user_id>/<project_id>/                    the project
#   <STORAGE_DIR>/<user_id>/<project_id>/<conversation_id>/  a workspace: the project's code, seeded
#                                                            from the template, where its agent session
#                                                            works (services/agent puts its uploads/ and
#                                                            artifacts/ in here too)
#
# user_id is the project's OWNER (projects.user_id), so every conversation of a project shares its tree.
# The workspace is per conversation, not per project; a project has one conversation for now.

import asyncio
import os
import shutil

from src.config import settings
from src.dto.projects import Project


def get_project_dir(project: Project) -> str:
    """[Pure-ish] <storage_dir>/<owner user_id>/<project_id>, absolute."""
    return os.path.join(os.path.realpath(settings.storage_dir), str(project["user_id"]), str(project["id"]))


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
