# Intentions: each project's container -- where everything that runs FOR the project will run: its dev
# servers, its own Redis, its agent's session runner (see docs/plans/project-containers.md). The container
# is the boundary between users: the agent can do anything inside it, nothing inside it sees the platform.
#
#   name        biveit-<env>-project-<id>, derived -- nothing is recorded but the image tag (below)
#   image       the tag the project was created with (projects.meta.devenv_image): new projects get
#               DEVENV_IMAGE, existing ones keep theirs until upgraded by hand
#   mounts      the project's folder at /project, its runner sockets' folder at /sockets -- nothing else
#   network     DEVENV_NETWORK, a fixed subnet (settings.devenv_network_subnet): the gateway is the one
#               host address a container can reach. Dev ports published on the host's 127.0.0.1 only
#   runs as     the backend's own uid -- which is why the host must never follow a path in the project
#               folder (utils/paths, "PROJECT FOLDERS ARE UNTRUSTED INPUT" in CLAUDE.md)
#   hardening   no capabilities, no new privileges, an init process; never --privileged, never the
#               docker socket. Limits: DEVENV_CONTAINER_LIMITS
#
# Disposable: all of a project's state lives in its folder (its own under DEVENV_STATE_DIR), so a container
# can be removed and recreated any time. Every "ensure" asks docker what's there and only does the rest.

import asyncio
import ipaddress
import json
import os

import pydash

from src.config import settings
from src.constants import (
    DEVENV_CONTAINER_LIMITS,
    DEVENV_IMAGE,
    DEVENV_INTERNAL_BACKEND_PORT,
    DEVENV_INTERNAL_FRONTEND_PORT,
    DEVENV_NETWORK,
    DEVENV_PROJECT_PATH,
    DEVENV_SOCKETS_PATH,
    DEVENV_STATE_DIR,
)
from src.dto.projects import Project
from src.repositories import projects as projects_repo
from src.services.devenv import addresses, workspaces
from src.utils.processes import CommandFailed, run_command, run_command_binary

# The account the container's processes run as -- ours, by uid -- named inside the container, since some
# tools look the user up by name.
_CONTAINER_USER = "biveit"


class ContainerUnavailable(Exception):
    """The project's container couldn't be created or started (no image, docker down...). 503."""


def get_container_name(project_id: int) -> str:
    """[Pure-ish] The env is in it: every backend on the machine (local, dev...) shares one docker, and
    project ids only mean something within one database."""
    return f"biveit-{settings.env}-project-{project_id}"


def get_host_socket_dir(project_id: int) -> str:
    """[Pure-ish] The project's runner sockets on the host, mounted at /sockets. Next to the storage root, not
    in the project folder: unix socket paths are capped at ~108 bytes, and the host connects by the HOST
    path, which in the project folder would run too long."""
    return os.path.join(os.path.dirname(os.path.realpath(settings.storage_dir)), "agent2_sockets", str(project_id))


def get_gateway_ip() -> str:
    """[Pure-ish] The host's address on the containers' network -- the first one of its subnet."""
    return str(next(ipaddress.ip_network(settings.devenv_network_subnet).hosts()))


def to_container_path(project: Project, host_path: str) -> str:
    """[Pure-ish] A host path in the project's folder as the container sees it. Raises ValueError for any
    other path: nothing else is in there."""
    relative = os.path.relpath(host_path, workspaces.get_project_dir(project))
    if relative == ".." or relative.startswith("../") or os.path.isabs(relative):
        raise ValueError(f"{host_path} isn't in project {project['id']}'s folder")
    return DEVENV_PROJECT_PATH if relative == "." else f"{DEVENV_PROJECT_PATH}/{relative}"


async def ensure_container(project: Project) -> None:
    """Make sure the project's container is running. On the inspect:
      none      record the project's image tag if it has none yet, then create it
      stopped   start it
      running   nothing to do
    Then its user (a recreated container loses it). A creation race lost to another ensure_container (the
    name was taken a moment ago) is fine if the other one's container runs. Raises ContainerUnavailable."""
    name = get_container_name(project["id"])
    container = await _inspect(name)
    if not (container and container["State"]["Running"]):
        if settings.env == "local":
            # Locally every project publishes the same two dev ports (LOCAL_DEV_BACKEND_PORT), so only one
            # project's container can run at a time: starting this one stops the others (their agents too).
            await _stop_other_local_containers(name)
        try:
            if container:
                await run_command(["docker", "start", name])
            else:
                image = await _ensure_image_recorded(project)
                await _prepare_mounts(project)
                await ensure_network()
                await run_command(_to_run_argv(project, image))
        except CommandFailed as error:
            if not await is_container_running(project["id"]):
                raise ContainerUnavailable(f"project {project['id']}'s container couldn't start: {error}") from error
    await _ensure_container_user(project["id"])


