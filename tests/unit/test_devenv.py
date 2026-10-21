# Dev environment tests: the layout, seeding, templates, copying the boilerplate, the setup steps, and
# the pm2/nginx commands. Real files in temp dirs (this IS the file handling); the external commands --
# uv, npm, pm2, sudo -- are faked and recorded.

import json
from contextlib import asynccontextmanager

import pytest

from src.config import settings
from src.constants import DevServerAction
from src.services import devenv
from src.services.devenv import addresses, containers, databases, environments, servers, sites, templates, workspaces
from src.utils import paths
from src.utils.processes import CommandFailed, get_base_env, run_command

_PROJECT = {"id": 42, "hash": "p42hash", "user_id": 7, "user_hash": "u7hash"}


@pytest.fixture(autouse=True)
def _storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    # A deployed platform: per-project ports and a dev site. The local exceptions have their own test.
    monkeypatch.setattr(settings, "env", "prod")


def test_layout_is_user_project_conversation(tmp_path):
    assert devenv.get_project_dir(_PROJECT) == f"{tmp_path}/storage/u7hash/p42hash"
    assert devenv.get_conversation_dir(_PROJECT, 9) == f"{tmp_path}/storage/u7hash/p42hash/9"


async def test_seeding_a_workspace_never_overwrites_or_follows_what_the_agent_put_there(tmp_path, monkeypatch):
    template = tmp_path / "template"
    (template / "src").mkdir(parents=True)
    (template / ".claude").mkdir()
    (template / "index.html").write_text("template index")
    (template / "src" / "app.js").write_text("template app")
    (template / ".claude" / "settings.json").write_text("template settings")
    monkeypatch.setattr(settings, "template_dir", str(template))
    outside = tmp_path / "outside"
    outside.mkdir()

    workspace = tmp_path / "storage/u7hash/p42hash/9"
    workspace.mkdir(parents=True)
    (workspace / "index.html").write_text("user edited")
    (workspace / ".claude").symlink_to(outside)  # planted: a folder of ours pointing out of the project

    await workspaces.ensure_workspace(_PROJECT, 9)

    assert (workspace / "index.html").read_text() == "user edited"
    assert (workspace / "src" / "app.js").read_text() == "template app"
    assert list(outside.iterdir()) == []  # nothing written through the link


async def test_ensure_workspace_creates_and_seeds_the_conversation_dir(tmp_path, monkeypatch):
    template = tmp_path / "template"
    template.mkdir()
    (template / "index.html").write_text("template index")
    monkeypatch.setattr(settings, "template_dir", str(template))

    await devenv.ensure_workspace(_PROJECT, 9)

    assert (tmp_path / "storage/u7hash/p42hash/9/index.html").read_text() == "template index"


# --- templates ---


@pytest.fixture
def templates_dir(tmp_path, monkeypatch):
    """A temp DEVENV_TEMPLATES_DIR, and a seed so secret() works."""
    directory = tmp_path / "devenv_templates"
    directory.mkdir()
    monkeypatch.setattr(settings, "devenv_templates_dir", str(directory))
    monkeypatch.setattr(settings, "project_secret_seed", "seed-for-tests")
    monkeypatch.setattr(settings, "dev_domain", "dev.example.com")
    return directory


def test_templates_get_the_projects_addresses_and_settings(templates_dir, monkeypatch):
    monkeypatch.setattr(settings, "project_mongo_uri", "mongodb://shared")
    (templates_dir / "t.tmpl").write_text(
        "PORT={{{dev_backend_port}}} VITE={{{dev_frontend_port}}} HOST={{{dev_host}}} DB={{{settings.project_mongo_uri}}}"
    )
    assert templates.render_template("t.tmpl", _PROJECT, 9) == (
        "PORT=8042 VITE=16042 HOST=p42hash.dev.example.com DB=mongodb://shared"
    )


