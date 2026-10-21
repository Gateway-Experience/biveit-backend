# Intentions: setting up a conversation's whole dev environment, once its agent starts implementing --
# the boilerplate's apps in the workspace, the owner's Mongo access, their env files, the project's
# container, a first install and the dev servers (both inside it), the dev site -- and describing it.
#
# Every step looks at what's already there and only does what's missing, so ensure_devenv is safe to
# run again and again: after a failure or a crash mid-way (it carries on from where it stopped), on
# every visit (it brings back servers a reboot took down), or twice at once (the lock makes the second
# one wait, then find everything done). What "already there" means is chosen so an interrupted step
# never looks finished: the boilerplate lands by rename, env files by rename, and npm counts as
# installed only once it wrote its end-of-install lockfile (the frontend's setup.sh checks the same). The agent's dev server commands take the
# same lock, so they never interleave with a setup's pm2 calls (which could start a server twice).

import asyncio
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pydash

from src.config import settings
from src.constants import (
    DEVENV_APP_DIRS,
    DEVENV_ENV_FILES,
    DEVENV_INSTALL_TIMEOUT_SECONDS,
    DEVENV_INTERNAL_BACKEND_PORT,
    DEVENV_INTERNAL_FRONTEND_PORT,
    DEVENV_LOCK_TTL_SECONDS,
    DEVENV_LOCK_WAIT_SECONDS,
    DevServerAction,
)
from src.dto.projects import Project
from src.services.devenv import addresses, containers, databases, servers, sites, templates, workspaces
from src.utils import paths, redis


async def ensure_devenv(project: Project, conversation_id: int) -> dict:
    """Make sure the conversation's dev environment is fully set up and its dev servers are up, and
    return describe_devenv's description of it. Files are put in place from the host (through
    utils/paths); everything that RUNS -- the installs, the servers -- runs in the project's container.
    Raises on the first step that fails -- CommandFailed, TemplateFailed, ContainerUnavailable,
    DevServersUnhealthy -- each with a message that reads on its own."""
    async with _lock_devenv(conversation_id):
        await workspaces.ensure_workspace(project, conversation_id)
        await workspaces.copy_boilerplate(project, conversation_id)
        await databases.ensure_dev_access(project)
        await _write_missing_env_files(project, conversation_id)
        await containers.ensure_container(project)
        await _install_dependencies(project, conversation_id)
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
    """[Pure-ish] Where everything is: {url, backend: {port, process, log}, frontend: {...}}. Ports are the
    servers' own, inside the project's container (where the agent works with them); log paths are
    relative to the workspace (the ecosystem template writes them there)."""
    names = servers.get_dev_server_names(project["id"])
    return {
        "url": addresses.get_dev_url(project),
        "backend": {"port": DEVENV_INTERNAL_BACKEND_PORT, "process": names["backend"], "log": "logs/be.log"},
        "frontend": {"port": DEVENV_INTERNAL_FRONTEND_PORT, "process": names["frontend"], "log": "logs/fe.log"},
    }


async def _write_missing_env_files(project: Project, conversation_id: int) -> None:
    """Render each of DEVENV_ENV_FILES that the workspace doesn't have yet, from the boilerplate's own
    templates (never the workspace's copies -- see DEVENV_ENV_FILES). Existing ones are never touched: they
    may hold edits, and they're the secrets the app is already using. All of them render BEFORE any is
    written, so a broken template leaves no half-configured app behind."""
    project_dir = workspaces.get_project_dir(project)
    boilerplate_dir = os.path.realpath(os.path.expanduser(settings.boilerplate_dir))
    missing = [
        (template_path, f"{conversation_id}/{output_path}")
        for template_path, output_path in DEVENV_ENV_FILES
        if paths.lstat_within(project_dir, f"{conversation_id}/{output_path}") is None
    ]
    rendered = [
        (
            output_path,
            templates.render_template_file(os.path.join(boilerplate_dir, template_path), project, conversation_id),
        )
        for template_path, output_path in missing
    ]
    # Owner-only (env files hold secrets), and atomic: a write cut off halfway must not leave a truncated
    # file that counts as written from then on.
    await asyncio.gather(
        *[
            asyncio.to_thread(paths.replace_file_within, project_dir, output_path, text.encode(), 0o600)
            for output_path, text in rendered
        ]
    )


async def _install_dependencies(project: Project, conversation_id: int) -> None:
    """Get each app's dependencies installed, all at once, INSIDE the project's container (what runs there
    must be built there). Each app brings its own setup.sh (the boilerplate's), run in its folder: it knows
    its own stack, is safe to re-run, and does nothing much when everything's installed. An app without one
    (a workspace from before setup scripts) gets the old commands instead:
      - backend: uv sync, every time -- quick when nothing changed, and it locks the environment
      - frontend: npm install until one has finished -- npm's end-of-install lockfile,
        node_modules/.package-lock.json (node_modules alone shows up as soon as an install starts)."""
    project_dir = workspaces.get_project_dir(project)

    def _to_setup_argv(app_dir: str) -> list[str] | None:
        app_path = f"{conversation_id}/{app_dir}"
        if _is_regular_file(project_dir, f"{app_path}/setup.sh"):
            return ["sh", "./setup.sh"]  # through sh: works whether or not the agent kept it executable
        if app_dir == "frontend" and _is_regular_file(project_dir, f"{app_path}/node_modules/.package-lock.json"):
            return None
        return _FALLBACK_SETUP_ARGV[app_dir]

    installs = [(app_dir, argv) for app_dir in DEVENV_APP_DIRS if (argv := _to_setup_argv(app_dir)) is not None]
    await asyncio.gather(
        *[
            containers.run_in_container(
                project["id"],
                argv,
                containers.to_container_path(project, os.path.join(project_dir, str(conversation_id), app_dir)),
                timeout_seconds=DEVENV_INSTALL_TIMEOUT_SECONDS,
            )
            for app_dir, argv in installs
        ]
    )


# What an app without its own setup.sh gets (_install_dependencies).
_FALLBACK_SETUP_ARGV = {"backend": ["uv", "sync"], "frontend": ["npm", "install"]}


def _is_regular_file(project_dir: str, project_relative_path: str) -> bool:
    try:
        entry = paths.lstat_within(project_dir, project_relative_path)
    except paths.UnsafePath:
        return False
    return entry is not None and stat.S_ISREG(entry.st_mode)
