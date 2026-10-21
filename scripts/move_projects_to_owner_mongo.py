# Intentions: move projects set up before per-owner Mongo logins (services/devenv/databases) onto them.
# A project's backend/.env.local is written once and never re-rendered, so an older one still has the
# shared PROJECT_MONGO_URI login. For each live project's workspace with an app, this makes sure its
# owner's Mongo user exists with access to the project's dev database, rewrites just the MONGO_URI line,
# and restarts the dev servers that are running so they pick it up. Dry run by default; safe to re-run.
#
#   uv run python -m scripts.move_projects_to_owner_mongo [--apply]

import argparse
import asyncio
import re

from src.constants import DevServerAction
from src.dto.projects import Project
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import projects as projects_repo
from src.services import devenv
from src.services.devenv import databases
from src.utils import http, paths, postgres, redis

_MONGO_URI_LINE = re.compile(r"^MONGO_URI=.*$", re.MULTILINE)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Move projects onto their owner's own Mongo login.")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    args = parser.parse_args()

    await postgres.connect()
    await redis.connect()
    await http.connect()
    try:
        workspaces = [
            (project, conversation["id"])
            for project in await projects_repo.find({})
            for conversation in await ai_conversations_repo.find({"project_id": project["id"]})
            if devenv.has_app(project, conversation["id"])
        ]
        outdated = [
            (project, conversation_id)
            for project, conversation_id in workspaces
            if _is_outdated(project, conversation_id)
        ]
        print(f"{len(workspaces)} workspace(s) with an app, {len(outdated)} still on another Mongo login")
        for project, conversation_id in outdated:
            print(f"  project {project['id']} (owner {project['user_id']}), conversation {conversation_id}")
            if args.apply:
                await _move(project, conversation_id)
        if outdated and not args.apply:
            print("dry run -- re-run with --apply to change them")
    finally:
        await http.close()
        await redis.close()
        await postgres.close()


def _get_env_path(conversation_id: int) -> str:
    """The env file's path relative to the project folder -- read and written through utils/paths, since
    the folder is the agent's (see "PROJECT FOLDERS ARE UNTRUSTED INPUT" in CLAUDE.md)."""
    return f"{conversation_id}/backend/.env.local"


def _is_outdated(project: Project, conversation_id: int) -> bool:
    """Whether the workspace's env file has a MONGO_URI that isn't the owner's login. One that can't be
    read safely (missing, a symlink) isn't touched."""
    try:
        env_text = paths.read_within(devenv.get_project_dir(project), _get_env_path(conversation_id)).decode()
    except (paths.UnsafePath, FileNotFoundError):
        return False
    match = _MONGO_URI_LINE.search(env_text)
    return bool(match) and match.group(0) != f"MONGO_URI={databases.get_dev_mongo_uri(project)}"


async def _move(project: Project, conversation_id: int) -> None:
    """Grant the owner's login, rewrite the env file's MONGO_URI (owner-only, via a renamed temp file, like
    the setup writes it), and restart the dev servers if they're up."""
    await databases.ensure_dev_access(project)
    await asyncio.to_thread(
        _rewrite_mongo_uri,
        devenv.get_project_dir(project),
        _get_env_path(conversation_id),
        databases.get_dev_mongo_uri(project),
    )
    status = await devenv.get_devenv_status(project, conversation_id)
    if any(server["status"] == "online" for server in status["servers"]):
        await devenv.control_dev_servers(project, conversation_id, DevServerAction.RESTART)
        print("    rewritten, dev servers restarted")
    else:
        print("    rewritten (dev servers weren't running)")


def _rewrite_mongo_uri(project_dir: str, env_path: str, mongo_uri: str) -> None:
    env_text = paths.read_within(project_dir, env_path).decode()
    new_text = _MONGO_URI_LINE.sub(lambda _: f"MONGO_URI={mongo_uri}", env_text)
    paths.replace_file_within(project_dir, env_path, new_text.encode(), 0o600)


if __name__ == "__main__":
    asyncio.run(main())
