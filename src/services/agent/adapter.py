# Intentions: the bridge between a conversation and its agent session -- a long-running process working
# inside the conversation's workspace (services/devenv). ensure_session is the entry point: it makes sure
# exactly one session is running there, and hands back its port.
#
# THERE'S NO SESSION TABLE on purpose. The OS is the source of truth: a session is "the process running
# SESSION_COMMAND, whose cwd is the workspace and whose env has COMMUNICATION_PORT=<port>". (Everything it
# launches -- MCP servers, dev servers, a browser -- inherits that cwd and env too, but runs some other
# command, so only the session itself matches.) So nothing can drift out of sync
# (a crashed session simply stops being found, and the next call starts a fresh one). The cost: the
# process scan reads /proc, so this is Linux-only.

import asyncio
import json
import os
import random
import re
import shlex
import signal
import socket
import sys
import time
from pathlib import Path

import pydash

from src.config import settings
from src.constants import (
    CLAUDE_PROJECTS_DIR,
    COMMUNICATION_MCP_NAME,
    SESSION_COMMAND,
    SESSION_CONTINUE_ARGS,
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
    SESSION_STOP_WAIT_SECONDS,
    SESSION_TRUST_PROMPT,
)
from src.dto.projects import Project
from src.services import devenv
from src.services.agent import tokens
from src.utils import redis
from src.utils.common import with_retries
from src.utils.processes import get_base_env

# This repo's root (src/services/agent/adapter.py is 3 levels in) -- where the session's MCP is imported from.
_REPO_DIR = str(Path(__file__).resolve().parents[3])
# Matched against each `NAME=value` entry of a process's environment; group 1 is the port.
_SESSION_PORT_PATTERN = re.compile(rf"^{SESSION_PORT_ENV}=(\d+)$")


class TooManySessions(Exception):
    """More than one session is running in a project's workspace. Shouldn't happen (ensure_session
    is serialized per workspace), so it's a real bug -- or someone started one by hand. (What a session
    launches doesn't count -- see find_session_processes.)"""


class SessionStartFailed(Exception):
    """The session couldn't be brought up: tmux refused, or it never got to (or past) its startup prompt."""


class _PortTaken(Exception):
    """Internal retry signal: the port we picked is in use, or already reserved by another session start."""


async def get_all_processes(abs_dir: str) -> list[dict]:
    """Every process whose working directory is abs_dir, as {pid, cmdline, environ} (cmdline/environ are
    lists of strings). Both paths go through realpath, so trailing slashes and symlinks don't matter.
    Processes we can't read (other users') or that exit mid-scan are skipped."""
    target_cwd = os.path.realpath(abs_dir)
    pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    # /proc reads are blocking, and there's one round of them per process on the box.
    return await asyncio.to_thread(lambda: pydash.compact([_read_process_in_dir(pid, target_cwd) for pid in pids]))


