# Intentions: a project's site container -- the built app, running in production. Unlike the dev container
# it mounts nothing of the project's: it runs a frozen image (images.py), and keeps its own state (Redis,
# anything the app stores on disk) in a named volume, the one thing that outlives a deployment.
#
#   name      biveit-<env>-site-<id>, labelled with the commit it runs and when it was deployed -- docker is
#             the source of truth for what's live, nothing is recorded on our side
#   network   the projects' network (devenv), so it reaches the platform's Mongo at the same gateway address,
#             and nothing else of the host; containers there can't reach each other
#   port      SITE_PORT_BASE + id on the host's 127.0.0.1 only: the host's nginx is the way in
#   restart   unless-stopped, so docker brings sites back after a reboot or a crash
#   hardening no capabilities, no new privileges, an init process, the image's unprivileged user

import json
from datetime import UTC, datetime

import aiohttp
import pydash

from src.config import settings
from src.constants import DEVENV_NETWORK
from src.dto.projects import Project
from src.modules.deployments.constants import (
    SITE_CONTAINER_LIMITS,
    SITE_HEALTH_PATH,
    SITE_HEALTH_WAIT_SECONDS,
    SITE_INTERNAL_PORT,
    SITE_PORT_BASE,
)
from src.services import devenv
from src.utils import http
from src.utils.common import with_retries
from src.utils.processes import CommandFailed, get_base_env, run_command


class SiteUnhealthy(Exception):
    """The site's container started but its backend didn't answer within SITE_HEALTH_WAIT_SECONDS."""


def get_site_port(project_id: int) -> int:
    return SITE_PORT_BASE + project_id


def _get_container_name(project_id: int) -> str:
    return f"biveit-{settings.env}-site-{project_id}"


async def get_live_release(project_id: int) -> dict | None:
    """What the site's container runs, read from docker: {commit, deployed_at, running}, or None when there's
    no container (never deployed)."""
    try:
        output = await run_command(["docker", "inspect", "--type=container", _get_container_name(project_id)])
    except CommandFailed:
        return None
    container = json.loads(output)[0]
    labels = pydash.get(container, "Config.Labels") or {}
    return {
        "commit": labels.get("biveit.commit"),
        "deployed_at": labels.get("biveit.deployed_at"),
        "running": bool(pydash.get(container, "State.Running")),
    }


async def ensure_site_container(project: Project, image_tag: str, commit: str, site_env: dict[str, str]) -> None:
    """Make sure the site's container runs `commit` and answers. Already running it and healthy -> nothing to
    do. Otherwise the old container goes (its volume stays) and a new one starts from `image_tag`, with
    `site_env` as its environment -- passed by NAME, so no value shows up in `ps`. Raises SiteUnhealthy."""
    live = await get_live_release(project["id"])
    if live and live["commit"] == commit and live["running"] and await _is_healthy(project["id"]):
        return
    if live:
        await run_command(["docker", "rm", "--force", _get_container_name(project["id"])])
    await devenv.ensure_network()
    await run_command(_to_run_argv(project, image_tag, commit, list(site_env)), env={**get_base_env(), **site_env})
    try:
        await with_retries(
            lambda: _check_health(project["id"]), SITE_HEALTH_WAIT_SECONDS, calculate_backoff=lambda _: 1
        )
    except Exception as error:
        raise SiteUnhealthy(f"project {project['id']}'s site didn't answer: {error}") from error


def _to_run_argv(project: Project, image_tag: str, commit: str, env_names: list[str]) -> list[str]:
    """[Pure-ish] The `docker run` starting the site's container (see the top of this file)."""
    project_id = project["id"]
    limits = SITE_CONTAINER_LIMITS
    return [
        "docker",
        "run",
        "--detach",
        f"--name={_get_container_name(project_id)}",
        f"--label=biveit.env={settings.env}",
        f"--label=biveit.site={project_id}",
        f"--label=biveit.commit={commit}",
        f"--label=biveit.deployed_at={datetime.now(UTC).isoformat()}",
        "--restart=unless-stopped",
        "--init",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--network={DEVENV_NETWORK}",
        f"--memory={limits['memory']}",
        f"--memory-swap={limits['memory']}",
        f"--cpus={limits['cpus']}",
        f"--pids-limit={limits['pids']}",
        f"--tmpfs=/tmp:size={limits['tmp_size']},mode=1777",
        f"--volume=biveit-{settings.env}-site-{project_id}-data:/data",
        f"--publish=127.0.0.1:{get_site_port(project_id)}:{SITE_INTERNAL_PORT}",
        *[option for name in env_names for option in ("--env", name)],
        image_tag,
    ]


async def _check_health(project_id: int) -> None:
    if not await _is_healthy(project_id):
        raise SiteUnhealthy(f"{SITE_HEALTH_PATH} on port {get_site_port(project_id)} isn't answering 200")


async def _is_healthy(project_id: int) -> bool:
    try:
        status, _, _ = await http.request(
            "GET", f"http://127.0.0.1:{get_site_port(project_id)}{SITE_HEALTH_PATH}", timeout_seconds=5
        )
        return status == 200
    except (OSError, TimeoutError, aiohttp.ClientError):
        return False  # refused, reset, timed out: all just "not up (yet)"
