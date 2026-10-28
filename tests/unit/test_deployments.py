# Deployment tests (modules/deployments): the retry / rollback flow and its log, folding the log into a history,
# reading the workspace's repo, what the site's container gets, the build context, the prod env, and the
# nginx/certbot commands. Docker, git, certbot, Redis and the events table are faked; the real thing is
# checked by hand (docs/plans/deployments.md).

import io
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.config import settings
from src.modules import deployments
from src.modules.deployments.constants import DEPLOYMENT_ATTEMPTS, DeploymentEvent
from src.modules.deployments.services import containers, environment, images, sites, sources
from src.modules.deployments.services import deployments as deployment_service
from src.services.devenv import templates as devenv_templates
from src.utils import nginx

_PROJECT = {"id": 42, "hash": "p42hash", "user_id": 7, "user_hash": "u7hash", "meta": {}}
_BOILERPLATE_CHECKOUT = Path(__file__).resolve().parents[3] / "biveit-boilerplate"


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(settings, "env", "dev")
    monkeypatch.setattr(settings, "project_secret_seed", "seed-for-tests")


# --- the retry / rollback flow ---


@pytest.fixture
def log(monkeypatch):
    """Records the deployment log (events), and the lock, without Postgres or Redis; no waiting between
    attempts. `state["fail"]` maps a commit to how many of its ensure_release calls fail (-1 = all)."""
    state = {"events": [], "released": [], "fail": {}, "lock_held": False}

    async def insert(event):
        state["events"] = [*state["events"], event]
        return event

    async def ensure_release(project, conversation_id, commit):
        state["released"] = [*state["released"], commit]
        remaining = state["fail"].get(commit, 0)
        if remaining:
            state["fail"] = {**state["fail"], commit: remaining - 1}
            raise RuntimeError(f"{commit} broke")

    async def no_sleep(seconds):
        pass

    async def nothing(*args):
        pass

    async def delete(key):
        state["lock_held"] = False

    monkeypatch.setattr(deployment_service.events_repo, "insert", insert)
    monkeypatch.setattr(deployment_service, "ensure_release", ensure_release)
    monkeypatch.setattr(deployment_service.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(deployment_service.images, "prune_images", nothing)
    monkeypatch.setattr(deployment_service.redis, "delete", delete)
    monkeypatch.setattr(deployment_service, "log_error", lambda *args, **kwargs: None)
    return state


def _types(state):
    return [(event["type"], event["direct_object_ref"]) for event in state["events"]]


async def test_a_deployment_retries_through_transient_failures(log):
    log["fail"] = {"new": 2}
    await deployment_service._roll_out(_PROJECT, 9, "run1", "new", "old")
    assert log["released"] == ["new", "new", "new"]
    assert _types(log) == [
        (DeploymentEvent.ATTEMPT_FAILED, "new"),
        (DeploymentEvent.ATTEMPT_FAILED, "new"),
        (DeploymentEvent.SUCCEEDED, "new"),
    ]
    assert [event["value"]["attempt"] for event in log["events"][:2]] == [1, 2]


async def test_a_deployment_that_never_comes_up_rolls_back_to_what_was_live(log):
    log["fail"] = {"new": -1}
    await deployment_service._roll_out(_PROJECT, 9, "run1", "new", "old")
    assert log["released"] == ["new"] * DEPLOYMENT_ATTEMPTS + ["old"]
    assert _types(log)[-1] == (DeploymentEvent.ROLLED_BACK, "old")
    assert "new broke" in log["events"][-1]["value"]["error"]
    assert all(event["grouping_ref"] == "run1" and event["subject_ref"] == "42" for event in log["events"])


async def test_a_first_deployment_that_fails_has_nothing_to_roll_back_to(log):
    log["fail"] = {"new": -1}
    await deployment_service._roll_out(_PROJECT, 9, "run1", "new", None)
    assert log["released"] == ["new"] * DEPLOYMENT_ATTEMPTS
    assert _types(log)[-1] == (DeploymentEvent.FAILED, "new")


async def test_a_failed_rollback_is_logged_as_failed_and_the_lock_still_goes(log):
    log["fail"] = {"new": -1, "old": -1}
    log["lock_held"] = True
    await deployment_service._roll_out(_PROJECT, 9, "run1", "new", "old")
    assert _types(log)[-1] == (DeploymentEvent.FAILED, "old")
    assert log["lock_held"] is False


@pytest.fixture
def deploy_world(monkeypatch, log):
    """deploy()'s surroundings: an app in the workspace, the dev container, the repo (`commit`) and docker
    (`live`) faked; the lock is a flag; the background half only recorded."""

    async def nothing(*args):
        pass

    async def set_if_absent(key, ttl):
        if log["lock_held"]:
            return False
        log["lock_held"] = True
        return True

    async def commit_workspace(project, conversation_id):
        if log.get("commit_fails"):
            raise RuntimeError("git broke")
        return "c0ffee"

    async def get_live_release(project_id):
        return {"commit": "old", "deployed_at": "then", "running": True}

    monkeypatch.setattr(deployment_service.devenv, "has_app", lambda project, conversation_id: True)
    monkeypatch.setattr(deployment_service.devenv, "ensure_workspace", nothing)
    monkeypatch.setattr(deployment_service.devenv, "ensure_container", nothing)
    monkeypatch.setattr(deployment_service.sources, "commit_workspace", commit_workspace)
    monkeypatch.setattr(deployment_service.containers, "get_live_release", get_live_release)
    monkeypatch.setattr(deployment_service.redis, "set_if_absent", set_if_absent)
    monkeypatch.setattr(deployment_service, "spawn", lambda coro: log.update(spawned=True) or coro.close())


async def test_deploy_pins_a_commit_and_hands_the_rest_to_the_background(deploy_world, log):
    result = await deployments.deploy(_PROJECT, 9)
    assert result["commit"] == "c0ffee" and log["spawned"]
    assert _types(log) == [(DeploymentEvent.STARTED, "c0ffee")]
    assert log["events"][0]["value"] == {"previous_commit": "old"}
    with pytest.raises(deployments.DeploymentInProgress):
        await deployments.deploy(_PROJECT, 9)


async def test_deploy_lets_go_of_the_lock_when_it_cant_even_start(deploy_world, log):
    log["commit_fails"] = True
    with pytest.raises(RuntimeError, match="git broke"):
        await deployments.deploy(_PROJECT, 9)
    assert log["lock_held"] is False and not log["events"]


async def test_there_is_nothing_to_deploy_before_the_app_exists(deploy_world, monkeypatch):
    monkeypatch.setattr(deployment_service.devenv, "has_app", lambda project, conversation_id: False)
    with pytest.raises(deployments.NothingToDeploy):
        await deployments.deploy(_PROJECT, 9)


# --- the history ---


def _event(event_type, run_id, commit, minute, value=None):
    return {
        "type": event_type,
        "grouping_ref": run_id,
        "direct_object_ref": commit,
        "created_at": datetime(2026, 10, 26, tzinfo=UTC) + timedelta(minutes=minute),
        "value": value or {},
    }


async def test_the_history_folds_each_run_newest_first(monkeypatch):
    events = [
        _event(DeploymentEvent.STARTED, "r1", "a", 0),
        _event(DeploymentEvent.SUCCEEDED, "r1", "a", 1),
        _event(DeploymentEvent.STARTED, "r2", "b", 2),
        _event(DeploymentEvent.ATTEMPT_FAILED, "r2", "b", 3, {"attempt": 1, "error": "boom"}),
        _event(DeploymentEvent.ROLLED_BACK, "r2", "a", 4, {"error": "boom"}),
        _event(DeploymentEvent.STARTED, "r3", "c", 5),
    ]

    async def find(criteria):
        assert criteria == {"subject_type": "project", "subject_ref": "42", "grouping_type": "deployment"}
        return events

    async def not_deploying(key):
        return False

    monkeypatch.setattr(deployment_service.events_repo, "find", find)
    monkeypatch.setattr(deployment_service.redis, "flag_exists", not_deploying)
    newest, rolled_back, succeeded = await deployments.list_deployments(_PROJECT)
    assert (newest["id"], newest["outcome"], newest["finished_at"]) == ("r3", "interrupted", None)
    assert (rolled_back["commit"], rolled_back["outcome"], rolled_back["live_commit"]) == ("b", "rolled_back", "a")
    assert rolled_back["attempts"][0]["error"] == "boom" and rolled_back["error"] == "boom"
    assert (succeeded["outcome"], succeeded["live_commit"]) == ("succeeded", "a")


def test_a_run_still_going_is_running_and_a_failed_one_leaves_nothing_live():
    running = deployment_service._fold_deployment([_event(DeploymentEvent.STARTED, "r", "a", 0)], True)
    assert running["outcome"] == "running"
    failed = deployment_service._fold_deployment(
        [_event(DeploymentEvent.STARTED, "r", "a", 0), _event(DeploymentEvent.FAILED, "r", "a", 1, {"error": "x"})],
        False,
    )
    assert (failed["outcome"], failed["live_commit"], failed["error"]) == ("failed", None, "x")


# --- the workspace's repo ---


@pytest.fixture
def repo(monkeypatch):
    state = {"running": True, "output": ""}

    async def is_container_running(project_id):
        return state["running"]

    async def run_in_container(project_id, argv, cwd=None, user=None, env=None, timeout_seconds=600):
        state["ran"] = (argv, cwd, env)
        return state["output"]

    monkeypatch.setattr(sources.devenv, "is_container_running", is_container_running)
    monkeypatch.setattr(sources.devenv, "run_in_container", run_in_container)
    return state


async def test_changes_since_a_commit_are_a_new_head_or_anything_uncommitted(repo):
    repo["output"] = "abc\n"
    assert await sources.has_changes_since(_PROJECT, 9, "abc") is False
    assert repo["ran"][1] == "/project/9"
    assert await sources.has_changes_since(_PROJECT, 9, "def") is True
    repo["output"] = "abc\n M backend/main.py\n"
    assert await sources.has_changes_since(_PROJECT, 9, "abc") is True
    repo["output"] = ""  # no repo yet
    assert await sources.has_changes_since(_PROJECT, 9, "abc") is True
    assert await sources.has_changes_since(_PROJECT, 9, None) is True


async def test_changes_are_unknown_while_the_dev_container_sleeps(repo):
    repo["running"] = False
    assert await sources.has_changes_since(_PROJECT, 9, "abc") is None


async def test_committing_the_workspace_returns_the_commit_and_leaves_the_repos_config_alone(repo):
    repo["output"] = "[main 1a2b3c] Deploy\n1a2b3c4d\n"
    assert await sources.commit_workspace(_PROJECT, 9) == "1a2b3c4d"
    argv, cwd, env = repo["ran"]
    assert cwd == "/project/9" and env["GIT_COMMITTER_NAME"] == "biveit"
    assert "git config" not in argv[-1]


# --- the site's container ---


def test_a_site_container_gets_no_host_folder_and_publishes_on_localhost_only():
    argv = containers._to_run_argv(_PROJECT, "biveit-dev-site-42:abc", "abc", ["JWT_SECRET", "MONGO_URI"])
    assert [option for option in argv if option.startswith("--volume=")] == ["--volume=biveit-dev-site-42-data:/data"]
    assert [option for option in argv if option.startswith("--publish=")] == ["--publish=127.0.0.1:24042:8080"]
    assert {"--init", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--restart=unless-stopped"} <= set(argv)
    assert "--label=biveit.commit=abc" in argv and argv[-1] == "biveit-dev-site-42:abc"
    assert not [option for option in argv if "privileged" in option or "docker.sock" in option]
    # Env vars by name only: their values never reach the command line.
    assert argv[argv.index("--env") + 1] == "JWT_SECRET" and not [
        option for option in argv if "=" in option and "SECRET" in option
    ]


# --- the build ---


def _tar(entries):
    """A tar archive of (name, kind, payload): kind "file" (payload = content) or "symlink" (payload = target)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, payload
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_the_apps_go_into_the_build_container_owned_by_its_user_with_links_left_as_links():
    archive = images._to_build_archive(
        _tar([("backend/main.py", "file", b"app"), ("backend/leak", "symlink", "/etc/passwd")])
    )
    with tarfile.open(fileobj=io.BytesIO(archive)) as packed:
        members = {member.name: member for member in packed.getmembers()}
        assert list(members) == ["app", "app/backend/main.py", "app/backend/leak"]
        assert members["app"].isdir() and {(m.uid, m.gid) for m in members.values()} == {(10001, 10001)}
        assert packed.extractfile("app/backend/main.py").read() == b"app"
        assert members["app/backend/leak"].issym() and members["app/backend/leak"].linkname == "/etc/passwd"


def test_the_build_container_is_sandboxed_like_a_dev_container():
    argv = images._to_build_argv("biveit-dev-site-42-build")
    assert {"--init", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--network=biveit-projects"} <= set(argv)
    assert "--user=10001:10001" in argv and "--memory=2g" in argv and "--memory-swap=2g" in argv
    assert not [option for option in argv if option.startswith(("--volume", "--mount", "--publish", "--privileged"))]
    assert argv[-3:-1] == ["bash", "-c"] and "npm run build" in argv[-1]


async def test_a_failed_build_still_cleans_up_its_container_and_image(monkeypatch):
    state = {"ran": []}

    async def fake_run_command(argv, cwd=None, input_text=None, timeout_seconds=600, env=None):
        state["ran"] = [*state["ran"], argv]
        if argv[:2] == ["docker", "start"]:
            raise images.CommandFailed("npm ci exited 1")
        return ""

    async def nothing():
        pass

    monkeypatch.setattr(images, "run_command", fake_run_command)
    monkeypatch.setattr(images.devenv, "ensure_network", nothing)
    with pytest.raises(images.CommandFailed):
        await images.build_image(42, "biveit-dev-site-42:abc", _tar([("backend/main.py", "file", b"app")]))
    assert [argv[:3] for argv in state["ran"]] == [
        ["docker", "rm", "--force"],
        ["docker", "create", "--name=biveit-dev-site-42-build"],
        ["docker", "cp", "-"],
        ["docker", "start", "--attach"],
        ["docker", "rm", "--force"],
        ["docker", "image", "rm"],
    ]
    assert state["ran"][-1] == ["docker", "image", "rm", "biveit-dev-site-42-build:abc"]


# --- the prod env ---


@pytest.mark.skipif(
    not _BOILERPLATE_CHECKOUT.is_dir(), reason="needs the biveit-boilerplate checkout next to this repo"
)
def test_the_prod_env_has_its_own_secrets_and_database(monkeypatch):
    monkeypatch.setattr(settings, "boilerplate_dir", str(_BOILERPLATE_CHECKOUT))
    monkeypatch.setattr(settings, "project_mongo_host", "10.213.0.1:27017")
    site_env = environment.render_site_env(_PROJECT, ["https://shop.example.com"])
    dev_env = devenv_templates.render_template_file(str(_BOILERPLATE_CHECKOUT / "backend/.env.local.tmpl"), _PROJECT, 9)
    assert site_env["ENV"] == "prod" and site_env["API_RELOAD"] == "false" and site_env["API_PORT"] == "8000"
    assert site_env["MONGO_DB"] == "db_project_42" and site_env["MONGO_URI"].startswith("mongodb://site_42:")
    assert site_env["CORS_ALLOWED_ORIGINS"] == "https://shop.example.com"
    assert site_env["JWT_SECRET"] and site_env["JWT_SECRET"] not in dev_env
    assert site_env["ADMIN_TOKEN"] not in dev_env


# --- domains, nginx and certificates ---


async def test_domains_are_normalized_and_anything_else_refused(monkeypatch):
    state = {"replaced": []}

    async def replace(project_id, domains):
        state["replaced"] = [*state["replaced"], (project_id, domains)]

    monkeypatch.setattr(sites.site_domains_repo, "replace", replace)
    assert await deployments.set_domains(_PROJECT, [" Shop.Example.com. ", "shop.example.com", "www.shop.io"]) == [
        "shop.example.com",
        "www.shop.io",
    ]
    assert state["replaced"] == [(42, ["shop.example.com", "www.shop.io"])]
    for bad in ["shop.example.com; include /etc/passwd", "localhost", "-x.com", "a..com", "*.example.com"]:
        with pytest.raises(deployments.InvalidDomain):
            await deployments.set_domains(_PROJECT, [bad])


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The host's commands (sudo certbot / tee / nginx) faked and recorded; sites in a temp folder."""
    state = {"ran": []}

    async def fake_run_command(argv, cwd=None, input_text=None, timeout_seconds=600, env=None):
        state["ran"] = [*state["ran"], argv]
        state["written"] = input_text if argv[2:3] == ["tee"] else state.get("written")
        return ""

    monkeypatch.setattr(settings, "nginx_sites_dir", str(tmp_path))
    monkeypatch.setattr(sites, "run_command", fake_run_command)
    monkeypatch.setattr(nginx, "run_command", fake_run_command)
    return state


async def test_each_domain_gets_its_certificate_then_a_server_block(host):
    await sites.ensure_site(_PROJECT, ["shop.example.com", "www.shop.io"])
    certbot_runs = [argv for argv in host["ran"] if "certbot" in argv]
    assert [argv[argv.index("certonly") :][3] for argv in certbot_runs] == [
        "--cert-name=shop.example.com",
        "--cert-name=www.shop.io",
    ]
    assert host["ran"][len(certbot_runs)][:3] == ["sudo", "-n", "tee"]
    site_text = next(argv for argv in host["ran"] if argv[2] == "tee")
    assert site_text[3].endswith("biveit-site-42.conf")


async def test_a_subdomain_of_ours_uses_the_wildcard_certificate(host, monkeypatch):
    monkeypatch.setattr(settings, "prod_domain", "sites.biveit.app")
    domains = ["p42hash.sites.biveit.app", "deep.p42hash.sites.biveit.app", "shop.example.com"]
    await sites.ensure_site(_PROJECT, domains)
    certbot_runs = [argv for argv in host["ran"] if "certbot" in argv]
    # The wildcard reaches one label down only, so the deeper one needs its own.
    assert [argv[argv.index("certonly") :][3] for argv in certbot_runs] == [
        "--cert-name=deep.p42hash.sites.biveit.app",
        "--cert-name=shop.example.com",
    ]
    assert host["written"].count("/etc/letsencrypt/live/sites.biveit.app/fullchain.pem") == 1
    assert "/etc/letsencrypt/live/shop.example.com/fullchain.pem" in host["written"]
    # HTTP/1.1 to the container, or its gzip stays off.
    assert host["written"].count("proxy_http_version 1.1;") == len(domains)


async def test_without_domains_there_is_no_site(host, tmp_path):
    (tmp_path / "biveit-site-42.conf").write_text("old")
    await sites.ensure_site(_PROJECT, [])
    assert ["sudo", "-n", "rm", "-f", str(tmp_path / "biveit-site-42.conf")] in host["ran"]
    assert not [argv for argv in host["ran"] if "certbot" in argv]