def test_a_projects_mongo_login_is_its_owners(templates_dir, monkeypatch):
    monkeypatch.setattr(settings, "project_mongo_host", "mongo.internal:27017")
    (templates_dir / "t.tmpl").write_text("{{{dev_mongo_uri()}}}")
    uri = templates.render_template("t.tmpl", _PROJECT, 9)
    same_owner = templates.render_template("t.tmpl", {**_PROJECT, "id": 43}, 9)
    other_owner = templates.render_template("t.tmpl", {**_PROJECT, "user_id": 8}, 9)
    assert uri.startswith("mongodb://user_7:") and uri.endswith(
        "@mongo.internal:27017/db_project_dev_42?authSource=admin"
    )
    assert same_owner.split("@")[0] == uri.split("@")[0]  # one login per owner...
    assert other_owner.split("@")[0] != uri.split("@")[0]  # ...each with its own password


def test_secrets_are_derived_per_project_and_name(templates_dir):
    (templates_dir / "t.tmpl").write_text('{{{secret("jwt")}}} {{{secret("jwt")}}} {{{secret("admin", 10)}}}')
    jwt, jwt_again, admin = templates.render_template("t.tmpl", _PROJECT, 9).split()
    other_project_jwt = templates.render_template("t.tmpl", {**_PROJECT, "id": 43}, 9).split()[0]
    assert jwt == jwt_again and len(jwt) == 48 and jwt[0].isalpha() and jwt.isalnum()
    assert len(admin) == 10 and admin != jwt[:10]
    assert other_project_jwt != jwt


def test_template_failures_name_the_template(templates_dir, monkeypatch):
    (templates_dir / "t.tmpl").write_text("{{{no_such_name}}}")
    with pytest.raises(templates.TemplateFailed, match=r"t\.tmpl: NameError"):
        templates.render_template("t.tmpl", _PROJECT, 9)
    monkeypatch.setattr(settings, "project_secret_seed", "")
    (templates_dir / "s.tmpl").write_text('{{{secret("jwt")}}}')
    with pytest.raises(templates.TemplateFailed, match="PROJECT_SECRET_SEED"):
        templates.render_template("s.tmpl", _PROJECT, 9)


def test_the_shipped_templates_render(monkeypatch):
    # The real templates in data/devenv_templates, with and without a dev domain.
    monkeypatch.setattr(settings, "project_secret_seed", "seed-for-tests")
    names = ["backend.env.local.tmpl", "frontend.env.tmpl", "ecosystem.config.cjs.tmpl"]
    assert all("{{{" not in templates.render_template(name, _PROJECT, 9) for name in names)
    monkeypatch.setattr(settings, "dev_domain", "dev.example.com")
    site = templates.render_template("nginx-dev.conf.tmpl", _PROJECT, 9)
    assert "server_name p42hash.dev.example.com;" in site and "127.0.0.1:16042" in site


def test_a_local_platform_uses_fixed_ports_and_no_site(monkeypatch):
    monkeypatch.setattr(settings, "env", "local")
    monkeypatch.setattr(settings, "dev_domain", "dev.example.com")
    assert (addresses.get_dev_backend_port(42), addresses.get_dev_frontend_port(42)) == (10101, 10102)
    assert addresses.get_dev_host(_PROJECT) is None
    assert addresses.get_dev_url(_PROJECT) == "http://localhost:10102"


# --- copying the boilerplate ---


@pytest.fixture
def boilerplate(tmp_path, monkeypatch):
    root = tmp_path / "boilerplate"
    (root / "backend" / "src").mkdir(parents=True)
    (root / "backend" / "main.py").write_text("app")
    (root / "backend" / ".env.local").write_text("SECRET=boilerplate's own")
    (root / "backend" / ".venv").mkdir()
    (root / "frontend" / "node_modules" / "react").mkdir(parents=True)
    (root / "frontend" / "index.html").write_text("<html>")
    monkeypatch.setattr(settings, "boilerplate_dir", str(root))
    return root


