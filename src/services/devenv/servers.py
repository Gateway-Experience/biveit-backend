# Intentions: a project's two dev servers -- its backend (uvicorn, reloading on save) and its frontend
# (Vite, hot reload) -- plus its own Redis, run by pm2 from the workspace's ecosystem.config.cjs as
# project_<id>_be, project_<id>_fe and project_<id>_redis. Redis comes and goes with the servers, but
# isn't one of them as far as callers go: the statuses list only the two (what the preview shows).
#
# pm2 runs INSIDE the project's container (devenv/containers), one per project, and is the source of
# truth for what's running (`pm2 jlist`); nothing is recorded on our side. A container that isn't
# running has no dev servers at all. The file pm2 starts them from is put in place by us (through
# utils/paths) and read by pm2 inside the container, so a link the agent planted there leads nowhere.

import asyncio
import json
import os
import time
from collections.abc import Callable

import aiohttp
import pydash

from src.constants import (
    DEVENV_BACKEND_HEALTH_PATH,
    DEVENV_ECOSYSTEM_TEMPLATE,
    DEVENV_HEALTH_WAIT_SECONDS,
    DEVENV_INTERNAL_BACKEND_PORT,
    DEVENV_INTERNAL_FRONTEND_PORT,
    DEVENV_STATE_DIR,
    DevServerAction,
)
from src.dto.projects import Project
from src.services.devenv import addresses, containers, templates, workspaces
from src.utils import http, paths


class DevServersUnhealthy(Exception):
    """The dev servers were started but didn't answer within DEVENV_HEALTH_WAIT_SECONDS."""


def get_dev_server_names(project_id: int) -> dict[str, str]:
    """[Pure] {"backend": pm2 name, "frontend": pm2 name}. The ecosystem template names them the same."""
    return {"backend": f"project_{project_id}_be", "frontend": f"project_{project_id}_fe"}


def _get_pm2_names(project_id: int) -> list[str]:
    """[Pure] Everything we run under pm2 for the project: the two servers and its Redis."""
    return [*get_dev_server_names(project_id).values(), f"project_{project_id}_redis"]


async def get_dev_server_statuses(project_id: int) -> list[dict]:
    """Each of the project's dev servers as {app, name, status, port}. status is pm2's ("online",
    "stopped", "errored"...) or "missing" when pm2 doesn't know the process at all -- or when the project's
    container isn't running. port is the server's own, inside the container."""
    statuses = {process["name"]: pydash.get(process, "pm2_env.status") for process in await _list_pm2(project_id)}
    ports = {"backend": DEVENV_INTERNAL_BACKEND_PORT, "frontend": DEVENV_INTERNAL_FRONTEND_PORT}
    return [
        {"app": app, "name": name, "status": statuses.get(name, "missing"), "port": ports[app]}
        for app, name in get_dev_server_names(project_id).items()
    ]


async def ensure_dev_servers(project: Project, conversation_id: int) -> None:
    """Re-render the pm2 file and start whichever of the project's dev servers (and its Redis) isn't
    online, in its container -- which must be running. Ones that are online are left running (a restart
    would kill Vite's hot reload for no reason)."""
    ecosystem_path = await _write_ecosystem_file(project, conversation_id)
    statuses = {process["name"]: pydash.get(process, "pm2_env.status") for process in await _list_pm2(project["id"])}
    not_online = [name for name in _get_pm2_names(project["id"]) if statuses.get(name) != "online"]
    if not_online:
        await _start_fresh(project, ecosystem_path, not_online)


async def control_dev_servers(project: Project, conversation_id: int, action: DevServerAction) -> list[dict]:
    """Do `action` to the project's dev servers and return their statuses after it. RESTART starts them
    from a freshly rendered pm2 file, so it also picks up changes to it."""
    names = _get_pm2_names(project["id"])
    if action == DevServerAction.START:
        await ensure_dev_servers(project, conversation_id)
    elif action == DevServerAction.RESTART:
        await _start_fresh(project, await _write_ecosystem_file(project, conversation_id), names)
    elif action == DevServerAction.STOP:
        await _run_pm2_on_known(project["id"], "stop", names)
    return await get_dev_server_statuses(project["id"])


