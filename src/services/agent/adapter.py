# Intentions: the bridge between a project and its agent session -- a long-running process working
# inside the project's own directory (the workspace). ensure_workspace is the entry point: it makes
# sure the workspace exists (seeded from the template) and that exactly one session is running in it,
# and hands back the session's port.
#
# THERE'S NO SESSION TABLE on purpose. The OS is the source of truth: a session is "the process whose
# cwd is the workspace and whose env has EVENT_CHANNEL_PORT=<port>". So nothing can drift out of sync
# (a crashed session simply stops being found, and the next call starts a fresh one). The cost: the
# process scan reads /proc, so this is Linux-only.

import asyncio
import os
import re
import shutil
import socket
from pathlib import Path

import pydash

from src.config import settings
from src.constants import (
    SESSION_COMMAND,
    SESSION_LOCK_TTL_SECONDS,
    SESSION_LOCK_WAIT_SECONDS,
    SESSION_PORT_ENV,
    SESSION_PORT_LOCK_TTL_SECONDS,
    SESSION_PORT_MAX_ATTEMPTS,
)
from src.dto.projects import Project
from src.services.agent import storage
from src.utils import redis
from src.utils.common import with_retries

# Matched against each `NAME=value` entry of a process's environment; group 1 is the port.
_SESSION_PORT_PATTERN = re.compile(rf"^{SESSION_PORT_ENV}=(\d+)$")


class TooManySessions(Exception):
    """More than one session is running in a project's workspace. Shouldn't happen (ensure_session
    is serialized per project), so it's a real bug -- or someone started one by hand. (A session's own
    child processes don't count -- see ensure_session.)"""


class _PortTaken(Exception):
    """Internal retry signal: the port we picked is already reserved by another session start."""


async def ensure_workspace(project: Project) -> int:
    """Make sure the project's workspace exists and is seeded, and that its session is running. Returns
    the session's port. Safe to call on every visit -- each step is idempotent."""
    pwd = storage.get_project_dir(project)
    await asyncio.to_thread(_copy_missing_files, os.path.realpath(settings.template_dir), pwd)
    return await ensure_session(project)


def _copy_missing_files(source_dir: str, destination_dir: str) -> None:
    """Copy every file under source_dir into destination_dir unless it already exists there, creating
    directories as needed. Existing files are the project's own (likely edited) versions, so they're
    never overwritten. A template file added later still reaches older workspaces on their next visit."""
    shutil.copytree(
        source_dir,
        destination_dir,
        dirs_exist_ok=True,
        copy_function=lambda src, dst: None if os.path.exists(dst) else shutil.copy2(src, dst),
    )


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


async def ensure_session(project: Project) -> int:
    """The port of the project's running session, starting one if none is running. Serialized per
    project, so two tabs opening the same project at once can't both start a session.

    The port comes back as soon as the process is launched. Whether it's LISTENING yet is the caller's
    problem: connect with a retry."""
    async with redis.lock(f"agent_session:{project['id']}", SESSION_LOCK_TTL_SECONDS, SESSION_LOCK_WAIT_SECONDS):
        pwd = storage.get_project_dir(project)
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
        await start_session(project, port)
        return port


async def _claim_free_port() -> int:
    """Pick a port the OS says is free and reserve it in redis for a few minutes. The OS check alone
    races: the port stays "free" until the session actually binds it, so two session starts at once
    could pick the same one. Raises _PortTaken if the reservation is already held, and the caller retries."""
    with socket.socket() as probe:
        probe.bind(("", 0))
        port = probe.getsockname()[1]
    if not await redis.set_if_absent(f"agent_session_port:{port}", SESSION_PORT_LOCK_TTL_SECONDS):
        raise _PortTaken(port)
    return port


async def start_session(project: Project, port: int) -> None:
    """Launch the project's session process (SESSION_COMMAND) and return once it's running -- not once
    it's listening.

    How it's launched is a CONTRACT with ensure_session, which finds the session again by these:
      - cwd = the project's workspace dir
      - SESSION_PORT_ENV=<port> in its environment, on top of ours
    It's also detached (its own session, no stdin, never awaited), so it outlives a backend restart
    instead of dying with it.

    Its output goes to `<storage_dir>/<user_id>/<project_id>.session.log`, next to the workspace rather than inside it,
    so the agent never sees its own log among the project's files. Appended, so it covers every session
    the project has had."""
    pwd = storage.get_project_dir(project)
    with await asyncio.to_thread(open, f"{pwd}.session.log", "ab") as session_log:
        # The child gets its own copy of the log's fd, so closing ours when the block ends is fine.
        await asyncio.create_subprocess_exec(
            *SESSION_COMMAND,
            cwd=pwd,
            env={**os.environ, SESSION_PORT_ENV: str(port)},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=session_log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