async def test_boilerplate_apps_are_copied_without_the_skipped_names(boilerplate, tmp_path):
    workspace = tmp_path / "storage/u7hash/p42hash/9"
    assert not devenv.has_app(_PROJECT, 9)
    await workspaces.copy_boilerplate(_PROJECT, 9)

    assert (workspace / "backend/main.py").read_text() == "app"
    assert (workspace / "frontend/index.html").exists()
    assert not (workspace / "backend/.env.local").exists()
    assert not (workspace / "backend/.venv").exists() and not (workspace / "frontend/node_modules").exists()
    assert sorted(path.name for path in workspace.iterdir()) == ["backend", "frontend"]  # no temp folders left
    assert devenv.has_app(_PROJECT, 9)


async def test_an_app_already_in_the_workspace_is_left_alone(boilerplate, tmp_path):
    workspace = tmp_path / "storage/u7hash/p42hash/9"
    (workspace / "backend").mkdir(parents=True)
    (workspace / "backend/main.py").write_text("the agent's version")
    await workspaces.copy_boilerplate(_PROJECT, 9)
    assert (workspace / "backend/main.py").read_text() == "the agent's version"
    assert (workspace / "frontend/index.html").exists()


async def test_a_symlinked_app_folder_isnt_an_app_and_gets_no_boilerplate(boilerplate, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "storage/u7hash/p42hash/9"
    workspace.mkdir(parents=True)
    (workspace / "backend").symlink_to(outside)
    (workspace / "frontend").symlink_to(outside)
    assert not devenv.has_app(_PROJECT, 9)
    with pytest.raises(OSError):  # the app folder's name is taken by the link: the copy can't be put in place
        await workspaces.copy_boilerplate(_PROJECT, 9)
    assert list(outside.iterdir()) == []


async def test_env_files_and_the_pm2_file_never_go_through_a_planted_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "project_secret_seed", "seed-for-tests")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target").write_text("host file")
    workspace = tmp_path / "storage/u7hash/p42hash/9"
    (workspace / "backend").mkdir(parents=True)
    (workspace / "frontend").symlink_to(outside)  # a whole folder pointing out
    (workspace / "ecosystem.config.cjs").symlink_to(outside / "target")  # one of our files pointing out

    with pytest.raises(paths.UnsafePath):
        await environments._write_missing_env_files(_PROJECT, 9)
    await servers._write_ecosystem_file(_PROJECT, 9)

    assert (outside / "target").read_text() == "host file"
    assert sorted(entry.name for entry in outside.iterdir()) == ["target"]
    assert "project_42_be" in (workspace / "ecosystem.config.cjs").read_text()  # the link itself got replaced


# --- the whole setup ---


@pytest.fixture
def commands(monkeypatch):
    """Fakes the commands devenv runs -- on the host (nginx) and in the project's container (installs,
    pm2), the container itself always up; records each argv (with its cwd: a container path for the
    container's) and answers pm2 jlist from `state["pm2"]` -- a list of {name, pm2_env: {status}}."""
    state = {"ran": [], "pm2": []}

    async def fake_run_command(argv, cwd=None, input_text=None, timeout_seconds=600):
        state["ran"] = [*state["ran"], (argv, cwd)]
        return json.dumps(state["pm2"]) if argv == ["pm2", "jlist"] else ""

    async def fake_run_in_container(project_id, argv, cwd=None, user=None, env=None, timeout_seconds=600):
        return await fake_run_command(argv, cwd)

    async def nothing_to_do(*args):
        pass

    async def running(project_id):
        return True

    monkeypatch.setattr(sites, "run_command", fake_run_command)
    monkeypatch.setattr(containers, "run_in_container", fake_run_in_container)
    monkeypatch.setattr(containers, "is_container_running", running)
    monkeypatch.setattr(containers, "ensure_container", nothing_to_do)
    monkeypatch.setattr(databases, "ensure_dev_access", nothing_to_do)
    return state


def _ran(commands, program):
    return [argv for argv, _ in commands["ran"] if argv[0] == program]