async def stop_container(project_id: int) -> bool:
    """Stop the project's container, and everything in it. Idempotent; returns whether it was running. Kept,
    so the next ensure_container just starts it again."""
    if not await is_container_running(project_id):
        return False
    await run_command(["docker", "stop", get_container_name(project_id)])
    return True


async def remove_container(project_id: int) -> None:
    """Remove the project's container altogether (its folder stays). Idempotent."""
    if await _inspect(get_container_name(project_id)):
        await run_command(["docker", "rm", "--force", get_container_name(project_id)])


async def list_running_project_ids() -> list[int]:
    """The projects (of this backend's env) whose containers are running, by the labels every container gets."""
    output = await run_command(
        ["docker", "ps", f"--filter=label=biveit.env={settings.env}", '--format={{.Label "biveit.project"}}']
    )
    return [int(project_id) for project_id in output.split() if project_id.isdigit()]


async def is_container_running(project_id: int) -> bool:
    container = await _inspect(get_container_name(project_id))
    return bool(container and container["State"]["Running"])


async def run_in_container(
    project_id: int,
    argv: list[str],
    cwd: str | None = None,
    user: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 600,
) -> str:
    """run_command inside the project's running container: as its usual user unless `user` (e.g. "root"),
    in `cwd` (a CONTAINER path). `env` goes by NAME on the command line -- docker copies the values from
    its own environment -- so no secret shows up in `ps`."""
    env_vars = env or {}
    return await run_command(
        _to_exec_argv(project_id, argv, cwd, user, env_vars),
        timeout_seconds=timeout_seconds,
        env={**_get_docker_env(), **env_vars} if env_vars else None,
    )


async def run_in_container_binary(project_id: int, argv: list[str], cwd: str | None = None) -> bytes:
    """run_in_container for a command whose output is data (e.g. `git archive`): its stdout, as raw bytes."""
    return await run_command_binary(_to_exec_argv(project_id, argv, cwd, None, {}))


def _to_exec_argv(project_id: int, argv: list[str], cwd: str | None, user: str | None, env_vars: dict) -> list[str]:
    """[Pure-ish] The `docker exec` running argv in the project's container (see run_in_container)."""
    return [
        "docker",
        "exec",
        *(["--workdir", cwd] if cwd else []),
        *(["--user", user] if user else []),
        *[option for name in env_vars for option in ("--env", name)],
        get_container_name(project_id),
        *argv,
    ]


async def extract_into_container(project_id: int, archive: bytes, container_dir: str) -> None:
    """Unpack a tar archive (ours, trusted) into `container_dir` in the container, keeping the owners the
    archive records. That's the way to put root-owned files in there: docker cp of a host folder hands them
    to the container's user (ours -- the agent's), and the container's root can't chown them back (no
    capabilities). Gotcha, checked by hand: WITHOUT --archive is what keeps the tar's owners -- --archive
    makes docker hand everything to the container's user again."""
    await run_command(["docker", "cp", "-", f"{get_container_name(project_id)}:{container_dir}"], input_text=archive)


async def ensure_network() -> None:
    """The containers' network: DEVENV_NETWORK on settings.devenv_network_subnet, its bridge interface named
    after it. Containers on it can't reach each other (enable_icc=false): every project shares the bridge,
    and the user is the security boundary. They still reach the host at the gateway (what the host lets in
    is the firewall's call). Idempotent -- but only on the name: a network made before an option was added
    keeps its old options until it's removed and recreated."""
    try:
        await run_command(["docker", "network", "inspect", DEVENV_NETWORK])
    except CommandFailed:
        await run_command(
            [
                "docker",
                "network",
                "create",
                f"--subnet={settings.devenv_network_subnet}",
                f"--opt=com.docker.network.bridge.name={DEVENV_NETWORK}",
                "--opt=com.docker.network.bridge.enable_icc=false",
                DEVENV_NETWORK,
            ]
        )


