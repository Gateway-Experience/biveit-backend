# Intentions: agent2's sessions as the backend sees them -- each conversation's runner (src/runners/agent2)
# started when needed, and talked to over its unix socket. The runtime-specific half only: what to say to
# the agent (introductions, translating stored messages into the channel's format) stays where it is.
#
# Nothing is recorded anywhere. A conversation's runner lives at a socket path derived from its workspace,
# and "is it running?" is just "does it answer there?" -- so a crashed runner simply stops answering, and
# the next ensure_session starts a fresh one, which continues the workspace's Claude session. Two racing
# starts are fine too: only one runner gets the socket (runners/agent2/main.claim_socket).
#
# Runners must outlive the backend (a deploy shouldn't cut every conversation), so where systemd is around
# each one is its own transient unit of the user's manager (utils/systemd): out of the backend service's
# cgroup, so its restart doesn't take them down. That's also a hard kill switch (stop_session) for a runner
# that stopped answering. Without systemd (macOS) they're plain detached processes -- fine for local dev.
# References: src/runners/agent2/main.py (the other end), services/agent/adapter.py (the tmux runtime).

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

from src.config import settings
from src.constants import (
    AGENT2_ENDPOINT_ENV,
    AGENT2_REQUEST_TIMEOUT_SECONDS,
    AGENT2_SOCKET_ENV,
    AGENT2_START_WAIT_SECONDS,
    SESSION_OUTBOX_URL_ENV,
    SESSION_SEND_ATTEMPTS,
    SESSION_TOKEN_ENV,
)
from src.dto.projects import Project
from src.services import devenv
from src.services.agent import tokens
from src.utils import systemd
from src.utils.common import exponential_backoff, with_retries
from src.utils.processes import CommandFailed, get_base_env

# This repo, for the runner's PYTHONPATH -- it runs our code with the backend's own interpreter.
_REPO_DIR = str(Path(__file__).resolve().parents[3])


class RunnerUnavailable(Exception):
    """The conversation's runner couldn't be started, didn't come up in time, or refused a message. 503."""


def get_socket_dir() -> str:
    """[Pure-ish] Where every runner's socket lives: one short folder next to the storage root. Not in the
    project folders, because unix socket paths are capped at ~108 bytes and those can run deep; and not in
    /tmp, whose periodic cleanup could delete a long-idle runner's socket from under it."""
    return os.path.join(os.path.dirname(os.path.realpath(settings.storage_dir)), "agent2_sockets")


def get_socket_path(project: Project, conversation_id: int) -> str:
    """[Pure-ish] Where the conversation's runner listens. Only the conversation decides it; `project` is
    taken anyway so every runner path helper has the same shape."""
    return os.path.join(get_socket_dir(), f"{conversation_id}.sock")


def get_unit_name(conversation_id: int) -> str:
    """[Pure-ish] The runner's systemd unit. The env is in it because every backend on the machine (local,
    dev...) shares the user's manager, and conversation ids only mean something within one database."""
    return f"agent2-{settings.env}-conversation-{conversation_id}"


def get_log_path(project: Project, conversation_id: int) -> str:
    """[Pure-ish] The runner's stdout/stderr, next to its socket."""
    return os.path.join(devenv.get_project_dir(project), f"{conversation_id}.agent.log")


async def ensure_session(project: Project, conversation_id: int) -> None:
    """Make sure the conversation's runner is up: if it doesn't answer, seed the workspace, start one and
    wait for it to answer. Idempotent -- safe on every visit and before every send. Raises RunnerUnavailable."""
    if await is_session_up(project, conversation_id):
        return
    await devenv.ensure_workspace(project, conversation_id)
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
    """Ask the conversation's runner to end (it finishes up and removes its socket); one that doesn't answer
    but whose unit is still there (hung) gets its unit stopped instead. Idempotent; returns whether there was
    one to stop. Its Claude session stays on disk, so the next ensure_session continues it."""
    try:
        return await _request_runner(project, conversation_id, "POST", "/shutdown") == 202
    except OSError:
        if not systemd.is_available() or not await systemd.is_active(get_unit_name(conversation_id)):
            return False
        await systemd.stop(get_unit_name(conversation_id))
        return True


