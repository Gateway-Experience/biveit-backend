# Project container tests (services/devenv/containers): what `docker run` gets -- the security-relevant part
# -- and ensure_container's branches, with docker faked (the real thing is checked by hand: see the plan).

import json
import os

import pytest

from src.config import settings
from src.constants import DEVENV_IMAGE
from src.services.devenv import containers

_PROJECT = {"id": 39, "hash": "b81d6e0f3a92c475", "user_id": 7, "user_hash": "4e7a1c9b0d2f8a63", "meta": {}}


@pytest.fixture(autouse=True)
def _storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "env", "dev")


def _options(argv, prefix):
    return [option.removeprefix(prefix) for option in argv if option.startswith(prefix)]


def test_a_container_gets_only_its_project_and_its_sockets(tmp_path):
    argv = containers._to_run_argv(_PROJECT, "biveit-devenv:1")
    project_dir = f"{tmp_path}/storage/4e7a1c9b0d2f8a63/b81d6e0f3a92c475"
    assert _options(argv, "--volume=") == [f"{project_dir}:/project", f"{tmp_path}/agent2_sockets/39:/sockets"]
    assert _options(argv, "--publish=") == ["127.0.0.1:8039:8000", "127.0.0.1:16039:5173"]  # host side only
    assert argv[-3:] == ["biveit-devenv:1", "sleep", "infinity"]


def test_a_container_is_hardened_and_limited():
    argv = containers._to_run_argv(_PROJECT, "biveit-devenv:1")
    assert {"--init", "--cap-drop=ALL", "--security-opt=no-new-privileges"} <= set(argv)
    assert f"--user={os.getuid()}:{os.getgid()}" in argv
    assert not [option for option in argv if "privileged" in option or "docker.sock" in option or "--cap-add" in option]
    assert not [option for option in argv if option.startswith(("--network=host", "--pid=", "--ipc="))]
    assert {"--memory=3g", "--memory-swap=3g", "--pids-limit=2048", "--ulimit=nofile=65536:65536"} <= set(argv)
    assert "--network=biveit-projects" in argv and "--name=biveit-dev-project-39" in argv


def test_container_paths_are_the_projects_only(tmp_path):
    project_dir = f"{tmp_path}/storage/4e7a1c9b0d2f8a63/b81d6e0f3a92c475"
    assert containers.to_container_path(_PROJECT, f"{project_dir}/57/backend") == "/project/57/backend"
    assert containers.to_container_path(_PROJECT, project_dir) == "/project"
    with pytest.raises(ValueError):
        containers.to_container_path(_PROJECT, f"{tmp_path}/storage/4e7a1c9b0d2f8a63/other")


def test_the_gateway_is_the_subnets_first_address(monkeypatch):
    monkeypatch.setattr(settings, "devenv_network_subnet", "10.213.0.0/16")
    assert containers.get_gateway_ip() == "10.213.0.1"


@pytest.fixture
def docker(monkeypatch):
    """A fake docker CLI: `state["container"]` is what inspect sees (None = no such container); records
    every command, and the project updates."""
    state = {"container": None, "ran": [], "updates": [], "fail_run": False}

    async def fake_run_command(argv, cwd=None, input_text=None, timeout_seconds=600, env=None):
        state["ran"] = [*state["ran"], argv]
        if argv[:2] == ["docker", "inspect"]:
            if state["container"] is None:
                raise containers.CommandFailed("no such container")
            return json.dumps([state["container"]])
        if argv[:2] == ["docker", "run"]:
            if state["fail_run"]:
                raise containers.CommandFailed("name already in use")
            state["container"] = {"State": {"Running": True}, "Config": {"Image": argv[-3]}}
        if argv[:2] == ["docker", "start"]:
            state["container"] = {**state["container"], "State": {"Running": True}}
        return ""

    async def fake_update(project_id, updates):
        state["updates"] = [*state["updates"], (project_id, updates)]

    monkeypatch.setattr(containers, "run_command", fake_run_command)
    monkeypatch.setattr(containers.projects_repo, "update", fake_update)
    return state


def _ran(docker, verb):
    return [argv for argv in docker["ran"] if argv[:2] == ["docker", verb]]


async def test_the_network_keeps_containers_apart(monkeypatch):
    # Every project shares the bridge, so container-to-container traffic is off; the host is still reachable.
    state = {"ran": []}

    async def fake_run_command(argv, **_):
        state["ran"] = [*state["ran"], argv]
        if argv[:3] == ["docker", "network", "inspect"]:
            raise containers.CommandFailed("no such network")
        return ""

    monkeypatch.setattr(containers, "run_command", fake_run_command)
    await containers.ensure_network()
    created = state["ran"][-1]
    assert created[:3] == ["docker", "network", "create"]
    assert "--opt=com.docker.network.bridge.enable_icc=false" in created


async def test_a_new_project_gets_the_current_image_recorded_and_a_container(docker):
    await containers.ensure_container(_PROJECT)
    assert docker["updates"] == [(39, {"meta": {"devenv_image": DEVENV_IMAGE}})]
    assert len(_ran(docker, "run")) == 1 and _ran(docker, "run")[0][-3] == DEVENV_IMAGE
    assert _ran(docker, "exec")  # its user is ensured


async def test_a_project_keeps_the_image_it_was_created_with(docker):
    await containers.ensure_container({**_PROJECT, "meta": {"devenv_image": "biveit-devenv:0"}})
    assert docker["updates"] == [] and _ran(docker, "run")[0][-3] == "biveit-devenv:0"


async def test_a_stopped_container_is_started_and_a_running_one_left_alone(docker):
    docker["container"] = {"State": {"Running": False}, "Config": {"Image": "biveit-devenv:1"}}
    await containers.ensure_container(_PROJECT)
    assert _ran(docker, "start") and not _ran(docker, "run")
    docker["ran"] = []
    await containers.ensure_container(_PROJECT)
    assert not _ran(docker, "start") and not _ran(docker, "run")


async def test_losing_a_creation_race_is_fine_only_if_the_other_container_runs(docker, monkeypatch):
    docker["fail_run"] = True

    async def raced(project_id):
        return True

    monkeypatch.setattr(containers, "is_container_running", raced)
    await containers.ensure_container(_PROJECT)  # the other ensure's container is up: fine

    async def not_running(project_id):
        return False

    monkeypatch.setattr(containers, "is_container_running", not_running)
    with pytest.raises(containers.ContainerUnavailable):
        await containers.ensure_container(_PROJECT)


async def test_locally_starting_a_projects_container_stops_the_others(docker, monkeypatch):
    # Locally every project publishes the same two dev ports: only one container can hold them.
    monkeypatch.setattr(settings, "env", "local")
    real_run_command = containers.run_command

    async def with_others_running(argv, cwd=None, input_text=None, timeout_seconds=600, env=None):
        if argv[:2] == ["docker", "ps"]:
            return "biveit-local-project-7\nbiveit-local-project-39\n"
        return await real_run_command(argv, cwd, input_text, timeout_seconds, env)

    monkeypatch.setattr(containers, "run_command", with_others_running)
    await containers.ensure_container(_PROJECT)
    assert _ran(docker, "stop") == [["docker", "stop", "biveit-local-project-7"]]  # not itself


async def test_stopping_and_removing_are_idempotent(docker):
    assert await containers.stop_container(39) is False
    await containers.remove_container(39)
    assert not _ran(docker, "stop") and not _ran(docker, "rm")
