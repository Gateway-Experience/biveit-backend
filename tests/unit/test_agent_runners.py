# Runner tests, the backend's side: services/agent/runners starting, finding and talking to runners, and
# runner_installs putting the runner into a container. The runner is the real one (vib3-session-runner's
# session_runner, served in-process on a real unix socket); only its Claude Agent SDK client is faked
# (session_runner.testing.FakeClaudeClient). The runner itself is tested in its own repo.

import asyncio
import os

import pytest
from session_runner import main as runner
from session_runner.testing import FakeClaudeClient

from src.config import settings
from src.services.agent import runner_installs, runners
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
def served_runners(tmp_path, monkeypatch):
    """services/agent/runners against runners served right here (runner.serve as a task) instead of in a container
    under pm2, on a socket in tmp_path; the container, the runner's install and pm2 are faked (`pm2 delete`
    shuts the runner down, as pm2's SIGTERM would). Yields the runner tasks started."""
    started = []
    socket_path = str(tmp_path / "5.sock")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    async def start_in_process(project, conversation_id):
        cwd = os.getcwd()
        os.chdir(workspace)  # serve() takes its workspace from its cwd, like the real process
        try:
            started.append(
                asyncio.create_task(runner.serve(socket_path, "http://127.0.0.1:9", runners.tokens.to_session_token(5)))
            )
            await asyncio.sleep(0)
        finally:
            os.chdir(cwd)

    async def fake_run_in_container(project_id, argv, cwd=None, user=None, env=None, timeout_seconds=600):
        if argv[:2] == ["pm2", "delete"]:
            try:
                await runners._request_runner(_PROJECT, 5, "POST", "/shutdown")
            except OSError as error:
                raise CommandFailed("process not found") from error
        return ""

    async def nothing(*args):
        pass

    async def running(project_id):
        return True

    monkeypatch.setattr(runners, "get_socket_path", lambda project, conversation_id: socket_path)
    monkeypatch.setattr(runners, "_start_runner", start_in_process)
    monkeypatch.setattr(runners.devenv, "ensure_workspace", nothing)
    monkeypatch.setattr(runners.devenv, "ensure_container", nothing)
    monkeypatch.setattr(runners.devenv, "is_container_running", running)
    monkeypatch.setattr(runners.devenv, "run_in_container", fake_run_in_container)
    monkeypatch.setattr(runner_installs, "ensure_runner_installed", nothing)
    return started


async def test_a_runner_is_started_once_then_reused(served_runners):
    await runners.ensure_session(_PROJECT, 5)
    await runners.ensure_session(_PROJECT, 5)
    assert len(served_runners) == 1 and len(FakeClaudeClient.instances) == 1
    await runners.stop_session(_PROJECT, 5)
    await asyncio.wait_for(served_runners[0], 5)


async def test_messages_reach_the_session_as_user_turns(served_runners):
    await runners.ensure_session(_PROJECT, 5)
    await runners.send(_PROJECT, 5, [_text("make it blue")])
    assert FakeClaudeClient.instances[0].turns == ["make it blue"]
    await runners.stop_session(_PROJECT, 5)
    await asyncio.wait_for(served_runners[0], 5)


async def test_a_stopped_or_dead_runner_is_replaced_on_the_next_ensure(served_runners):
    await runners.ensure_session(_PROJECT, 5)
    assert await runners.stop_session(_PROJECT, 5)
    await asyncio.wait_for(served_runners[0], 5)
    assert not await runners.stop_session(_PROJECT, 5)  # nothing left to stop

    await runners.ensure_session(_PROJECT, 5)  # a fresh one
    FakeClaudeClient.instances[1].end()  # Claude Code goes away: the runner ends with it
    await asyncio.wait_for(served_runners[1], 5)
    await runners.ensure_session(_PROJECT, 5)
    assert len(served_runners) == 3
    await runners.stop_session(_PROJECT, 5)
    await asyncio.wait_for(served_runners[2], 5)


async def test_sending_to_a_runner_that_isnt_there_is_unavailable(served_runners, monkeypatch):
    monkeypatch.setattr(runners, "RUNNER_SEND_ATTEMPTS", 2)
    monkeypatch.setattr(runners, "exponential_backoff", lambda *a, **k: lambda attempt: 0)
    with pytest.raises(runners.RunnerUnavailable):
        await runners.send(_PROJECT, 5, [_text("hello?")])


