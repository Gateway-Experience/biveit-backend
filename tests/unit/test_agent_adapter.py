# Agent adapter tests (services/agent/adapter). Sessions are found by scanning /proc, so these spawn
# REAL throwaway processes (`sleep`) inside temp workspaces. Linux-only, like the code under test.
# Redis is faked: the lock is a no-op and port reservations live in a set.

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from src.config import settings
from src.constants import SESSION_PORT_ENV, SESSION_PORT_RANGE, SESSION_STARTUP_PROMPT, SESSION_TRUST_PROMPT
from src.services import devenv
from src.services.agent import adapter, tokens
from src.utils import redis

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="process discovery reads /proc")
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="sessions run inside tmux")

_PROJECT = {"id": 42, "hash": "p42hash", "user_id": 7, "user_hash": "u7hash"}
_CONVERSATION_ID = 5


@pytest.fixture(autouse=True)
def _session_token_secret(monkeypatch):
    monkeypatch.setattr(settings, "session_token_secret", "session-token-secret-for-tests")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Points STORAGE_DIR at a temp dir and returns the project's (created) workspace path."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    pwd = devenv.get_conversation_dir(_PROJECT, _CONVERSATION_ID)
    os.makedirs(pwd)
    return pwd


@pytest.fixture
def fake_redis(monkeypatch):
    reserved: set[str] = set()

    @asynccontextmanager
    async def fake_lock(key, ttl_seconds, wait_seconds):
        yield

    async def fake_set_if_absent(key, ttl_seconds):
        if key in reserved:
            return False
        reserved.add(key)
        return True

    monkeypatch.setattr(redis, "lock", fake_lock)
    monkeypatch.setattr(redis, "set_if_absent", fake_set_if_absent)
    return reserved


@pytest.fixture
async def spawn_in():
    """spawn_in(cwd, env) -> a `sleep` process running there, in its own OS session like a real one
    (start_session); all are killed at teardown."""
    processes = []

    async def _spawn(cwd, env):
        process = await asyncio.create_subprocess_exec(
            "sleep", "30", cwd=cwd, env={**os.environ, **env}, start_new_session=True
        )
        processes.append(process)
        return process

    yield _spawn
    for process in processes:
        process.kill()
        await process.wait()


# --- process discovery ---


async def test_get_all_processes_matches_cwd_ignoring_trailing_slash(workspace, spawn_in, tmp_path):
    inside = await spawn_in(workspace, {})
    await spawn_in(str(tmp_path), {})  # elsewhere -- must not show up

    processes = await adapter.get_all_processes(workspace + "/")
    assert [process["pid"] for process in processes] == [inside.pid]


async def test_find_processes_in_dir_filters_by_env(workspace, spawn_in):
    session = await spawn_in(workspace, {SESSION_PORT_ENV: "8899"})
    await spawn_in(workspace, {})  # same dir, but not a session

    processes = await adapter.find_processes_in_dir(workspace, adapter._SESSION_PORT_PATTERN)
    assert [process["pid"] for process in processes] == [session.pid]


@pytest.fixture
def isolated_tmux(monkeypatch):
    """Sessions run inside tmux (start_session), so give these tests a PRIVATE tmux server -- never the
    developer's own, where a test session named conv_<id> could collide with a real one. tmux keeps its
    socket under $TMUX_TMPDIR (short path on purpose: unix socket paths are capped at ~108 chars).
    The whole server is killed afterwards, taking every test session with it."""
    socket_dir = tempfile.mkdtemp(prefix="tmx")
    monkeypatch.setenv("TMUX_TMPDIR", socket_dir)
    yield
    subprocess.run(
        ["tmux", "kill-server"], env={**os.environ, "TMUX_TMPDIR": socket_dir}, capture_output=True, check=False
    )
    shutil.rmtree(socket_dir, ignore_errors=True)


def _claude_like(then: str) -> list[str]:
    """A stand-in session command that behaves like Claude at startup: shows the confirmation prompt and
    waits for Enter, then runs `then` (shell). `then` must not `exec`: the shell has to stay the running
    process, since sessions are recognized by their command line (find_session_processes)."""
    return ["sh", "-c", f'echo "{SESSION_STARTUP_PROMPT}"; read confirmation; echo accepted; {then}']


