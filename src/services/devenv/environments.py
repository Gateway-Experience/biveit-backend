# Intentions: setting up a conversation's whole dev environment, once its agent starts implementing --
# the boilerplate's apps in the workspace, the owner's Mongo access, their env files, a first install, the
# dev servers and the dev site -- and describing it.
#
# Every step looks at what's already there and only does what's missing, so ensure_devenv is safe to
# run again and again: after a failure or a crash mid-way (it carries on from where it stopped), on
# every visit (it brings back servers a reboot took down), or twice at once (the lock makes the second
# one wait, then find everything done). What "already there" means is chosen so an interrupted step
# never looks finished: the boilerplate lands by rename, env files by rename, and npm counts as
# installed only once it wrote its end-of-install lockfile. The agent's dev server commands take the
# same lock, so they never interleave with a setup's pm2 calls (which could start a server twice).

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pydash

from src.constants import (
    DEVENV_ENV_FILES,
    DEVENV_INSTALL_TIMEOUT_SECONDS,
    DEVENV_LOCK_TTL_SECONDS,
    DEVENV_LOCK_WAIT_SECONDS,
    DevServerAction,
)
from src.dto.projects import Project
from src.services.devenv import addresses, databases, servers, sites, templates, workspaces
from src.utils import paths, redis
from src.utils.processes import run_command


async def ensure_devenv(project: Project, conversation_id: int) -> dict:
    """Make sure the conversation's dev environment is fully set up and its dev servers are up, and
    return describe_devenv's description of it. Raises on the first step that fails -- CommandFailed,
    TemplateFailed, DevServersUnhealthy -- each with a message that reads on its own."""
    async with _lock_devenv(conversation_id):
        await workspaces.ensure_workspace(project, conversation_id)
        await workspaces.copy_boilerplate(project, conversation_id)
        await databases.ensure_dev_access(project)
        await _write_missing_env_files(project, conversation_id)
        await _install_dependencies(workspaces.get_conversation_dir(project, conversation_id))
        await servers.ensure_dev_servers(project, conversation_id)
        await sites.ensure_dev_site(project, conversation_id)
        await servers.wait_until_healthy(project["id"])
    return describe_devenv(project)


async def control_dev_servers(project: Project, conversation_id: int, action: DevServerAction) -> list[dict]:
    """servers.control_dev_servers, under the setup's lock (see the top of this file)."""
    async with _lock_devenv(conversation_id):
        return await servers.control_dev_servers(project, conversation_id, action)


async def stop_dev_servers(project: Project, conversation_id: int) -> list[str]:
    """Turn the project's dev servers off (servers.remove_dev_servers), under the setup's lock. Idempotent;
    returns the pm2 names it removed. The workspace is untouched, so the next ensure_devenv brings them
    back."""
    async with _lock_devenv(conversation_id):
        return await servers.remove_dev_servers(project["id"])


@asynccontextmanager
async def _lock_devenv(conversation_id: int) -> AsyncIterator[None]:
    """The one lock around everything that changes a conversation's dev environment."""
    async with redis.lock(_get_lock_key(conversation_id), DEVENV_LOCK_TTL_SECONDS, DEVENV_LOCK_WAIT_SECONDS):
        yield


def _get_lock_key(conversation_id: int) -> str:
    return f"devenv:{conversation_id}"


async def get_devenv_status(project: Project, conversation_id: int) -> dict:
    """The dev environment as the FE needs it for previewing, all read live (nothing is recorded):
    busy         whether a setup or a dev server command is running right now (the lock is held)
    preview_url  where the dev site opens; None until the app is in the workspace
    servers      [{app, status, port}] from pm2 (status as in servers.get_dev_server_statuses); [] until
                 the app is in the workspace"""
    app_present = workspaces.has_app(project, conversation_id)
    statuses = await servers.get_dev_server_statuses(project["id"]) if app_present else []
    return {
        "busy": await redis.flag_exists(_get_lock_key(conversation_id)),
        "preview_url": addresses.get_dev_url(project) if app_present else None,
        "servers": [pydash.omit(status, "name") for status in statuses],
    }


def describe_devenv(project: Project) -> dict:
    """[Pure-ish] Where everything is: {url, backend: {port, process, log}, frontend: {...}}. Log paths
    are relative to the workspace (the ecosystem template writes them there)."""
    names = servers.get_dev_server_names(project["id"])
    return {
        "url": addresses.get_dev_url(project),
        "backend": {
            "port": addresses.get_dev_backend_port(project["id"]),
            "process": names["backend"],
            "log": "logs/be.log",
        },
        "frontend": {
            "port": addresses.get_dev_frontend_port(project["id"]),
            "process": names["frontend"],
            "log": "logs/fe.log",
        },
    }


async def _write_missing_env_files(project: Project, conversation_id: int) -> None:
    """Render each of DEVENV_ENV_FILES that the workspace doesn't have yet. Existing ones are never
    touched: they may hold edits, and they're the secrets the app is already using. All of them render
    BEFORE any is written, so a broken template leaves no half-configured app behind."""
    project_dir = workspaces.get_project_dir(project)
    missing = [
        (template_name, f"{conversation_id}/{output_path}")
        for template_name, output_path in DEVENV_ENV_FILES
        if paths.lstat_within(project_dir, f"{conversation_id}/{output_path}") is None
    ]
    rendered = [
        (output_path, templates.render_template(template_name, project, conversation_id))
        for template_name, output_path in missing
    ]
    # Owner-only (env files hold secrets), and atomic: a write cut off halfway must not leave a truncated
    # file that counts as written from then on.
    await asyncio.gather(
        *[
            asyncio.to_thread(paths.replace_file_within, project_dir, output_path, text.encode(), 0o600)
            for output_path, text in rendered
        ]
    )


async def _install_dependencies(workspace_dir: str) -> None:
    """Get each app's dependencies installed, both at once. After the first time, keeping them up to
    date is the agent's job -- it knows when it changed them -- so this only makes sure there's a
    complete install:
      - uv sync runs every time: it's quick when nothing changed, and it locks the environment, so it's
        safe even while the agent runs its own.
      - npm install runs only until one has finished: npm has no such lock, and two at once in the same
        folder can wreck it. "Finished" is npm's own end-of-install lockfile, node_modules/.package-lock.json
        -- node_modules alone shows up as soon as an install starts, so an interrupted one would pass."""
    frontend_dir = os.path.join(workspace_dir, "frontend")
    npm_installed = os.path.isfile(os.path.join(frontend_dir, "node_modules", ".package-lock.json"))
    installs = [
        run_command(
            ["uv", "sync"], os.path.join(workspace_dir, "backend"), timeout_seconds=DEVENV_INSTALL_TIMEOUT_SECONDS
        ),
        *(
            []
            if npm_installed
            else [run_command(["npm", "install"], frontend_dir, timeout_seconds=DEVENV_INSTALL_TIMEOUT_SECONDS)]
        ),
    ]
    await asyncio.gather(*installs)