async def wait_until_healthy(project_id: int) -> None:
    """Poll both dev servers until they answer: the backend's health endpoint with a 200, Vite with
    anything that isn't a server error. Raises DevServersUnhealthy after DEVENV_HEALTH_WAIT_SECONDS."""
    deadline = time.monotonic() + DEVENV_HEALTH_WAIT_SECONDS
    backend_url = f"http://127.0.0.1:{addresses.get_dev_backend_port(project_id)}{DEVENV_BACKEND_HEALTH_PATH}"
    frontend_url = f"http://127.0.0.1:{addresses.get_dev_frontend_port(project_id)}/"
    await asyncio.gather(
        _wait_for_url(backend_url, lambda status: status == 200, deadline),
        _wait_for_url(frontend_url, lambda status: status < 500, deadline),
    )


async def _wait_for_url(url: str, is_up: Callable[[int], bool], deadline: float) -> None:
    """Recursive: one request per call, a second apart, until `url` answers with a status `is_up`
    accepts, or the deadline passes."""
    try:
        status, _, _ = await http.request("GET", url, timeout_seconds=5)
        if is_up(status):
            return
    except (OSError, TimeoutError, aiohttp.ClientError):
        pass  # refused, reset, timed out: all just "not up yet" while polling
    if time.monotonic() > deadline:
        raise DevServersUnhealthy(f"{url} didn't answer within {DEVENV_HEALTH_WAIT_SECONDS}s")
    await asyncio.sleep(1)
    await _wait_for_url(url, is_up, deadline)


async def _write_ecosystem_file(project: Project, conversation_id: int) -> str:
    """Render the pm2 file into the workspace (and make the folders it points at: logs/, the project's
    Redis data) and return its path AS THE CONTAINER SEES IT -- that's where pm2 reads it."""
    template_name, output_path = DEVENV_ECOSYSTEM_TEMPLATE
    project_dir = workspaces.get_project_dir(project)
    ecosystem_text = templates.render_template(template_name, project, conversation_id)
    for folder in (f"{conversation_id}/logs", f"{DEVENV_STATE_DIR}/redis"):  # one side effect each: the mkdir
        os.close(await asyncio.to_thread(paths.open_dir_within, project_dir, folder, True))
    await asyncio.to_thread(
        paths.replace_file_within, project_dir, f"{conversation_id}/{output_path}", ecosystem_text.encode()
    )
    return containers.to_container_path(
        project, os.path.join(workspaces.get_conversation_dir(project, conversation_id), output_path)
    )


async def _start_fresh(project: Project, ecosystem_path: str, names: list[str]) -> None:
    """(Re)start `names` from the pm2 file (a container path). Deleted first: `pm2 restart` keeps the OLD
    args and env, so a port or env change in the file would otherwise be ignored."""
    await _run_pm2_on_known(project["id"], "delete", names)
    await containers.run_in_container(project["id"], ["pm2", "start", ecosystem_path, "--only", ",".join(names)])


async def remove_dev_servers(project_id: int) -> list[str]:
    """Take the project's dev servers (and its Redis) out of pm2 altogether (stop + forget). Idempotent;
    returns the names it removed. Starting them again is ensure_dev_servers' job, from the pm2 file."""
    return await _run_pm2_on_known(project_id, "delete", _get_pm2_names(project_id))


async def _run_pm2_on_known(project_id: int, command: str, names: list[str]) -> list[str]:
    """`pm2 <command> <name>` for each of `names` pm2 knows, and return those. The others are skipped:
    there's nothing to stop or delete, and pm2 would fail on them."""
    known_names = pydash.map_(await _list_pm2(project_id), "name")
    acted_on = [name for name in names if name in known_names]
    await asyncio.gather(*[containers.run_in_container(project_id, ["pm2", command, name]) for name in acted_on])
    return acted_on


async def _list_pm2(project_id: int) -> list[dict]:
    """`pm2 jlist` in the project's container -- nothing when the container isn't running. Only the output's
    last line is the list: the first pm2 call in a fresh container also starts pm2's daemon, and says so
    ("[PM2] Spawning PM2 daemon...") before it."""
    if not await containers.is_container_running(project_id):
        return []
    return json.loads((await containers.run_in_container(project_id, ["pm2", "jlist"])).strip().splitlines()[-1])