async def _wait_for_sessions(workspace, count, timeout_seconds=5.0):
    """The session processes in `workspace` once there are `count` of them -- a tmux pane takes a moment to
    start. Returns whatever is there at the timeout, so the assertion shows what went wrong."""
    deadline = time.monotonic() + timeout_seconds
    processes = await adapter.find_processes_in_dir(workspace, adapter._SESSION_PORT_PATTERN)
    while len(processes) < count and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        processes = await adapter.find_processes_in_dir(workspace, adapter._SESSION_PORT_PATTERN)
    return processes


# --- ensure_session ---


async def test_ensure_session_reuses_the_running_session(workspace, spawn_in, fake_redis, monkeypatch):
    monkeypatch.setattr(adapter, "SESSION_COMMAND", ["sleep", "30"])  # what spawn_in runs
    await spawn_in(workspace, {SESSION_PORT_ENV: "8899"})

    async def fail_start(project, conversation_id, port):
        raise AssertionError("must not start a second session")

    monkeypatch.setattr(adapter, "start_session", fail_start)
    assert await adapter.ensure_session(_PROJECT, _CONVERSATION_ID) == 8899


@needs_tmux
async def test_ensure_session_ignores_what_the_session_launches(workspace, fake_redis, isolated_tmux, monkeypatch):
    # The session launches things that inherit its cwd and env: a dev server, and a browser that puts
    # itself in its own OS session (setsid -- what Chrome does). Neither runs the session's command.
    monkeypatch.setattr(adapter, "SESSION_COMMAND", _claude_like("sleep 30 & setsid sleep 30 & sleep 30"))
    port = await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)
    processes = await _wait_for_sessions(workspace, 4)
    assert len(processes) == 4  # the session shell, and the three sleeps
    assert len(await adapter.find_session_processes(workspace)) == 1
    assert await adapter.ensure_session(_PROJECT, _CONVERSATION_ID) == port  # one session, not TooManySessions


async def test_ensure_session_refuses_when_several_are_running(workspace, spawn_in, fake_redis, monkeypatch):
    monkeypatch.setattr(adapter, "SESSION_COMMAND", ["sleep", "30"])  # two real sessions, as far as it knows
    await spawn_in(workspace, {SESSION_PORT_ENV: "8899"})
    await spawn_in(workspace, {SESSION_PORT_ENV: "8900"})

    with pytest.raises(adapter.TooManySessions):
        await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)


async def test_ensure_session_starts_one_on_a_reserved_port(workspace, fake_redis, monkeypatch):
    started = {}

    async def fake_start(project, conversation_id, port):
        started["port"] = port

    monkeypatch.setattr(adapter, "start_session", fake_start)

    port = await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)
    assert started["port"] == port
    assert f"agent_session_port:{port}" in fake_redis


async def test_claim_free_port_retries_past_busy_and_reserved_ports(fake_redis, monkeypatch):
    # Picks: one already reserved, one some other process holds, then a free one.
    picks = iter([50001, 50002, 50003])

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def bind(self, address):
            if address[1] == 50002:
                raise OSError("address already in use")

    fake_redis.add("agent_session_port:50001")
    monkeypatch.setattr(adapter.random, "randint", lambda low, high: next(picks))
    monkeypatch.setattr(adapter.socket, "socket", FakeSocket)
    monkeypatch.setattr(adapter, "start_session", lambda project, conversation_id, port: asyncio.sleep(0))
    monkeypatch.setattr(settings, "storage_dir", "/nonexistent-storage")

    assert await adapter.ensure_session(_PROJECT, _CONVERSATION_ID) == 50003


async def test_claim_free_port_stays_in_the_session_range(fake_redis):
    port = await adapter._claim_free_port()
    assert SESSION_PORT_RANGE[0] <= port <= SESSION_PORT_RANGE[1]


# --- start_session ---


