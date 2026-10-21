# agent2 tests, the backend's side: services/agent2 starting, finding and talking to runners. The runner is
# the real one (vib3-session-runner's session_runner, served in-process on a real unix socket); only its
# Claude Agent SDK client is faked (session_runner.testing.FakeClaudeClient). The runner itself is tested in
# its own repo.

import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from session_runner import main as runner
from session_runner.testing import FakeClaudeClient

from src.app import app
from src.config import settings
from src.controllers.agent import listener_router
from src.services import agent2
from src.services.agent2 import models as agent2_models
from src.services.agent2 import runners as agent2_runners
from src.services.agent2 import sessions as agent2_sessions
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
    """services/agent2 against runners served right here (runner.serve as a task) instead of in a container
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
                asyncio.create_task(
                    runner.serve(socket_path, "http://127.0.0.1:9", agent2_sessions.tokens.to_session_token(5))
                )
            )
            await asyncio.sleep(0)
        finally:
            os.chdir(cwd)

    async def fake_run_in_container(project_id, argv, cwd=None, user=None, env=None, timeout_seconds=600):
        if argv[:2] == ["pm2", "delete"]:
            try:
                await agent2_sessions._request_runner(_PROJECT, 5, "POST", "/shutdown")
            except OSError as error:
                raise CommandFailed("process not found") from error
        return ""

    async def nothing(*args):
        pass

    async def running(project_id):
        return True

    monkeypatch.setattr(agent2_sessions, "get_socket_path", lambda project, conversation_id: socket_path)
    monkeypatch.setattr(agent2_sessions, "_start_runner", start_in_process)
    monkeypatch.setattr(agent2_sessions.devenv, "ensure_workspace", nothing)
    monkeypatch.setattr(agent2_sessions.devenv, "ensure_container", nothing)
    monkeypatch.setattr(agent2_sessions.devenv, "is_container_running", running)
    monkeypatch.setattr(agent2_sessions.devenv, "run_in_container", fake_run_in_container)
    monkeypatch.setattr(agent2_sessions.runners, "ensure_runner_installed", nothing)
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


async def test_a_hung_runner_is_thrown_out_and_replaced(monkeypatch):
    calls = []

    async def hung(project, conversation_id):
        return "hung"

    async def record(*args, **kwargs):
        calls.append(args)

    async def up_after_start(project, conversation_id, deadline):
        calls.append(("waited",))

    monkeypatch.setattr(agent2_sessions, "_probe", hung)
    monkeypatch.setattr(agent2_sessions, "_delete_runner", record)
    monkeypatch.setattr(agent2_sessions, "_start_runner", record)
    monkeypatch.setattr(agent2_sessions, "_wait_until_up", up_after_start)
    monkeypatch.setattr(agent2_sessions.devenv, "ensure_workspace", record)
    monkeypatch.setattr(agent2_sessions.devenv, "ensure_container", record)
    monkeypatch.setattr(agent2_sessions.runners, "ensure_runner_installed", record)
    await agent2.ensure_session(_PROJECT, 5)
    assert calls[0] == (3, 5) and calls[-1] == ("waited",)  # deleted first, a fresh one waited for last


# --- what a runner gets: started in its container, its env, its model ---


def test_a_runner_never_gets_the_model_key(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-real")
    monkeypatch.setattr(settings, "anthropic_auth_token", "sk-ds-real")
    monkeypatch.setattr(settings, "anthropic_base_url", "")
    env = agent2_sessions.to_runner_env(5, "session-token")
    assert "sk-ant-real" not in env.values() and "sk-ds-real" not in env.values()
    assert env["ANTHROPIC_BASE_URL"] == "http://10.213.0.1:3002/api/agent/model"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "session-token" and env["AGENT_SOCKET"] == "/sockets/5.sock"
    assert "ANTHROPIC_MODEL" not in env  # Anthropic's own: Claude Code's defaults
    monkeypatch.setattr(settings, "anthropic_base_url", "https://api.deepseek.com/anthropic")
    assert agent2_sessions.to_runner_env(5, "t")["ANTHROPIC_MODEL"] == "deepseek-flash[1m]"


async def test_without_a_model_key_no_runner_starts(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "anthropic_auth_token", "")
    with pytest.raises(agent2.RunnerUnavailable, match="ANTHROPIC_API_KEY"):
        await agent2_sessions._start_runner(_PROJECT, 5)


async def test_a_runner_starts_under_its_containers_pm2_with_its_secrets_off_the_command_line(tmp_path, monkeypatch):
    calls = []

    async def fake_run_in_container(project_id, argv, cwd=None, user=None, env=None, timeout_seconds=600):
        calls.append((project_id, argv, env))
        return ""

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "anthropic_auth_token", "sk-ds-real")
    monkeypatch.setattr(agent2_sessions.devenv, "run_in_container", fake_run_in_container)
    await agent2_sessions._start_runner(_PROJECT, 5)
    [_, (project_id, argv, env)] = calls  # the leftover's delete, then the start
    token = agent2_sessions.tokens.to_session_token(5)
    assert project_id == 3 and argv[:3] == ["pm2", "start", "/opt/session-runner/.venv/bin/python"]
    assert argv[-2:] == ["-m", "session_runner"] and "/project/5" in argv
    assert token not in " ".join(argv) and env["COMMUNICATION_TOKEN"] == token  # by name, via docker's env


# --- the runner installed in a container ---


def test_the_runners_version_follows_its_source_not_its_venv(tmp_path):
    (tmp_path / "session_runner").mkdir()
    (tmp_path / "session_runner/main.py").write_text("v1")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv/junk").write_text("x")
    first = agent2_runners.get_source_version(str(tmp_path))
    (tmp_path / ".venv/junk").write_text("y")
    assert agent2_runners.get_source_version(str(tmp_path)) == first
    (tmp_path / "session_runner/main.py").write_text("v2")
    assert agent2_runners.get_source_version(str(tmp_path)) != first


async def test_the_runner_is_installed_only_when_the_container_has_another_version(tmp_path, monkeypatch):
    (tmp_path / "main.py").write_text("v1")
    monkeypatch.setattr(settings, "session_runner_dir", str(tmp_path))
    version = agent2_runners.get_source_version(str(tmp_path))
    state = {"installed": version, "ran": []}

    async def fake_run_in_container(project_id, argv, cwd=None, user=None, env=None, timeout_seconds=600):
        state["ran"] = [*state["ran"], (argv, user)]
        if argv[0] == "cat":
            return f"{state['installed']}\n"
        return ""

    async def fake_extract(project_id, archive, container_dir):
        state["ran"] = [*state["ran"], (["docker cp", container_dir], None)]

    monkeypatch.setattr(agent2_runners.devenv, "run_in_container", fake_run_in_container)
    monkeypatch.setattr(agent2_runners.devenv, "extract_into_container", fake_extract)
    await agent2_runners.ensure_runner_installed(_PROJECT)
    assert [argv[0] for argv, _ in state["ran"]] == ["cat"]  # current already: nothing to do

    state.update(installed="older", ran=[])
    await agent2_runners.ensure_runner_installed(_PROJECT)
    build = next(argv for argv, user in state["ran"] if argv[0] == "sh")
    assert "uv sync --frozen" in build[-1] and f"echo {version} > VERSION" in build[-1]
    assert "HOME=/root" in build[-1]  # never the agent's HOME: nothing the agent wrote steers a root install
    assert ("sh", "root") in [
        (argv[0], user) for argv, user in state["ran"]
    ]  # built as root: the agent can't change it


# --- the model proxy ---


async def test_the_model_proxy_forwards_only_model_calls_with_our_key(monkeypatch):
    sent = {}

    class FakeSession:
        async def request(self, method, url, **kwargs):
            sent.update(method=method, url=url, **kwargs)
            return "response"

    monkeypatch.setattr(settings, "anthropic_base_url", "https://api.deepseek.com/anthropic")
    monkeypatch.setattr(settings, "anthropic_auth_token", "sk-ds-real")
    monkeypatch.setattr(agent2_models.http, "session", lambda: FakeSession())
    headers = {"Authorization": "Bearer session-token", "anthropic-version": "2023-06-01", "Host": "10.213.0.1"}
    await agent2.open_model_request("POST", "v1/messages", "beta=true", headers, b"{}")
    assert sent["url"] == "https://api.deepseek.com/anthropic/v1/messages?beta=true"
    assert sent["headers"] == {"anthropic-version": "2023-06-01", "Authorization": "Bearer sk-ds-real"}
    with pytest.raises(agent2.ModelPathRefused):  # with our key, a provider's other endpoints are off limits
        await agent2.open_model_request("GET", "v1/files", "", headers, b"")


def test_the_model_route_is_never_on_the_public_api():
    # Not even reached: the public app has no such route (no token check, no proxying -- a plain 404).
    response = TestClient(app).post("/api/agent/model/v1/messages", headers={"Authorization": "Bearer x"}, json={})
    assert response.status_code == 404
    assert any("/agent/model" in route.path for route in listener_router.routes)