@pytest.fixture
def no_lock_no_health(monkeypatch):
    @asynccontextmanager
    async def fake_lock(key, ttl_seconds, wait_seconds):
        yield

    async def healthy(project_id):
        pass

    monkeypatch.setattr(environments.redis, "lock", fake_lock)
    monkeypatch.setattr(servers, "wait_until_healthy", healthy)


async def test_ensure_devenv_sets_everything_up_then_only_fills_gaps(
    boilerplate, tmp_path, monkeypatch, commands, no_lock_no_health
):
    monkeypatch.setattr(settings, "template_dir", str(tmp_path / "no-template"))
    (tmp_path / "no-template").mkdir()
    monkeypatch.setattr(settings, "project_secret_seed", "seed-for-tests")
    workspace = tmp_path / "storage/u7hash/p42hash/9"

    description = await devenv.ensure_devenv(_PROJECT, 9)

    assert description["backend"] == {"port": 8000, "process": "project_42_be", "log": "logs/be.log"}
    env_dev = workspace / "backend/.env.local"
    assert "API_PORT=8000" in env_dev.read_text() and oct(env_dev.stat().st_mode & 0o777) == "0o600"
    assert (workspace / "frontend/.env").exists() and (workspace / "ecosystem.config.cjs").exists()
    # The boilerplate here has no setup.sh: the old commands, run in the container, in the app's folder there.
    assert sorted(_ran(commands, "uv") + _ran(commands, "npm")) == [["npm", "install"], ["uv", "sync"]]
    assert ("uv", "/project/9/backend") in [(argv[0], cwd) for argv, cwd in commands["ran"]]
    names = "project_42_be,project_42_fe,project_42_redis"
    assert ["pm2", "start", "/project/9/ecosystem.config.cjs", "--only", names] in _ran(commands, "pm2")

    # Second run: env files edited since, npm finished, servers online -> only the (cheap) uv sync again.
    env_dev.write_text("EDITED=1")
    (workspace / "frontend/node_modules").mkdir()
    (workspace / "frontend/node_modules/.package-lock.json").write_text("{}")
    commands["ran"] = []
    commands["pm2"] = [
        {"name": name, "pm2_env": {"status": "online"}}
        for name in ("project_42_be", "project_42_fe", "project_42_redis")
    ]
    await devenv.ensure_devenv(_PROJECT, 9)

    assert env_dev.read_text() == "EDITED=1"
    assert _ran(commands, "uv") == [["uv", "sync"]] and _ran(commands, "npm") == []
    assert _ran(commands, "pm2") == [["pm2", "jlist"]]


async def test_each_apps_own_setup_script_runs_in_its_folder_in_the_container(
    boilerplate, tmp_path, monkeypatch, commands, no_lock_no_health
):
    monkeypatch.setattr(settings, "template_dir", str(tmp_path / "no-template"))
    (tmp_path / "no-template").mkdir()
    monkeypatch.setattr(settings, "project_secret_seed", "seed-for-tests")
    (boilerplate / "backend/setup.sh").write_text("uv sync")
    (boilerplate / "frontend/setup.sh").write_text("npm install")

    await devenv.ensure_devenv(_PROJECT, 9)

    setups = [(argv, cwd) for argv, cwd in commands["ran"] if argv == ["sh", "./setup.sh"]]
    assert sorted(cwd for _, cwd in setups) == ["/project/9/backend", "/project/9/frontend"]
    assert _ran(commands, "uv") == [] and _ran(commands, "npm") == []  # the scripts own it now