@needs_tmux
async def test_started_session_is_found_again(workspace, fake_redis, isolated_tmux, monkeypatch):
    # The launch contract end to end: whatever start_session runs, ensure_session must find it next time --
    # which takes the port reaching the pane (tmux -e) and the pane running in the workspace (tmux -c).
    monkeypatch.setattr(adapter, "SESSION_COMMAND", _claude_like("sleep 30"))

    port = await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)
    await _wait_for_sessions(workspace, 1)
    processes = await adapter.find_session_processes(workspace)
    assert len(processes) == 1
    assert f"{SESSION_PORT_ENV}={port}" in processes[0]["environ"]
    # ...and so does the way back: where the channel posts the agent's messages, and the token it sends.
    assert f"COMMUNICATION_OUTBOX_URL=http://127.0.0.1:{settings.api_port}" in processes[0]["environ"]
    assert f"COMMUNICATION_OUTBOX_TOKEN={tokens.to_session_token(_CONVERSATION_ID)}" in processes[0]["environ"]
    assert await adapter.ensure_session(_PROJECT, _CONVERSATION_ID) == port  # reused, not started again


def test_latest_claude_transcript_is_found_by_the_workspace_path(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "CLAUDE_PROJECTS_DIR", str(tmp_path / "claude-projects"))
    assert adapter.get_latest_claude_transcript("/srv/storage_root/u1/p2/3") is None
    transcripts_dir = tmp_path / "claude-projects" / "-srv-storage-root-u1-p2-3"
    transcripts_dir.mkdir(parents=True)
    (transcripts_dir / "old.jsonl").write_text("{}")
    (transcripts_dir / "new.jsonl").write_text("{}")
    os.utime(transcripts_dir / "old.jsonl", (1000, 1000))
    assert adapter.get_latest_claude_transcript("/srv/storage_root/u1/p2/3").stem == "new"


@needs_tmux
@pytest.mark.parametrize("has_conversation", [False, True])
async def test_start_continues_an_existing_claude_conversation(
    has_conversation, workspace, fake_redis, isolated_tmux, monkeypatch, tmp_path
):
    monkeypatch.setattr(adapter, "CLAUDE_PROJECTS_DIR", str(tmp_path / "claude-projects"))
    if has_conversation:
        transcripts_dir = tmp_path / "claude-projects" / re.sub(r"[^a-zA-Z0-9]", "-", workspace)
        transcripts_dir.mkdir(parents=True)
        (transcripts_dir / "abc.jsonl").write_text("{}")
    # sh -c '<script>' <extra args>: the first extra arg lands in $0, so the stand-in can echo it.
    monkeypatch.setattr(adapter, "SESSION_COMMAND", _claude_like('echo "extra:$0"; sleep 30'))
    await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)
    await asyncio.sleep(0.2)
    pane_text = await adapter._run_tmux("capture-pane", "-p", "-t", f"conv_{_CONVERSATION_ID}")
    assert ("extra:--continue" in pane_text) == has_conversation


@needs_tmux
async def test_stop_session_stops_everything_and_is_idempotent(workspace, fake_redis, isolated_tmux, monkeypatch):
    monkeypatch.setattr(adapter, "SESSION_COMMAND", _claude_like("sleep 30"))
    port = await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)
    # Something that outlived its tmux session -- an MCP server whose parent died, say: same workspace,
    # same port in its env, but in no tmux session at all.
    orphan = await asyncio.create_subprocess_exec(
        "sleep", "30", cwd=workspace, env={**os.environ, SESSION_PORT_ENV: str(port)}, start_new_session=True
    )
    # The session's shell, the sleep it runs, and the orphan.
    assert len(await _wait_for_sessions(workspace, 3)) == 3

    stopped = await adapter.stop_session(_PROJECT, _CONVERSATION_ID)

    assert stopped["tmux_session"] is True and orphan.pid in stopped["pids"]
    assert await asyncio.wait_for(orphan.wait(), 5) is not None
    assert await adapter.find_processes_in_dir(workspace, adapter._SESSION_PORT_PATTERN) == []
    assert await adapter.stop_session(_PROJECT, _CONVERSATION_ID) == {"tmux_session": False, "pids": []}