async def test_a_hung_runner_is_thrown_out_and_replaced(monkeypatch):
    calls = []

    async def hung(project, conversation_id):
        return "hung"

    async def record(*args, **kwargs):
        calls.append(args)

    async def up_after_start(project, conversation_id, deadline):
        calls.append(("waited",))

    monkeypatch.setattr(runners, "_probe", hung)
    monkeypatch.setattr(runners, "_delete_runner", record)
    monkeypatch.setattr(runners, "_start_runner", record)
    monkeypatch.setattr(runners, "_wait_until_up", up_after_start)
    monkeypatch.setattr(runners.devenv, "ensure_workspace", record)
    monkeypatch.setattr(runners.devenv, "ensure_container", record)
    monkeypatch.setattr(runner_installs, "ensure_runner_installed", record)
    await runners.ensure_session(_PROJECT, 5)
    assert calls[0] == (3, 5) and calls[-1] == ("waited",)  # deleted first, a fresh one waited for last


# --- what a runner gets: started in its container, its env, its model ---


def test_a_runner_never_gets_the_model_key(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-real")
    monkeypatch.setattr(settings, "anthropic_auth_token", "sk-ds-real")
    monkeypatch.setattr(settings, "anthropic_base_url", "")
    env = runners.to_runner_env(5, "session-token")
    assert "sk-ant-real" not in env.values() and "sk-ds-real" not in env.values()
    assert env["ANTHROPIC_BASE_URL"] == "http://10.213.0.1:3002/api/agent/model"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "session-token" and env["AGENT_SOCKET"] == "/sockets/5.sock"
    assert "ANTHROPIC_MODEL" not in env  # Anthropic's own: Claude Code's defaults
    monkeypatch.setattr(settings, "anthropic_base_url", "https://api.deepseek.com/anthropic")
    assert runners.to_runner_env(5, "t")["ANTHROPIC_MODEL"] == "deepseek-flash[1m]"


async def test_without_a_model_key_no_runner_starts(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "anthropic_auth_token", "")
    with pytest.raises(runners.RunnerUnavailable, match="ANTHROPIC_API_KEY"):
        await runners._start_runner(_PROJECT, 5)


async def test_a_runner_starts_under_its_containers_pm2_with_its_secrets_off_the_command_line(tmp_path, monkeypatch):
    calls = []

    async def fake_run_in_container(project_id, argv, cwd=None, user=None, env=None, timeout_seconds=600):
        calls.append((project_id, argv, env))
        return ""

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "anthropic_auth_token", "sk-ds-real")
    monkeypatch.setattr(runners.devenv, "run_in_container", fake_run_in_container)
    await runners._start_runner(_PROJECT, 5)
    [_, (project_id, argv, env)] = calls  # the leftover's delete, then the start
    token = runners.tokens.to_session_token(5)
    assert project_id == 3 and argv[:3] == ["pm2", "start", "/opt/session-runner/.venv/bin/python"]
    assert argv[-2:] == ["-m", "session_runner"] and "/project/5" in argv
    assert token not in " ".join(argv) and env["COMMUNICATION_TOKEN"] == token  # by name, via docker's env


# --- the runner installed in a container ---


def test_the_runners_version_follows_its_source_not_its_venv(tmp_path):
    (tmp_path / "session_runner").mkdir()
    (tmp_path / "session_runner/main.py").write_text("v1")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv/junk").write_text("x")
    first = runner_installs.get_source_version(str(tmp_path))
    (tmp_path / ".venv/junk").write_text("y")
    assert runner_installs.get_source_version(str(tmp_path)) == first
    (tmp_path / "session_runner/main.py").write_text("v2")
    assert runner_installs.get_source_version(str(tmp_path)) != first


async def test_the_runner_is_installed_only_when_the_container_has_another_version(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("v1")
    monkeypatch.setattr(settings, "session_runner_dir", str(tmp_path))
    version = runner_installs.get_source_version(str(tmp_path))
    state = {"installed": version, "ran": []}

    async def fake_run_in_container(project_id, argv, cwd=None, user=None, env=None, timeout_seconds=600):
        state["ran"] = [*state["ran"], (argv, user)]
        if argv[0] == "cat":
            return f"{state['installed']}\n"
        return ""

    async def fake_extract(project_id, archive, container_dir):
        state["ran"] = [*state["ran"], (["docker cp", container_dir], None)]

    monkeypatch.setattr(runner_installs.devenv, "run_in_container", fake_run_in_container)
    monkeypatch.setattr(runner_installs.devenv, "extract_into_container", fake_extract)
    await runner_installs.ensure_runner_installed(_PROJECT)
    assert [argv[0] for argv, _ in state["ran"]] == ["cat"]  # current already: nothing to do

    state.update(installed="older", ran=[])
    await runner_installs.ensure_runner_installed(_PROJECT)
    build = next(argv for argv, user in state["ran"] if argv[0] == "sh")
    assert "uv sync --frozen" in build[-1] and f"echo {version} > VERSION" in build[-1]
    assert "HOME=/root" in build[-1]  # never the agent's HOME: nothing the agent wrote steers a root install
    assert ("sh", "root") in [
        (argv[0], user) for argv, user in state["ran"]
    ]  # built as root: the agent can't change it
