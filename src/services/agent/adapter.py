# Intentions: the bridge between a conversation and its agent session -- a long-running process working
# inside the conversation's workspace (services/devenv). ensure_session is the entry point: it makes sure
# exactly one session is running there, and hands back its port.
#
# THERE'S NO SESSION TABLE on purpose. The OS is the source of truth: a session is "the process whose
# cwd is the workspace and whose env has EVENT_CHANNEL_PORT=<port>". So nothing can drift out of sync
# (a crashed session simply stops being found, and the next call starts a fresh one). The cost: the
# process scan reads /proc, so this is Linux-only.

import asyncio
import os
import random
import re
import shlex
import socket
import time
from pathlib import Path

import pydash

from src.config import settings
from src.constants import (
    SESSION_COMMAND,
    SESSION_LOCK_TTL_SECONDS,
    SESSION_LOCK_WAIT_SECONDS,
    SESSION_OUTBOX_TOKEN_ENV,
    SESSION_OUTBOX_URL_ENV,
    SESSION_PORT_ENV,
    SESSION_PORT_LOCK_TTL_SECONDS,
    SESSION_PORT_MAX_ATTEMPTS,
    SESSION_PORT_RANGE,
    SESSION_STARTUP_PROMPT,
    SESSION_STARTUP_PROMPT_WAIT_SECONDS,
    SESSION_TRUST_PROMPT,
)
from src.dto.projects import Project
from src.services import devenv
from src.utils import redis
from src.utils.common import with_retries

# Matched against each `NAME=value` entry of a process's environment; group 1 is the port.
_SESSION_PORT_PATTERN = re.compile(rf"^{SESSION_PORT_ENV}=(\d+)$")


class TooManySessions(Exception):
    """More than one session is running in a project's workspace. Shouldn't happen (ensure_session
    is serialized per workspace), so it's a real bug -- or someone started one by hand. (A session's own
    child processes don't count -- see ensure_session.)"""


class SessionStartFailed(Exception):
    """The session couldn't be brought up: tmux refused, or it never got to (or past) its startup prompt."""


class _PortTaken(Exception):
    """Internal retry signal: the port we picked is in use, or already reserved by another session start."""


async def get_all_processes(abs_dir: str) -> list[dict]:
    """Every process whose working directory is abs_dir, as {pid, sid, cmdline, environ} (sid = the OS
    session id; cmdline/environ are lists of strings). Both paths go through realpath, so trailing slashes and symlinks don't matter.
    Processes we can't read (other users') or that exit mid-scan are skipped."""
    target_cwd = os.path.realpath(abs_dir)
    pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    # /proc reads are blocking, and there's one round of them per process on the box.
    return await asyncio.to_thread(lambda: pydash.compact([_read_process_in_dir(pid, target_cwd) for pid in pids]))


def _read_process_in_dir(pid: str, target_cwd: str) -> dict | None:
    """[Pure-ish] One /proc entry as {pid, sid, cmdline, environ} if its cwd is target_cwd, else None.
    The cwd check comes first so we only read the env of processes we actually care about."""
    proc_dir = Path("/proc", pid)
    try:
        if os.readlink(proc_dir / "cwd") != target_cwd:
            return None
        return {
            "pid": int(pid),
            # /proc/<pid>/stat is "pid (comm) state ppid pgrp session ...". comm can contain spaces and
            # parens, so split after its LAST ')' -- session is then the 4th field.
            "sid": int((proc_dir / "stat").read_text().rpartition(")")[2].split()[3]),
            "cmdline": _read_nul_separated(proc_dir / "cmdline"),
            "environ": _read_nul_separated(proc_dir / "environ"),
        }
    except (FileNotFoundError, PermissionError):
        return None


def _read_nul_separated(path: Path) -> list[str]:
    return pydash.compact(path.read_bytes().decode(errors="replace").split("\0"))


