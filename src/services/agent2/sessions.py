# Intentions: agent2's sessions as the backend sees them -- each conversation's runner (vib3-session-runner)
# started when needed, and talked to over its unix socket. The runtime-specific half only: what to say to
# the agent (introductions, translating stored messages into the channel's format) stays where it is.
#
# A runner runs IN its project's container (services/devenv/containers), under the container's pm2 as
# runner_<conversation>, from the copy installed there (runners.py) -- and so does the Claude Code it
# starts: everything the agent does happens in there. It belongs to the container, not to us, so a backend
# restart or deploy doesn't touch it. It calls back to the backend's agent listener on the containers'
# gateway (src/app.py), and so do Claude Code's model calls: the model key stays with us (agent2/models).
#
# Nothing is recorded anywhere. A conversation's runner listens at a socket in its project's socket folder
# (mounted at /sockets), and "is it running?" is just "does it answer there?" -- so a crashed runner simply
# stops answering (and pm2 starts it again), and the next ensure_session starts a fresh one if it must,
# which continues the workspace's Claude session. Two racing starts are fine too: only one runner gets the
# socket (the runner's main.claim_socket).
# References: vib3-session-runner's session_runner/main.py (the other end), services/agent/adapter.py (the
# tmux runtime).

import asyncio
import os
import time

import aiohttp

from src.config import settings
from src.constants import (
    AGENT2_ENDPOINT_ENV,
    AGENT2_REQUEST_TIMEOUT_SECONDS,
    AGENT2_SOCKET_ENV,
    AGENT2_START_WAIT_SECONDS,
    AGENT_LISTENER_PORT,
    DEVENV_SOCKETS_PATH,
    DEVENV_STATE_DIR,
    RUNNER_INSTALL_PATH,
    SESSION_OUTBOX_URL_ENV,
    SESSION_SEND_ATTEMPTS,
    SESSION_TOKEN_ENV,
)
from src.dto.projects import Project
from src.services import devenv
from src.services.agent import tokens
from src.services.agent2 import runners
from src.utils import paths
from src.utils.common import exponential_backoff, with_retries
from src.utils.processes import CommandFailed


class RunnerUnavailable(Exception):
    """The conversation's runner couldn't be started, didn't come up in time, or refused a message. 503."""


def get_socket_path(project: Project, conversation_id: int) -> str:
    """[Pure-ish] Where the conversation's runner listens, as the HOST sees it (the backend connects there):
    its project's socket folder, the one mounted into the container at /sockets."""
    return os.path.join(devenv.get_host_socket_dir(project["id"]), f"{conversation_id}.sock")


def get_log_path(project: Project, conversation_id: int) -> str:
    """[Pure-ish] The runner's output on the host (pm2 writes it, in the container): in the project's state
    folder -- so read it through utils/paths, it's the agent's."""
    return os.path.join(devenv.get_project_dir(project), DEVENV_STATE_DIR, "logs", f"runner_{conversation_id}.log")


def get_backend_url() -> str:
    """[Pure-ish] The backend as a container reaches it: its agent listener on the containers' gateway."""
    return f"http://{devenv.get_gateway_ip()}:{AGENT_LISTENER_PORT}"


def get_process_name(conversation_id: int) -> str:
    """[Pure] The runner's pm2 name in its project's container."""
    return f"runner_{conversation_id}"


async def ensure_session(project: Project, conversation_id: int) -> None:
    """Make sure the conversation's runner is up, on its socket:
      answers its health check             -> done
      accepts connections but won't answer -> hung: thrown out, then a fresh one started
      nothing there                        -> its workspace, its container and the runner installed in it
                                              made sure of, a runner started
    and then waited for until it answers. Idempotent -- safe on every visit and before every send. Raises
    RunnerUnavailable."""
    state = await _probe(project, conversation_id)
    if state == "up":
        return
    if state == "hung":
        await _delete_runner(project["id"], conversation_id)
    await devenv.ensure_workspace(project, conversation_id)
    try:
        await devenv.ensure_container(project)
        await runners.ensure_runner_installed(project)
    except (devenv.ContainerUnavailable, runners.RunnerInstallFailed) as error:
        raise RunnerUnavailable(str(error)) from error
    await _start_runner(project, conversation_id)
    await _wait_until_up(project, conversation_id, time.monotonic() + AGENT2_START_WAIT_SECONDS)


async def send(project: Project, conversation_id: int, content: list[dict]) -> None:
    """Push a message (the channel's format: {content: [...]}'s items) into the conversation's session.
    Retries connection failures for a few seconds, since a runner that was just started takes a moment to
    listen. Raises RunnerUnavailable if it never answers, or answers with anything but success."""
    try:
        status = await with_retries(
            lambda: _request_runner(project, conversation_id, "POST", "/messages", {"content": content}),
            retries=SESSION_SEND_ATTEMPTS,
            calculate_backoff=exponential_backoff(0.2, cap=2),
            # Not listening yet / gone: OSErrors (aiohttp's connection errors included). But never a timeout
            # (an OSError too): the runner may well have taken the message, and a retry would say it twice.
            should_retry=lambda error, _: isinstance(error, OSError) and not isinstance(error, TimeoutError),
        )
    except OSError as error:
        raise RunnerUnavailable(f"conversation {conversation_id}'s runner unreachable") from error
    if not 200 <= status < 300:
        raise RunnerUnavailable(f"conversation {conversation_id}'s runner answered {status}", {"status": status})


async def stop_session(project: Project, conversation_id: int) -> bool:
    """Take the conversation's runner out of its container's pm2 (it gets SIGTERM and finishes up). Not
    /shutdown: pm2 would only start it again. Idempotent; returns whether there was one to stop. Its
    Claude session stays on disk, so the next ensure_session continues it."""
    if not await devenv.is_container_running(project["id"]):
        return False
    return await _delete_runner(project["id"], conversation_id)