async def test_an_interrupted_setup_doesnt_look_finished(
    boilerplate, tmp_path, monkeypatch, commands, no_lock_no_health
):
    # What a crash mid-setup leaves behind: a half-done boilerplate copy and an npm install that never
    # wrote its end-of-install lockfile.
    monkeypatch.setattr(settings, "template_dir", str(tmp_path / "no-template"))
    (tmp_path / "no-template").mkdir()
    monkeypatch.setattr(settings, "project_secret_seed", "seed-for-tests")
    workspace = tmp_path / "storage/u7hash/p42hash/9"
    (workspace / ".frontend.copying-dead0000/half").mkdir(parents=True)
    (workspace / "frontend/node_modules/react").mkdir(parents=True)

    await devenv.ensure_devenv(_PROJECT, 9)

    assert not (workspace / ".frontend.copying-dead0000").exists()
    assert (workspace / "backend/main.py").exists()
    assert _ran(commands, "npm") == [["npm", "install"]]


# --- dev servers ---


async def test_only_servers_that_arent_online_are_started_fresh(monkeypatch, commands):
    monkeypatch.setattr(settings, "project_secret_seed", "seed-for-tests")
    commands["pm2"] = [
        {"name": "project_42_be", "pm2_env": {"status": "online"}},
        {"name": "project_42_fe", "pm2_env": {"status": "errored"}},
        {"name": "project_7_fe", "pm2_env": {"status": "stopped"}},  # another project's: never touched
    ]
    await servers.ensure_dev_servers(_PROJECT, 9)
    assert _ran(commands, "pm2")[-2:] == [
        ["pm2", "delete", "project_42_fe"],
        ["pm2", "start", "/project/9/ecosystem.config.cjs", "--only", "project_42_fe,project_42_redis"],
    ]


async def test_a_fresh_containers_pm2_daemon_banner_isnt_mistaken_for_the_list(monkeypatch):
    async def fresh_daemon(project_id, argv, cwd=None, user=None, env=None, timeout_seconds=600):
        return "[PM2] Spawning PM2 daemon with pm2_home=/project/.devenv/pm2\n[PM2] PM2 Successfully daemonized\n[]\n"

    async def running(project_id):
        return True

    monkeypatch.setattr(containers, "run_in_container", fresh_daemon)
    monkeypatch.setattr(containers, "is_container_running", running)
    assert [status["status"] for status in await servers.get_dev_server_statuses(42)] == ["missing", "missing"]


async def test_statuses_cover_both_servers_even_unknown_ones(commands):
    commands["pm2"] = [{"name": "project_42_be", "pm2_env": {"status": "online"}}]
    statuses = await servers.get_dev_server_statuses(42)
    assert statuses == [  # the servers' own ports, inside the container; Redis isn't one of them
        {"app": "backend", "name": "project_42_be", "status": "online", "port": 8000},
        {"app": "frontend", "name": "project_42_fe", "status": "missing", "port": 5173},
    ]


async def test_stop_only_names_this_projects_known_servers(commands):
    commands["pm2"] = [{"name": "project_42_be", "pm2_env": {"status": "online"}}]
    await servers.control_dev_servers(_PROJECT, 9, DevServerAction.STOP)
    assert [argv for argv in _ran(commands, "pm2") if argv[1] != "jlist"] == [["pm2", "stop", "project_42_be"]]


# --- dev site ---


@pytest.fixture
def nginx_dir(tmp_path, monkeypatch):
    directory = tmp_path / "nginx"
    directory.mkdir()
    monkeypatch.setattr(settings, "nginx_sites_dir", str(directory))
    monkeypatch.setattr(settings, "dev_domain", "dev.example.com")
    return directory


@pytest.mark.parametrize(("env", "dev_domain"), [("prod", ""), ("local", "dev.example.com")])
async def test_no_dev_host_means_no_site(env, dev_domain, nginx_dir, commands, monkeypatch):
    monkeypatch.setattr(settings, "env", env)
    monkeypatch.setattr(settings, "dev_domain", dev_domain)
    await sites.ensure_dev_site(_PROJECT, 9)
    assert commands["ran"] == []


async def test_a_changed_site_is_written_and_nginx_reloaded(nginx_dir, commands):
    await sites.ensure_dev_site(_PROJECT, 9)
    site_path = str(nginx_dir / "vib3-project-42.conf")
    assert [argv for argv, _ in commands["ran"]] == [
        ["sudo", "-n", "tee", site_path],
        ["sudo", "-n", "nginx", "-t"],
        ["sudo", "-n", "systemctl", "reload", "nginx"],
    ]