async def find_processes_in_dir(abs_dir: str, regex: re.Pattern) -> list[dict]:
    """Processes running in abs_dir that have an env entry (`NAME=value`) matching `regex`."""
    dir_processes = await get_all_processes(abs_dir)
    return [process for process in dir_processes if any(regex.search(entry) for entry in process["environ"])]


async def ensure_session(project: Project, conversation_id: int) -> int:
    """The port of the conversation's running session, starting one if none is running. Serialized per
    workspace, so two tabs opening the same conversation at once can't both start a session.

    The port comes back as soon as the process is launched. Whether it's LISTENING yet is the caller's
    problem: connect with a retry."""
    async with redis.lock(f"agent_session:{conversation_id}", SESSION_LOCK_TTL_SECONDS, SESSION_LOCK_WAIT_SECONDS):
        pwd = devenv.get_conversation_dir(project, conversation_id)
        # A session's children (a dev server, say) inherit its cwd AND its env, so they match too. They
        # all share the session's OS session id though (start_session gives it its own), so count those.
        processes = await find_processes_in_dir(pwd, _SESSION_PORT_PATTERN)
        sessions = pydash.uniq_by(processes, "sid")
        if len(sessions) > 1:
            raise TooManySessions(f"{len(sessions)} sessions in {pwd}", {"pids": pydash.map_(sessions, "pid")})
        if sessions:
            return next(
                int(match.group(1)) for entry in sessions[0]["environ"] if (match := _SESSION_PORT_PATTERN.match(entry))
            )
        port = await with_retries(
            _claim_free_port,
            retries=SESSION_PORT_MAX_ATTEMPTS,
            should_retry=lambda error, _: isinstance(error, _PortTaken),
        )
        await start_session(project, conversation_id, port)
        return port


async def _claim_free_port() -> int:
    """Pick a random port from SESSION_PORT_RANGE that's free right now, and reserve it in redis for a few
    minutes. Free means we can bind it ourselves. That alone races though: the port stays "free" until the
    session actually binds it, so two session starts at once could pick the same one -- hence the
    reservation. Raises _PortTaken if either check fails, and the caller retries with another pick."""
    port = random.randint(*SESSION_PORT_RANGE)
    try:
        with socket.socket() as probe:
            probe.bind(("", port))
    except OSError as error:
        raise _PortTaken(port) from error
    if not await redis.set_if_absent(f"agent_session_port:{port}", SESSION_PORT_LOCK_TTL_SECONDS):
        raise _PortTaken(port)
    return port


async def start_session(project: Project, conversation_id: int, port: int) -> None:
    """Launch the conversation's session (SESSION_COMMAND) in a detached tmux session, get it past its
    startup prompt, and return -- once it's running, not once it's listening.

    How it's launched is a CONTRACT with ensure_session, which finds the session again by these:
      - cwd = the conversation's workspace dir
      - SESSION_PORT_ENV=<port> in its environment, on top of ours
    Living in tmux, it outlives a backend restart, and you can watch it: `tmux attach -t conv_<id>`.

    The tmux client's own output (its errors, e.g. a duplicate session name) goes to
    `<project dir>/<conversation_id>.session.log`, next to the workspace rather than inside it; the
    session's output is in its tmux pane. Raises SessionStartFailed if tmux refuses, or the startup
    prompt never shows."""
    pwd = devenv.get_conversation_dir(project, conversation_id)
    tmux_session = _get_tmux_session_name(conversation_id)
    with await asyncio.to_thread(open, f"{pwd}.session.log", "ab") as session_log:
        # exec takes argv, not a shell line, so every argument is its own string; tmux itself takes the
        # session command as ONE string, hence shlex.join.
        # The port goes ONLY to the pane, via -e: a tmux pane doesn't get the client's environment, and the
        # client's env must not carry it either -- the first `tmux new` starts the tmux SERVER with that
        # env and cwd, and a server carrying the port would match ensure_session's discovery as an extra
        # session. -c sets the pane's cwd, which is what discovery matches on. The outbox URL and token are
        # how the session's channel posts the agent's messages back to us -- passed explicitly too, since
        # the pane would otherwise only have whatever env the tmux server happened to start with.
        # Gotcha: the token is the admin token, so the agent holds full admin rights.
        tmux_client = await asyncio.create_subprocess_exec(
            "tmux",
            "new-session",
            "-d",
            "-s",
            tmux_session,
            "-c",
            pwd,
            "-e",
            f"{SESSION_PORT_ENV}={port}",
            "-e",
            f"{SESSION_OUTBOX_URL_ENV}=http://127.0.0.1:{settings.api_port}",
            "-e",
            f"{SESSION_OUTBOX_TOKEN_ENV}={settings.admin_token}",
            shlex.join(SESSION_COMMAND),
            cwd=pwd,
            env=_get_tmux_client_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=session_log,
            stderr=asyncio.subprocess.STDOUT,
        )
        # With -d the client exits as soon as the session exists. Waiting for it means the session is
        # there before we look at its pane, and a refusal (e.g. "duplicate session") doesn't go unnoticed.
        if await tmux_client.wait() != 0:
            raise SessionStartFailed(f"tmux couldn't start {tmux_session}; see {pwd}.session.log")
    await _accept_startup_prompt(tmux_session)


