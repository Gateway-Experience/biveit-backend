# Intentions: the workspace's git repository -- what a deployment deploys is a commit of it, and what a code
# download zips up is a snapshot of it. Every git command runs INSIDE the project's dev container (the workspace
# is the agent's; the host never walks it), in the workspace's folder. The repo is created on first use; its
# .gitignore comes with the workspace template.
#
# The agent can use the repo too (and rewrite it -- it's theirs). That's fine: a commit only has to exist
# until its image is built, and rollbacks run from images.

import re

from src.dto.projects import Project
from src.services import devenv

# Who the platform's commits are by. Passed as env vars, so the repo's own config is left alone.
_COMMITTER_ENV = {
    "GIT_AUTHOR_NAME": "biveit",
    "GIT_AUTHOR_EMAIL": "deploy@biveit.local",
    "GIT_COMMITTER_NAME": "biveit",
    "GIT_COMMITTER_EMAIL": "deploy@biveit.local",
}


# The apps as they are right now (committed or not), zipped under a top-level folder ($1), from a throwaway index
# -- so the repo's own index, HEAD and history are left exactly as they were. Leaves out what .gitignore does
# (node_modules, .venv, dist, logs, .env.local...) and on top of that every env file but .env.example: what
# isn't ignored (the frontend's .env) is still the project's config, not code to hand out. The blobs `git add`
# writes stay behind as loose objects, which is harmless.
_ARCHIVE_SCRIPT = r"""
set -e
test -d .git || git init -q -b main
export GIT_INDEX_FILE="$(git rev-parse --git-dir)/biveit-download-$$.index"
trap 'rm -f "$GIT_INDEX_FILE"' EXIT
git add -A
git ls-files -z | grep -zE '(^|/)\.env(\.[^/]*)?$' | grep -zvE '(^|/)\.env\.example$' | xargs -0r git rm --cached -q --
git archive --format=zip --prefix="$1/" "$(git write-tree)" backend frontend
"""


class NothingToDownload(Exception):
    """The workspace has no app (yet) -- nothing to zip."""


async def archive_workspace(project: Project, conversation_id: int) -> tuple[str, bytes]:
    """The project's code as it is right now -- its backend/ and frontend/, minus dependencies, build output and
    env files (see _ARCHIVE_SCRIPT) -- as (folder name, zip archive). Wakes the dev container if it's stopped. No
    versions: every call is a fresh snapshot. Raises NothingToDownload."""
    if not devenv.has_app(project, conversation_id):
        raise NothingToDownload(f"project {project['id']} has no app yet")
    # Older workspaces get the template's .gitignore here, before their first snapshot.
    await devenv.ensure_workspace(project, conversation_id)
    await devenv.ensure_container(project)
    folder_name = to_code_folder_name(project)
    archive = await devenv.run_in_container_binary(
        project["id"], ["sh", "-c", _ARCHIVE_SCRIPT, "sh", folder_name], _get_workspace_path(project, conversation_id)
    )
    return folder_name, archive


def to_code_folder_name(project: Project) -> str:
    """[Pure] What the download's folder (and file) is called: the project's name, lower-case ASCII letters and
    digits joined by hyphens -- it goes in a header and a shell argument, so nothing else gets through."""
    slug = re.sub(r"[^a-z0-9]+", "-", project["name"].lower()).strip("-")[:60].strip("-")
    return slug or f"project-{project['id']}"


async def commit_workspace(project: Project, conversation_id: int) -> str:
    """Commit everything in the workspace (creating the repo the first time) and return the commit -- HEAD as
    it is when there's nothing new. The project's dev container must be running."""
    script = (
        "test -d .git || git init -q -b main; "
        "git add -A; "
        "git diff --cached --quiet || git commit -q -m 'Deploy'; "
        "git rev-parse HEAD"
    )
    output = await devenv.run_in_container(
        project["id"], ["sh", "-c", script], _get_workspace_path(project, conversation_id), env=_COMMITTER_ENV
    )
    return output.strip().splitlines()[-1]


async def has_changes_since(project: Project, conversation_id: int, commit: str | None) -> bool | None:
    """Whether the workspace differs from `commit` (None = nothing deployed yet: any workspace does) -- a
    different HEAD, or anything uncommitted. None when the dev container isn't running: we can't tell, and
    asking isn't worth waking it up."""
    if not await devenv.is_container_running(project["id"]):
        return None
    # First line: HEAD (empty with no repo or no commit yet), then one line per uncommitted change.
    script = 'test -d .git || exit 0; echo "$(git rev-parse -q --verify HEAD)"; git status --porcelain'
    output = await devenv.run_in_container(
        project["id"], ["sh", "-c", script], _get_workspace_path(project, conversation_id)
    )
    head, *changes = output.splitlines() or [""]
    return commit is None or head != commit or bool(changes)


async def archive_commit(project: Project, conversation_id: int, commit: str) -> bytes:
    """The apps (backend/, frontend/) at `commit`, as a tar archive. The project's dev container must be running."""
    return await devenv.run_in_container_binary(
        project["id"],
        ["git", "archive", "--format=tar", commit, "backend", "frontend"],
        _get_workspace_path(project, conversation_id),
    )


def _get_workspace_path(project: Project, conversation_id: int) -> str:
    return devenv.to_container_path(project, devenv.get_conversation_dir(project, conversation_id))