def _to_run_argv(project: Project, image: str) -> list[str]:
    """[Pure-ish] The `docker run` creating the project's container (see the top of this file)."""
    state_dir = f"{DEVENV_PROJECT_PATH}/{DEVENV_STATE_DIR}"
    limits = DEVENV_CONTAINER_LIMITS
    published = [
        (addresses.get_dev_backend_port(project["id"]), DEVENV_INTERNAL_BACKEND_PORT),
        (addresses.get_dev_frontend_port(project["id"]), DEVENV_INTERNAL_FRONTEND_PORT),
    ]
    return [
        "docker",
        "run",
        "--detach",
        f"--name={get_container_name(project['id'])}",
        f"--label=biveit.env={settings.env}",
        f"--label=biveit.project={project['id']}",
        "--init",
        f"--user={os.getuid()}:{os.getgid()}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--network={DEVENV_NETWORK}",
        f"--memory={limits['memory']}",
        f"--memory-swap={limits['memory']}",
        f"--cpus={limits['cpus']}",
        f"--pids-limit={limits['pids']}",
        f"--ulimit=nofile={limits['nofile']}:{limits['nofile']}",
        f"--tmpfs=/tmp:size={limits['tmp_size']},mode=1777",
        f"--volume={workspaces.get_project_dir(project)}:{DEVENV_PROJECT_PATH}",
        f"--volume={get_host_socket_dir(project['id'])}:{DEVENV_SOCKETS_PATH}",
        f"--env=HOME={state_dir}/home",
        f"--env=PM2_HOME={state_dir}/pm2",
        *[f"--publish=127.0.0.1:{host_port}:{internal_port}" for host_port, internal_port in published],
        image,
        "sleep",
        "infinity",
    ]


async def _ensure_image_recorded(project: Project) -> str:
    """The image the project's container runs: the tag it got when first created, or DEVENV_IMAGE now if
    it never had one (recorded, so it keeps it from then on)."""
    recorded = pydash.get(project, "meta.devenv_image")
    if recorded:
        return recorded
    await projects_repo.update(project["id"], {"meta": {"devenv_image": DEVENV_IMAGE}})
    return DEVENV_IMAGE


async def _prepare_mounts(project: Project) -> None:
    """Create what gets mounted, as ours (docker would create a missing one owned by root). The socket
    folder is owner-only: nobody else needs to see the sockets, though each request needs a token too."""
    await asyncio.gather(
        asyncio.to_thread(os.makedirs, workspaces.get_project_dir(project), exist_ok=True),
        asyncio.to_thread(os.makedirs, get_host_socket_dir(project["id"]), 0o700, True),
    )


async def _ensure_container_user(project_id: int) -> None:
    """A named account for our uid inside the container, if it doesn't have one yet. Appended to
    /etc/passwd and /etc/group directly: useradd needs capabilities (to write /etc/shadow) that even the
    container's root doesn't get here (--cap-drop=ALL), and nobody logs in, so there's no password entry."""
    uid, gid = os.getuid(), os.getgid()
    home = f"{DEVENV_PROJECT_PATH}/{DEVENV_STATE_DIR}/home"
    script = (
        f"grep -q '^[^:]*:[^:]*:{gid}:' /etc/group || echo '{_CONTAINER_USER}:x:{gid}:' >> /etc/group; "
        f"grep -q '^[^:]*:[^:]*:{uid}:' /etc/passwd || echo '{_CONTAINER_USER}:x:{uid}:{gid}::{home}:/bin/bash' >> /etc/passwd"
    )
    await run_in_container(project_id, ["sh", "-c", script], user="root")


async def _stop_other_local_containers(name: str) -> None:
    running = await run_command(
        [
            "docker",
            "ps",
            "--format={{.Names}}",
            f"--filter=label=biveit.env={settings.env}",
            "--filter=label=biveit.project",
        ]
    )
    await asyncio.gather(*[run_command(["docker", "stop", other]) for other in running.split() if other != name])


async def _inspect(name: str) -> dict | None:
    """docker inspect's view of the container -- None when there's none by that name, or no docker at all
    (nothing to stop or remove then)."""
    try:
        return json.loads(await run_command(["docker", "inspect", "--type=container", name]))[0]
    except (CommandFailed, FileNotFoundError):
        return None


def _get_docker_env() -> dict[str, str]:
    """What the docker CLI itself needs from our environment: where to find the daemon."""
    return pydash.pick(dict(os.environ), "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")
