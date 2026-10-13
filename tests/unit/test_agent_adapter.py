# Agent adapter tests (services/agent/adapter). Sessions are found by scanning /proc, so these spawn
# REAL throwaway processes (`sleep`) inside temp workspaces. Linux-only, like the code under test.
# Redis is faked: the lock is a no-op and port reservations live in a set.

import asyncio
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from src.config import settings
from src.constants import SESSION_PORT_ENV
from src.services.agent import adapter, storage
from src.utils import redis

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="process discovery reads /proc")

_PROJECT = {"id": 42, "user_id": 7}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Points STORAGE_DIR at a temp dir and returns the project's (created) workspace path."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    pwd = storage.get_project_dir(_PROJECT)
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


# --- ensure_session ---


async def test_ensure_session_reuses_the_running_session(workspace, spawn_in, fake_redis, monkeypatch):
    await spawn_in(workspace, {SESSION_PORT_ENV: "8899"})

    async def fail_start(project, port):
        raise AssertionError("must not start a second session")

    monkeypatch.setattr(adapter, "start_session", fail_start)
    assert await adapter.ensure_session(_PROJECT) == 8899


async def test_ensure_session_ignores_a_sessions_own_children(workspace, fake_redis, monkeypatch):
    # The session forks a child (think: a dev server). Same cwd, same env -- but the same OS session.
    monkeypatch.setattr(adapter, "SESSION_COMMAND", ["sh", "-c", "sleep 30 & exec sleep 30"])
    port = await adapter.ensure_session(_PROJECT)
    try:
        await asyncio.sleep(0.2)  # let the child start
        processes = await adapter.find_processes_in_dir(workspace, adapter._SESSION_PORT_PATTERN)
        assert len(processes) == 2
        assert await adapter.ensure_session(_PROJECT) == port  # one session, not TooManySessions
    finally:
        os.killpg(processes[0]["sid"], signal.SIGKILL)


async def test_ensure_session_refuses_when_several_are_running(workspace, spawn_in, fake_redis):
    await spawn_in(workspace, {SESSION_PORT_ENV: "8899"})
    await spawn_in(workspace, {SESSION_PORT_ENV: "8900"})

    with pytest.raises(adapter.TooManySessions):
        await adapter.ensure_session(_PROJECT)


async def test_ensure_session_starts_one_on_a_reserved_port(workspace, fake_redis, monkeypatch):
    started = {}

    async def fake_start(project, port):
        started["port"] = port

    monkeypatch.setattr(adapter, "start_session", fake_start)

    port = await adapter.ensure_session(_PROJECT)
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
    monkeypatch.setattr(adapter, "start_session", lambda project, port: asyncio.sleep(0))
    monkeypatch.setattr(settings, "storage_dir", "/nonexistent-storage")

    assert await adapter.ensure_session(_PROJECT) == 5002


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


async def test_started_session_is_found_again(workspace, fake_redis, monkeypatch):
    # The launch contract end to end: whatever start_session runs, ensure_session must find it next time.
    monkeypatch.setattr(adapter, "SESSION_COMMAND", ["sh", "-c", "echo booted; exec sleep 30"])

    port = await adapter.ensure_session(_PROJECT)
    try:
        processes = await adapter.find_processes_in_dir(workspace, adapter._SESSION_PORT_PATTERN)
        assert len(processes) == 1
        assert await adapter.ensure_session(_PROJECT) == port  # reused, not started again
        await asyncio.sleep(0.2)  # let the shell write before we read its log
        assert "booted" in await asyncio.to_thread(Path(f"{workspace}.session.log").read_text)
    finally:
        for process in processes:
            os.killpg(process["pid"], signal.SIGKILL)  # its own session -> its own process group