def _read_process_in_dir(pid: str, target_cwd: str) -> dict | None:
    """[Pure-ish] One /proc entry as {pid, cmdline, environ} if its cwd is target_cwd, else None.
    The cwd check comes first so we only read the env of processes we actually care about."""
    proc_dir = Path("/proc", pid)
    try:
        if os.readlink(proc_dir / "cwd") != target_cwd:
            return None
        return {
            "pid": int(pid),
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


async def find_session_processes(abs_dir: str) -> list[dict]:
    """The session processes in abs_dir: running SESSION_COMMAND (with or without SESSION_CONTINUE_ARGS),
    with the session port in their env. Normally zero or one. What a session launches inherits its cwd
    and env, so it passes find_processes_in_dir too -- the command line is what tells them apart."""
    processes = await find_processes_in_dir(abs_dir, _SESSION_PORT_PATTERN)
    return [process for process in processes if process["cmdline"][: len(SESSION_COMMAND)] == SESSION_COMMAND]


async def ensure_session(project: Project, conversation_id: int) -> int:
    """The port of the conversation's running session, starting one if none is running. Serialized per
    workspace, so two tabs opening the same conversation at once can't both start a session.

    The port comes back as soon as the process is launched. Whether it's LISTENING yet is the caller's
    problem: connect with a retry."""
    async with redis.lock(f"agent_session:{conversation_id}", SESSION_LOCK_TTL_SECONDS, SESSION_LOCK_WAIT_SECONDS):
        pwd = devenv.get_conversation_dir(project, conversation_id)
        sessions = await find_session_processes(pwd)
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


async def stop_session(project: Project, conversation_id: int) -> dict:
    """Turn the conversation's session off: its tmux session, and every process still running as part of
    it -- Claude, its communication MCP (which holds the channel port), and anything they started in the
    workspace. Idempotent: with nothing running it does nothing. Returns what it found, as
    {tmux_session: whether there was one, pids: the session processes it stopped}.

    The processes are the ones ensure_session would find (cwd = the workspace, the port in their env),
    so afterwards it's guaranteed to find none and start fresh -- even if something outlived the tmux
    session (an MCP server whose parent died, say). They get SIGTERM, then SIGKILL after
    SESSION_STOP_WAIT_SECONDS. Serialized with ensure_session, so a stop can't race a start."""
    async with redis.lock(f"agent_session:{conversation_id}", SESSION_LOCK_TTL_SECONDS, SESSION_LOCK_WAIT_SECONDS):
        tmux_session = _get_tmux_session_name(conversation_id)
        try:
            await _run_tmux("kill-session", "-t", tmux_session)
            had_tmux_session = True
        except SessionStartFailed:
            had_tmux_session = False  # no such session, or no tmux server at all
        pwd = devenv.get_conversation_dir(project, conversation_id)
        pids = pydash.map_(await find_processes_in_dir(pwd, _SESSION_PORT_PATTERN), "pid")
        _signal_all(pids, signal.SIGTERM)
        survivors = await _wait_for_exit(pids, time.monotonic() + SESSION_STOP_WAIT_SECONDS)
        _signal_all(survivors, signal.SIGKILL)
        return {"tmux_session": had_tmux_session, "pids": pids}


def _signal_all(pids: list[int], signal_number: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal_number)
        except ProcessLookupError:
            pass  # already gone


async def _wait_for_exit(pids: list[int], deadline: float) -> list[int]:
    """Recursive: the pids still alive, checked every 0.1s until none are or the deadline passes."""
    alive = [pid for pid in pids if os.path.exists(f"/proc/{pid}")]
    if not alive or time.monotonic() >= deadline:
        return alive
    await asyncio.sleep(0.1)
    return await _wait_for_exit(alive, deadline)


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


def get_latest_claude_transcript(workspace_dir: str) -> Path | None:
    """[Pure-ish] The newest Claude conversation transcript for this working directory (see
    CLAUDE_PROJECTS_DIR), or None if Claude has never had one there. Its file name is the session id."""
    transcripts_dir = Path(CLAUDE_PROJECTS_DIR).expanduser() / re.sub(r"[^a-zA-Z0-9]", "-", workspace_dir)
    transcripts = list(transcripts_dir.glob("*.jsonl")) if transcripts_dir.is_dir() else []
    return max(transcripts, key=lambda path: path.stat().st_mtime, default=None)


async def start_session(project: Project, conversation_id: int, port: int) -> None:
    """Launch the conversation's session (SESSION_COMMAND) in a detached tmux session, get it past its
    startup prompt, and return -- once it's running, not once it's listening.

    How it's launched is a CONTRACT with ensure_session, which finds the session again by these:
      - cwd = the conversation's workspace dir
      - SESSION_PORT_ENV=<port> in its environment, on top of ours
    Living in tmux, it outlives a backend restart, and you can watch it: `tmux attach -t conv_<id>`.
    If Claude already had a conversation in the workspace, the session continues it (SESSION_CONTINUE_ARGS)
    rather than starting from scratch. Its MCP config is (re)written first (write_mcp_config).

    The tmux client's own output (its errors, e.g. a duplicate session name) goes to
    `<project dir>/<conversation_id>.session.log`, next to the workspace rather than inside it; the
    session's output is in its tmux pane. Raises SessionStartFailed if tmux refuses, or the startup
    prompt never shows."""
    pwd = devenv.get_conversation_dir(project, conversation_id)
    tmux_session = _get_tmux_session_name(conversation_id)
    await asyncio.to_thread(write_mcp_config, pwd)
    with await asyncio.to_thread(open, f"{pwd}.session.log", "ab") as session_log:
        # exec takes argv, not a shell line, so every argument is its own string; tmux itself takes the
        # session command as ONE string, hence shlex.join.
        # The port goes ONLY to the pane, via -e: a tmux pane doesn't get the client's environment, and the
        # client's env must not carry it either -- the first `tmux new` starts the tmux SERVER with that
        # env and cwd, and a server carrying the port would match ensure_session's discovery as an extra
        # session. -c sets the pane's cwd, which is what discovery matches on. The outbox URL and token are
        # how the session's channel posts the agent's messages back to us -- passed explicitly too, since
        # the pane would otherwise only have whatever env the tmux server happened to start with.
        # The token only speaks for this conversation (services/agent/tokens).
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
            f"{SESSION_OUTBOX_TOKEN_ENV}={tokens.to_session_token(conversation_id)}",
            shlex.join([*SESSION_COMMAND, *(SESSION_CONTINUE_ARGS if get_latest_claude_transcript(pwd) else [])]),
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


def write_mcp_config(workspace_dir: str) -> None:
    """Write the workspace's .mcp.json: the communication MCP (src/mcps/communication), run by the very
    interpreter running this backend, with this repo on its PYTHONPATH so it can import our code. Ours,
    not the template's: it's rewritten before every session start, so a workspace always points at
    wherever the platform lives now (a moved repo, a renamed server) instead of a stale copy. (Claude
    only uses it without asking because the template's .claude/settings.local.json enables project MCPs.)"""
    mcp_config = {
        "mcpServers": {
            COMMUNICATION_MCP_NAME: {
                "command": sys.executable,
                "args": ["-m", f"src.mcps.{COMMUNICATION_MCP_NAME}.main"],
                "env": {"PYTHONPATH": _REPO_DIR},
            }
        }
    }
    with open(os.path.join(workspace_dir, ".mcp.json"), "w", encoding="utf-8") as config_file:
        json.dump(mcp_config, config_file, indent=2)


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
    """[Pure-ish] The tmux client's environment: only the base env (utils/processes), so neither our
    secrets nor the session port reach it -- see start_session for why the port must never be there.
    The first `tmux new` starts the tmux SERVER with this env, and every pane inherits it."""
    return pydash.omit(get_base_env(), SESSION_PORT_ENV)