async def is_session_up(project: Project, conversation_id: int) -> bool:
    """Whether the conversation's runner is up: it answers its health check. A socket file nobody listens on
    (a runner that died) counts as down."""
    try:
        return await _request_runner(project, conversation_id, "GET", "/health") == 200
    except OSError:
        return False


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
    """Start the conversation's runner, detached, in its workspace, and return without waiting for it. Its
    env is only what it needs: our base env, where to listen, where to call back, its session token -- and
    what it bills to (to_billing_env). Outside local, having nothing to bill to is refused (RunnerUnavailable)
    rather than letting Claude Code fall back to the machine's claude.ai login (see settings.anthropic_api_key)."""
    if not settings.anthropic_api_key and not settings.anthropic_auth_token and settings.env != "local":
        raise RunnerUnavailable(
            "neither ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN is set: agent2 runners must bill to an API key"
        )
    try:
        token = tokens.to_session_token(conversation_id)
    except tokens.SessionTokenSecretMissing as error:
        raise RunnerUnavailable(str(error)) from error
    env = {
        **get_base_env(),
        "PYTHONPATH": _REPO_DIR,
        AGENT2_SOCKET_ENV: get_socket_path(project, conversation_id),
        SESSION_OUTBOX_URL_ENV: f"http://127.0.0.1:{settings.api_port}",
        SESSION_TOKEN_ENV: token,
        **to_billing_env(settings.anthropic_api_key, settings.anthropic_base_url, settings.anthropic_auth_token),
    }
    # Owner-only: the token guards each request too, but nobody else needs to even see the sockets.
    await asyncio.to_thread(os.makedirs, get_socket_dir(), 0o700, True)
    argv = [sys.executable, "-m", "src.runners.agent2.main"]
    cwd = devenv.get_conversation_dir(project, conversation_id)
    log_path = get_log_path(project, conversation_id)
    if not systemd.is_available():
        await _start_detached(argv, cwd, env, log_path)
        return
    unit = get_unit_name(conversation_id)
    try:
        await systemd.start_service(unit, argv, cwd, env, f"{get_socket_path(project, conversation_id)}.env", log_path)
    except CommandFailed as error:
        # A racing start got the unit first -- fine, ensure_session waits for that one to answer. Anything
        # else (no user manager: see utils/systemd) is a real failure.
        if not await systemd.is_active(unit):
            raise RunnerUnavailable(f"conversation {conversation_id}'s runner couldn't be started: {error}") from error


async def _start_detached(argv: list[str], cwd: str, env: dict[str, str], log_path: str) -> None:
    """Start the runner as a plain detached process, where there's no systemd. It outlives us, except a
    service manager's restart that kills our whole process tree."""
    # The child keeps its own copy of the log's file descriptor, so ours can close right away.
    with await asyncio.to_thread(open, log_path, "ab") as log:
        # Plain Popen, NOT asyncio's subprocess: asyncio kills a child it still tracks when its event loop
        # closes, so every runner would die with whatever process started it (a backend restart, a script).
        # The Popen object is dropped right away; the subprocess module reaps the runner once it exits.
        # start_new_session: its own process group, so signals aimed at ours don't reach it either.
        await asyncio.to_thread(
            subprocess.Popen,
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def to_billing_env(api_key: str, base_url: str, auth_token: str) -> dict[str, str]:
    """[Pure] The env vars telling a runner's Claude Code what to bill to: Anthropic's API key, and/or another
    Anthropic-compatible endpoint with its key -- which also brings that endpoint's models (AGENT2_ENDPOINT_ENV).
    Unset ones are left out, so Claude Code's own defaults apply."""
    return {
        **({"ANTHROPIC_API_KEY": api_key} if api_key else {}),
        **({"ANTHROPIC_BASE_URL": base_url, **AGENT2_ENDPOINT_ENV} if base_url else {}),
        **({"ANTHROPIC_AUTH_TOKEN": auth_token} if auth_token else {}),
    }


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
