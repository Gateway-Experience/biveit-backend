# Agent adapter tests (services/agent/adapter). Sessions are found by scanning /proc, so these spawn
# REAL throwaway processes (`sleep`) inside temp workspaces. Linux-only, like the code under test.
# Redis is faked: the lock is a no-op and port reservations live in a set.

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager

import pytest

from src.config import settings
from src.constants import SESSION_PORT_ENV, SESSION_STARTUP_PROMPT, SESSION_TRUST_PROMPT
from src.services.agent import adapter, storage
from src.utils import redis

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="process discovery reads /proc")
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="sessions run inside tmux")

_PROJECT = {"id": 42, "user_id": 7}
_CONVERSATION_ID = 5


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Points STORAGE_DIR at a temp dir and returns the project's (created) workspace path."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    pwd = storage.get_conversation_dir(_PROJECT, _CONVERSATION_ID)
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
    waits for Enter, then runs `then` (shell)."""
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
    await spawn_in(workspace, {SESSION_PORT_ENV: "8899"})

    async def fail_start(project, conversation_id, port):
        raise AssertionError("must not start a second session")

    monkeypatch.setattr(adapter, "start_session", fail_start)
    assert await adapter.ensure_session(_PROJECT, _CONVERSATION_ID) == 8899


@needs_tmux
async def test_ensure_session_ignores_a_sessions_own_children(workspace, fake_redis, isolated_tmux, monkeypatch):
    # The session forks a child (think: a dev server). Same cwd, same env -- but the same OS session.
    monkeypatch.setattr(adapter, "SESSION_COMMAND", _claude_like("sleep 30 & exec sleep 30"))
    port = await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)
    processes = await _wait_for_sessions(workspace, 2)
    assert len(processes) == 2
    assert await adapter.ensure_session(_PROJECT, _CONVERSATION_ID) == port  # one session, not TooManySessions


async def test_ensure_session_refuses_when_several_are_running(workspace, spawn_in, fake_redis):
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


async def test_claim_free_port_retries_past_a_reserved_port(fake_redis, monkeypatch):
    # The first probe lands on an already-reserved port; with_retries should move on to the next one.
    ports = iter([5001, 5002])

    class FakeSocket:
        def __enter__(self):
            self.port = next(ports)
            return self

        def __exit__(self, *exc):
            return False

        def bind(self, address):
            pass

        def getsockname(self):
            return ("0.0.0.0", self.port)

    fake_redis.add("agent_session_port:5001")
    monkeypatch.setattr(adapter.socket, "socket", FakeSocket)
    monkeypatch.setattr(adapter, "start_session", lambda project, conversation_id, port: asyncio.sleep(0))
    monkeypatch.setattr(settings, "storage_dir", "/nonexistent-storage")

    assert await adapter.ensure_session(_PROJECT, _CONVERSATION_ID) == 5002


# --- workspace seeding ---


def test_copy_missing_files_never_overwrites(tmp_path):
    template = tmp_path / "template"
    (template / "src").mkdir(parents=True)
    (template / "index.html").write_text("template index")
    (template / "src" / "app.js").write_text("template app")

    destination = tmp_path / "ws"
    (destination).mkdir()
    (destination / "index.html").write_text("user edited")

    adapter._copy_missing_files(str(template), str(destination))

    assert (destination / "index.html").read_text() == "user edited"
    assert (destination / "src" / "app.js").read_text() == "template app"


# --- start_session ---


@needs_tmux
async def test_started_session_is_found_again(workspace, fake_redis, isolated_tmux, monkeypatch):
    # The launch contract end to end: whatever start_session runs, ensure_session must find it next time --
    # which takes the port reaching the pane (tmux -e) and the pane running in the workspace (tmux -c).
    monkeypatch.setattr(adapter, "SESSION_COMMAND", _claude_like("exec sleep 30"))
    monkeypatch.setattr(settings, "admin_token", "outbox-test-token")

    port = await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)
    processes = await _wait_for_sessions(workspace, 1)
    assert len(processes) == 1
    assert f"{SESSION_PORT_ENV}={port}" in processes[0]["environ"]
    # ...and so does the way back: where the channel posts the agent's messages, and the token it sends.
    assert f"EVENT_CHANNEL_OUTBOX_URL=http://127.0.0.1:{settings.api_port}" in processes[0]["environ"]
    assert "EVENT_CHANNEL_OUTBOX_TOKEN=outbox-test-token" in processes[0]["environ"]
    assert await adapter.ensure_session(_PROJECT, _CONVERSATION_ID) == port  # reused, not started again


@needs_tmux
async def test_start_presses_enter_on_the_startup_prompt(workspace, fake_redis, isolated_tmux, monkeypatch):
    monkeypatch.setattr(adapter, "SESSION_COMMAND", _claude_like("exec sleep 30"))
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
    monkeypatch.setattr(adapter, "SESSION_COMMAND", _claude_like("exec sleep 30"))
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
                f'echo "{SESSION_STARTUP_PROMPT}"; read confirmation; echo "confirmation=[$confirmation]"; exec sleep 30'
            ),
        ],
    )
    await adapter.ensure_session(_PROJECT, _CONVERSATION_ID)
    await asyncio.sleep(0.2)
    pane_text = await adapter._run_tmux("capture-pane", "-p", "-t", f"conv_{_CONVERSATION_ID}")
    assert "trust answered" in pane_text
    assert "confirmation=[]" in pane_text  # a bare Enter -- no stray Down from a second trust answer