async def test_an_unchanged_site_is_left_alone(nginx_dir, commands):
    (nginx_dir / "vib3-project-42.conf").write_text(templates.render_template("nginx-dev.conf.tmpl", _PROJECT, 9))
    await sites.ensure_dev_site(_PROJECT, 9)
    assert commands["ran"] == []


async def test_a_site_nginx_rejects_is_removed_again(nginx_dir, monkeypatch):
    ran = []

    async def nginx_rejects(argv, cwd=None, input_text=None, timeout_seconds=600):
        ran.append(argv)
        if argv[2:] == ["nginx", "-t"]:
            raise CommandFailed("`nginx -t` exited 1")
        return ""

    monkeypatch.setattr(sites, "run_command", nginx_rejects)
    with pytest.raises(CommandFailed):
        await sites.ensure_dev_site(_PROJECT, 9)
    assert ran[-1] == ["sudo", "-n", "rm", "-f", str(nginx_dir / "vib3-project-42.conf")]


# --- running commands ---


async def test_run_command_returns_output_and_raises_with_its_tail():
    assert await run_command(["sh", "-c", "echo out; echo err >&2"]) == "out\nerr\n"
    assert await run_command(["cat"], input_text="fed in") == "fed in"
    with pytest.raises(CommandFailed) as failure:
        await run_command(["sh", "-c", "echo boom; exit 3"])
    assert failure.value.args[0].endswith("exited 3:\nboom\n")
    with pytest.raises(CommandFailed, match="timed out"):
        await run_command(["sleep", "5"], timeout_seconds=0.2)


def test_commands_never_get_our_config_or_our_venv(monkeypatch):
    monkeypatch.setenv("API_PORT", "3001")
    monkeypatch.setenv("SESSION_TOKEN_SECRET", "ours")
    monkeypatch.setenv("VIRTUAL_ENV", "/srv/vib3/.venv")
    monkeypatch.setenv("PATH", "/srv/vib3/.venv/bin:/usr/bin")
    base_env = get_base_env()
    assert "API_PORT" not in base_env and "SESSION_TOKEN_SECRET" not in base_env and "VIRTUAL_ENV" not in base_env
    assert base_env["PATH"] == "/usr/bin"


# --- status ---


async def test_status_before_and_after_the_app_is_set_up(boilerplate, monkeypatch, commands):
    async def lock_free(key):
        return False

    monkeypatch.setattr(environments.redis, "flag_exists", lock_free)
    monkeypatch.setattr(settings, "dev_domain", "dev.example.com")
    assert await devenv.get_devenv_status(_PROJECT, 9) == {
        "busy": False,
        "preview_url": None,
        "servers": [],
    }
    assert commands["ran"] == []  # no pm2 call while there's nothing to run

    await workspaces.copy_boilerplate(_PROJECT, 9)
    commands["pm2"] = [{"name": "project_42_be", "pm2_env": {"status": "online"}}]
    status = await devenv.get_devenv_status(_PROJECT, 9)
    assert status["preview_url"] == "https://p42hash.dev.example.com"
    assert status["servers"] == [
        {"app": "backend", "status": "online", "port": 8000},
        {"app": "frontend", "status": "missing", "port": 5173},
    ]


async def test_removing_dev_servers_only_touches_this_projects_known_ones(commands):
    commands["pm2"] = [
        {"name": "project_42_fe", "pm2_env": {"status": "online"}},
        {"name": "project_7_fe", "pm2_env": {"status": "online"}},
    ]
    assert await servers.remove_dev_servers(42) == ["project_42_fe"]
    assert [argv for argv in _ran(commands, "pm2") if argv[1] != "jlist"] == [["pm2", "delete", "project_42_fe"]]
    commands["pm2"] = []
    assert await servers.remove_dev_servers(42) == []  # nothing left: nothing to do