async def is_session_up(project: Project, conversation_id: int) -> bool:
    """Whether the conversation's runner is up: it answers its health check. A socket file nobody listens on
    (a runner that died) counts as down."""
    return await _probe(project, conversation_id) == "up"


async def _probe(project: Project, conversation_id: int) -> str:
    """The runner's state on its socket: "up" (answers health), "hung" (a connection, but no answer in
    time), "down" (nothing listening)."""
    try:
        return "up" if await _request_runner(project, conversation_id, "GET", "/health") == 200 else "hung"
    except TimeoutError:
        return "hung"
    except OSError:
        return "down"


async def _wait_until_up(project: Project, conversation_id: int, deadline: float) -> None:
    """Recursive: poll the health check every 0.2s until it answers, or raise RunnerUnavailable at the
    deadline (the runner's log says why)."""
    if await is_session_up(project, conversation_id):
        return
    if time.monotonic() > deadline:
        raise RunnerUnavailable(
            f"conversation {conversation_id}'s runner didn't come up within {AGENT2_START_WAIT_SECONDS}s "
            + f"(see {get_log_path(project, conversation_id)})"
        )
    await asyncio.sleep(0.2)
    await _wait_until_up(project, conversation_id, deadline)


async def _start_runner(project: Project, conversation_id: int) -> None:
    """Start the conversation's runner under its container's pm2, in its workspace, and return without
    waiting for it. Its env (to_runner_env) goes by NAME to docker, and from pm2's start into the runner.
    Without a model key there's nothing for it to run on, so that's refused up front."""
    if not settings.anthropic_api_key and not settings.anthropic_auth_token:
        raise RunnerUnavailable("neither ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN is set: agents have no model")
    try:
        token = tokens.to_session_token(conversation_id)
    except tokens.SessionTokenSecretMissing as error:
        raise RunnerUnavailable(str(error)) from error
    project_dir = devenv.get_project_dir(project)
    log_dir_fd = await asyncio.to_thread(paths.open_dir_within, project_dir, f"{DEVENV_STATE_DIR}/logs", True)
    os.close(log_dir_fd)
    log_path = devenv.to_container_path(project, get_log_path(project, conversation_id))
    workspace = devenv.to_container_path(project, devenv.get_conversation_dir(project, conversation_id))
    pm2_start = [
        "pm2", "start", f"{RUNNER_INSTALL_PATH}/.venv/bin/python",
        "--name", get_process_name(conversation_id),
        "--cwd", workspace,
        "--interpreter", "none",
        "--output", log_path, "--error", log_path, "--time",
        # Restarted when it exits (Claude Code went away), but backing off: a runner that can't start (or
        # finds another one live on its socket) mustn't spin.
        "--exp-backoff-restart-delay", "1000",
        "--", "-m", "session_runner",
    ]  # fmt: skip
    try:
        await _delete_runner(project["id"], conversation_id)  # a stopped or errored leftover keeps its old env
        await devenv.run_in_container(project["id"], pm2_start, env=to_runner_env(conversation_id, token))
    except CommandFailed as error:
        raise RunnerUnavailable(f"conversation {conversation_id}'s runner couldn't be started: {error}") from error


def to_runner_env(conversation_id: int, token: str) -> dict[str, str]:
    """[Pure-ish] Everything a runner (and the Claude Code it starts, which inherits it) is told:
    - where to listen (the socket, as the container sees it), where to call back, its session token
    - its model: the backend's model proxy, with the session token as the "key" -- the real key never
      leaves the backend -- and another endpoint's own model names when it proxies to one
    - no auto-updates and no bytecode writes: the runner and Claude Code are read-only for the agent's user
    """
    backend_url = get_backend_url()
    return {
        AGENT2_SOCKET_ENV: f"{DEVENV_SOCKETS_PATH}/{conversation_id}.sock",
        SESSION_OUTBOX_URL_ENV: backend_url,
        SESSION_TOKEN_ENV: token,
        "ANTHROPIC_BASE_URL": f"{backend_url}/api/agent/model",
        "ANTHROPIC_AUTH_TOKEN": token,
        **(AGENT2_ENDPOINT_ENV if settings.anthropic_base_url else {}),
        "DISABLE_AUTOUPDATER": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


async def _delete_runner(project_id: int, conversation_id: int) -> bool:
    """`pm2 delete` the conversation's runner, if pm2 has one. Returns whether it did."""
    try:
        await devenv.run_in_container(project_id, ["pm2", "delete", get_process_name(conversation_id)])
    except CommandFailed:
        return False  # pm2 doesn't know it: nothing to delete
    return True


async def _request_runner(
    project: Project, conversation_id: int, method: str, path: str, body: dict | None = None
) -> int:
    """One authenticated request to the conversation's runner over its unix socket; returns the status.
    Raises OSError (aiohttp's connection errors are OSErrors) when nothing listens there -- or TimeoutError
    (an OSError too) when a hung runner doesn't answer in time. A session per call: the connector is bound to
    this one socket, and these calls are rare."""
    headers = {"Authorization": f"Bearer {tokens.to_session_token(conversation_id)}"}
    connector = aiohttp.UnixConnector(path=get_socket_path(project, conversation_id))
    timeout = aiohttp.ClientTimeout(total=AGENT2_REQUEST_TIMEOUT_SECONDS)
    async with (
        aiohttp.ClientSession(connector=connector, timeout=timeout) as client,
        client.request(method, f"http://runner{path}", json=body, headers=headers) as response,
    ):
        return response.status
