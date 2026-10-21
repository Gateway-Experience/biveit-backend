# agent2 tests, the backend's side: services/agent2 starting, finding and talking to runners. The runner is
# the real one (vib3-session-runner's session_runner, served in-process on a real unix socket); only its
# Claude Agent SDK client is faked (session_runner.testing.FakeClaudeClient). The runner itself is tested in
# its own repo.

import asyncio
import os

import pytest
from session_runner import main as runner
from session_runner.testing import FakeClaudeClient

from src.config import settings
from src.services import agent2
from src.services.agent2 import sessions as agent2_sessions
from src.utils import systemd
from src.utils.processes import CommandFailed


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch):
    FakeClaudeClient.instances = []
    monkeypatch.setattr(runner, "ClaudeSDKClient", FakeClaudeClient)
    monkeypatch.setattr(settings, "session_token_secret", "session-token-secret-for-tests")


def _text(text):
    return {"type": "text", "text": text}


# --- the backend's side, talking to a real runner over its socket ---


_PROJECT = {"id": 3, "hash": "p3", "user_hash": "u7"}


@pytest.fixture
def runners(tmp_path, monkeypatch):
    """services/agent2 against runners served right here (runner.serve as a task) instead of spawned
    processes, on a socket in tmp_path. Yields the runner tasks started."""
    started = []
    socket_path = str(tmp_path / "5.agent.sock")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    async def start_in_process(project, conversation_id):
        cwd = os.getcwd()
        os.chdir(workspace)  # serve() takes its workspace from its cwd, like the real process
        try:
            started.append(
                asyncio.create_task(
                    runner.serve(socket_path, "http://127.0.0.1:9", agent2_sessions.tokens.to_session_token(5))
                )
            )
            await asyncio.sleep(0)
        finally:
            os.chdir(cwd)

    async def no_workspace(project, conversation_id):
        pass

    monkeypatch.setattr(agent2_sessions, "get_socket_path", lambda project, conversation_id: socket_path)
    monkeypatch.setattr(agent2_sessions, "_start_runner", start_in_process)
    monkeypatch.setattr(agent2_sessions.devenv, "ensure_workspace", no_workspace)
    return started


async def test_a_runner_is_started_once_then_reused(runners):
    await agent2.ensure_session(_PROJECT, 5)
    await agent2.ensure_session(_PROJECT, 5)
    assert len(runners) == 1 and len(FakeClaudeClient.instances) == 1
    await agent2.stop_session(_PROJECT, 5)
    await asyncio.wait_for(runners[0], 5)


async def test_messages_reach_the_session_as_user_turns(runners):
    await agent2.ensure_session(_PROJECT, 5)
    await agent2.send(_PROJECT, 5, [_text("make it blue")])
    assert FakeClaudeClient.instances[0].turns == ["make it blue"]
    await agent2.stop_session(_PROJECT, 5)
    await asyncio.wait_for(runners[0], 5)


async def test_a_stopped_or_dead_runner_is_replaced_on_the_next_ensure(runners):
    await agent2.ensure_session(_PROJECT, 5)
    assert await agent2.stop_session(_PROJECT, 5)
    await asyncio.wait_for(runners[0], 5)
    assert not await agent2.stop_session(_PROJECT, 5)  # nothing left to stop

    await agent2.ensure_session(_PROJECT, 5)  # a fresh one
    FakeClaudeClient.instances[1].end()  # Claude Code goes away: the runner ends with it
    await asyncio.wait_for(runners[1], 5)
    await agent2.ensure_session(_PROJECT, 5)
    assert len(runners) == 3
    await agent2.stop_session(_PROJECT, 5)
    await asyncio.wait_for(runners[2], 5)


async def test_sending_to_a_runner_that_isnt_there_is_unavailable(runners, monkeypatch):
    monkeypatch.setattr(agent2_sessions, "SESSION_SEND_ATTEMPTS", 2)
    monkeypatch.setattr(agent2_sessions, "exponential_backoff", lambda *a, **k: lambda attempt: 0)
    with pytest.raises(agent2.RunnerUnavailable):
        await agent2.send(_PROJECT, 5, [_text("hello?")])


def test_billing_env_brings_an_endpoints_models_only_with_the_endpoint():
    assert agent2_sessions.to_billing_env("", "", "") == {}  # locally: Claude Code's own login and defaults
    assert agent2_sessions.to_billing_env("sk-ant", "", "") == {"ANTHROPIC_API_KEY": "sk-ant"}
    deepseek = agent2_sessions.to_billing_env("", "https://api.deepseek.com/anthropic", "sk-ds")
    assert deepseek["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert deepseek["ANTHROPIC_AUTH_TOKEN"] == "sk-ds" and deepseek["ANTHROPIC_MODEL"] == "deepseek-flash[1m]"


async def test_outside_local_a_runner_needs_an_api_key(monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "anthropic_auth_token", "")
    with pytest.raises(agent2.RunnerUnavailable, match="ANTHROPIC_API_KEY"):
        await agent2_sessions._start_runner(_PROJECT, 5)


def test_a_runner_units_env_stays_off_its_command_line():
    command = systemd.to_run_command(
        "agent2-local-conversation-5", ["python", "-m", "runner"], "/ws", "/s/5.sock.env", "/ws/5.agent.log"
    )
    assert command[:2] == ["systemd-run", "--user"] and "--property=EnvironmentFile=/s/5.sock.env" in command
    assert command[-3:] == ["python", "-m", "runner"] and "--property=StandardOutput=append:/ws/5.agent.log" in command
    assert systemd.to_env_file({"TOKEN": "a$b c"}) == "TOKEN='a$b c'\n"  # taken literally by systemd
    with pytest.raises(ValueError):
        systemd.to_env_file({"TOKEN": "it's"})


async def test_a_runner_that_stopped_answering_gets_its_unit_stopped(tmp_path, monkeypatch):
    stopped = []

    async def unit_active(unit):
        return True

    async def stop_unit(unit):
        stopped.append(unit)

    monkeypatch.setattr(agent2_sessions, "get_socket_path", lambda project, conversation_id: str(tmp_path / "x.sock"))
    monkeypatch.setattr(agent2_sessions.systemd, "is_available", lambda: True)
    monkeypatch.setattr(agent2_sessions.systemd, "is_active", unit_active)
    monkeypatch.setattr(agent2_sessions.systemd, "stop", stop_unit)
    assert await agent2.stop_session(_PROJECT, 5)
    assert stopped == [agent2.get_unit_name(5)]


async def test_losing_the_start_race_to_another_runner_is_fine(tmp_path, monkeypatch):
    async def unit_taken(*args):
        raise CommandFailed("Unit agent2-local-conversation-5.service already exists.")

    async def unit_active(unit):
        return True

    monkeypatch.setattr(agent2_sessions.systemd, "is_available", lambda: True)
    monkeypatch.setattr(agent2_sessions.systemd, "start_service", unit_taken)
    monkeypatch.setattr(agent2_sessions.systemd, "is_active", unit_active)
    monkeypatch.setattr(agent2_sessions, "get_socket_dir", lambda: str(tmp_path))
    await agent2_sessions._start_runner(_PROJECT, 5)  # no RunnerUnavailable: ensure_session waits for the winner
