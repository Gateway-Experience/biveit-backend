# Intentions: the workspace's git repository -- what a deployment deploys is a commit of it. Every git command
# runs INSIDE the project's dev container (the workspace is the agent's; the host never walks it), in the
# workspace's folder. The repo is created on first use; its .gitignore comes with the workspace template.
#
# The agent can use the repo too (and rewrite it -- it's theirs). That's fine: a commit only has to exist
# until its image is built, and rollbacks run from images.

from src.dto.projects import Project
from src.services import devenv

# Who the platform's commits are by. Passed as env vars, so the repo's own config is left alone.
_COMMITTER_ENV = {
    "GIT_AUTHOR_NAME": "biveit",
    "GIT_AUTHOR_EMAIL": "deploy@biveit.local",
    "GIT_COMMITTER_NAME": "biveit",
    "GIT_COMMITTER_EMAIL": "deploy@biveit.local",
}


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