async def _accept_startup_prompt(tmux_session: str) -> None:
    """Answer Claude's startup screens in the session's terminal (see SESSION_TRUST_PROMPT /
    SESSION_STARTUP_PROMPT): the folder-trust dialog if it shows, then the development-channels
    confirmation. Raises SessionStartFailed if the confirmation hasn't shown within
    SESSION_STARTUP_PROMPT_WAIT_SECONDS, or the pane is gone (the session died during startup)."""
    await _answer_startup_screens(tmux_session, time.monotonic() + SESSION_STARTUP_PROMPT_WAIT_SECONDS)


async def _answer_startup_screens(tmux_session: str, deadline: float, trust_answered: bool = False) -> None:
    """Check the terminal once a second until the development-channels confirmation shows, answering
    the trust dialog on the way. Recursive: each call is one look at the screen.

    `trust_answered`: whether we've already answered the trust dialog. It's answered at most ONCE -- the
    screen can still show it for a moment after (or, in a plain terminal, keep it scrolled up), and
    answering again would send Down+Enter into the NEXT prompt, which selects "Exit"."""
    pane_text = await _run_tmux("capture-pane", "-p", "-t", tmux_session)
    if SESSION_STARTUP_PROMPT in pane_text:
        await _run_tmux("send-keys", "-t", tmux_session, "Enter")
        return
    if SESSION_TRUST_PROMPT in pane_text and not trust_answered:
        # Separate keystrokes with a beat between: the dialog should see the selection move before Enter.
        await _run_tmux("send-keys", "-t", tmux_session, "Down")
        await asyncio.sleep(0.5)
        await _run_tmux("send-keys", "-t", tmux_session, "Enter")
        return await _answer_startup_screens(tmux_session, deadline, trust_answered=True)
    if time.monotonic() >= deadline:
        raise SessionStartFailed(f"{tmux_session} never showed its startup prompt")
    await asyncio.sleep(1)
    return await _answer_startup_screens(tmux_session, deadline, trust_answered)


async def _run_tmux(*args: str) -> str:
    """Run one tmux command against the same tmux server sessions are started on, and return its stdout.
    Raises SessionStartFailed if tmux fails (e.g. the session is gone)."""
    tmux = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        env=_get_tmux_client_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await tmux.communicate()
    if tmux.returncode != 0:
        raise SessionStartFailed(f"tmux {args[0]} failed: {stderr.decode(errors='replace').strip()}")
    return stdout.decode(errors="replace")


def _get_tmux_session_name(conversation_id: int) -> str:
    return f"conv_{conversation_id}"


def _get_tmux_client_env() -> dict[str, str]:
    """[Pure-ish] Our environment minus the session port -- see start_session for why the tmux client
    must never carry it."""
    return pydash.omit(dict(os.environ), SESSION_PORT_ENV)