async def test_stop_session_sigkills_what_ignores_sigterm(workspace, fake_redis, monkeypatch):
    monkeypatch.setattr(adapter, "SESSION_STOP_WAIT_SECONDS", 0.3)
    stubborn = await asyncio.create_subprocess_exec(
        "sh", "-c", "trap '' TERM; sleep 30", cwd=workspace, env={**os.environ, SESSION_PORT_ENV: "47001"}
    )
    await _wait_for_sessions(workspace, 1)
    await adapter.stop_session(_PROJECT, _CONVERSATION_ID)
    assert await asyncio.wait_for(stubborn.wait(), 5) == -9  # SIGKILL


@needs_tmux
async def test_start_presses_enter_on_the_startup_prompt(workspace, fake_redis, isolated_tmux, monkeypatch):
    monkeypatch.setattr(adapter, "SESSION_COMMAND", _claude_like("sleep 30"))
    await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)
    # ensure_session returned, so Enter was already sent -- the stand-in printed "accepted" after reading it.
    await asyncio.sleep(0.2)
    pane_text = await adapter._run_tmux("capture-pane", "-p", "-t", f"conv_{_CONVERSATION_ID}")
    assert "accepted" in pane_text


@needs_tmux
async def test_start_fails_if_the_prompt_never_shows(workspace, fake_redis, isolated_tmux, monkeypatch):
    monkeypatch.setattr(adapter, "SESSION_COMMAND", ["sleep", "30"])  # never prompts
    monkeypatch.setattr(adapter, "SESSION_STARTUP_PROMPT_WAIT_SECONDS", 2)
    with pytest.raises(adapter.SessionStartFailed, match="never showed"):
        await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)


@needs_tmux
async def test_start_fails_on_a_duplicate_tmux_session(workspace, fake_redis, isolated_tmux, monkeypatch):
    monkeypatch.setattr(adapter, "SESSION_COMMAND", _claude_like("sleep 30"))
    await adapter._run_tmux("new-session", "-d", "-s", f"conv_{_CONVERSATION_ID}", "sleep 30")  # a leftover
    with pytest.raises(adapter.SessionStartFailed, match="couldn't start"):
        await adapter.start_session(_PROJECT, _CONVERSATION_ID, 8899)


@needs_tmux
async def test_start_answers_the_trust_dialog_once_then_the_prompt(workspace, fake_redis, isolated_tmux, monkeypatch):
    # A folder Claude doesn't trust yet: the trust dialog first, then the channels prompt. In this plain
    # terminal the answered trust line stays on screen -- it must NOT be answered a second time (that
    # would push Down+Enter into the next prompt, i.e. "Exit"). The stand-in records each line it reads.
    monkeypatch.setattr(
        adapter,
        "SESSION_COMMAND",
        [
            "sh",
            "-c",
            (
                f'echo "{SESSION_TRUST_PROMPT}"; read trust; echo "trust answered"; '
                f'echo "{SESSION_STARTUP_PROMPT}"; read confirmation; echo "confirmation=[$confirmation]"; sleep 30'
            ),
        ],
    )
    await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)
    await asyncio.sleep(0.2)
    pane_text = await adapter._run_tmux("capture-pane", "-p", "-t", f"conv_{_CONVERSATION_ID}")
    assert "trust answered" in pane_text
    assert "confirmation=[]" in pane_text  # a bare Enter -- no stray Down from a second trust answer


def test_the_mcp_config_runs_our_communication_mcp(tmp_path):
    adapter.write_mcp_config(str(tmp_path))
    server = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["communication"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "src.mcps.communication.main"]
    repo_dir = Path(adapter.__file__).resolve().parents[3]
    assert server["env"]["PYTHONPATH"] == str(repo_dir) and (repo_dir / "src/mcps/communication/main.py").exists()


def test_the_template_lets_sessions_use_the_mcp_without_asking():
    # Committed despite settings.local.json often being git-ignored globally -- see .gitignore.
    settings = json.loads(
        (Path(adapter.__file__).resolve().parents[3] / "data/template/.claude/settings.local.json").read_text()
    )
    assert settings["enableAllProjectMcpServers"] is True  # no "New MCP server found" prompt at startup
    assert "Bash(pm2:*)" in settings["permissions"]["deny"]
