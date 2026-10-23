# Intentions: one look at the platform as it currently is -- for the operator, through scripts/manage.py's
# `status` and the admin dashboard (controllers/admin). Read-only: it never starts, stops or writes anything,
# and never creates a conversation if there's none yet.
#
# Everything comes back as SECTIONS -- {"title", "rows": [{"label", "value"}]}, every value a string ready to
# show -- so the script prints them and the dashboard renders the very same rows. Each check that can fail
# (a dead redis, a stopped container...) says so in its own row instead of failing the rest.
#   get_platform_sections   config, the infra everything shares, and what's running across all projects
#   get_project_sections    one project: its workspace on disk, its agent's runner, its container, its dev
#                           servers, its slice of the infra
#   list_projects           every live project, its owner, and whether its container runs (the dashboard's list)
#   get_server_usage        the machine's CPU, memory and disk, as numbers
#
# The project folder is the agent's (see "PROJECT FOLDERS ARE UNTRUSTED INPUT" in CLAUDE.md): it's only ever
# looked at through utils/paths, which never follows a symlink.

import asyncio
import json
import os
import socket
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psutil
import pydash

from src.config import settings
from src.constants import (
    AGENT_API_KEY_NAMES,
    AGENT_MOCK_REPLIES,
    DEVENV_APP_DIRS,
    DEVENV_BACKEND_HEALTH_PATH,
    DEVENV_ENV_FILES,
    TurnState,
)
from src.dto.projects import Project
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import projects as projects_repo
from src.repositories import users as users_repo
from src.services import agent_api_keys, devenv
from src.services.agent import runners, storage
from src.services.devenv import addresses, servers
from src.utils import http, paths, postgres, redis
from src.utils.processes import CommandFailed, run_command


def _to_section(title: str, rows: list) -> dict:
    """[Pure] A section from (label, value) pairs; values become strings."""
    return {"title": title, "rows": [{"label": label, "value": str(value)} for label, value in rows]}


def _mask_url(url: str) -> str:
    """[Pure] The URL with its password replaced by ***, so it can be shown anywhere."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    return urlunsplit(parts._replace(netloc=parts.netloc.replace(f":{parts.password}@", ":***@")))


async def _is_listening(port: int | None, host: str = "127.0.0.1") -> bool:
    """Whether something accepts connections on host:port -- in a thread, since it's a blocking connect."""

    def _probe() -> bool:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            return probe.connect_ex((host, port)) == 0

    return port is not None and await asyncio.to_thread(_probe)


def _format_duration(seconds: float) -> str:
    """[Pure] A rough duration for reading: 40s, 12 min, 3 h 5 min."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds // 3600:.0f} h {seconds % 3600 / 60:.0f} min"


def _format_size(size: int) -> str:
    units = [("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)]
    return next((f"{size / unit:.1f} {name}" for name, unit in units if size >= unit), f"{size} B")


async def _check_health(port: int, path: str = DEVENV_BACKEND_HEALTH_PATH) -> str:
    try:
        status, body, _ = await http.request("GET", f"http://127.0.0.1:{port}{path}", timeout_seconds=3)
        return f"{status} {json.dumps(body)}"
    except Exception as error:
        return f"no answer ({type(error).__name__})"


# --- the server -----------------------------------------------------------------------------------


def get_server_usage() -> dict:
    """[Pure-ish] The machine's CPU, memory and disk (the disk STORAGE_DIR is on, where projects grow). CPU %
    is since the previous call (psutil keeps that), so the first call after start reads 0 -- polling fixes
    itself on the next one."""
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.realpath(settings.storage_dir) if os.path.isdir(settings.storage_dir) else "/")
    return {
        "cpu": {"percent": psutil.cpu_percent(interval=None), "count": psutil.cpu_count(), "load": os.getloadavg()},
        "memory": {"used": memory.total - memory.available, "total": memory.total, "percent": memory.percent},
        "disk": {"used": disk.used, "total": disk.total, "percent": disk.percent},
        "uptime_seconds": time.time() - psutil.boot_time(),
    }


# --- one project ----------------------------------------------------------------------------------


async def get_project_sections(project: Project, conversation_id: int | None = None) -> list[dict]:
    """One project's sections, about the given conversation or else its newest one. With no conversation yet,
    there's nothing on disk or running to show beyond the project itself."""
    conversations = await ai_conversations_repo.find({"project_id": project["id"]})
    conversation = (
        pydash.find(conversations, {"id": conversation_id}) if conversation_id else pydash.head(conversations)
    )
    project_section = await _get_project_section(project, conversations, conversation)
    if conversation is None:
        return [project_section]
    return [
        project_section,
        await _get_workspace_section(project, conversation["id"]),
        await _get_runner_section(project, conversation["id"]),
        await _get_container_section(project),
        await _get_servers_section(project, conversation["id"]),
        await _get_data_section(project, conversation["id"]),
    ]


async def _get_project_section(project: Project, conversations: list, conversation) -> dict:
    message_count = len(await ai_messages_repo.find(conversation["id"])) if conversation else 0
    return _to_section(
        "Project",
        [
            ("id / hash", f"{project['id']} / {project['hash']}"),
            ("name", project["name"]),
            ("stage", project["stage"]),
            ("owner (user id / hash)", f"{project['user_id']} / {project['user_hash']}"),
            # The one shown below first (marked *), then the rest.
            (
                "conversations",
                ", ".join(
                    [f"{conversation['id']}*"] * bool(conversation)
                    + [str(c["id"]) for c in conversations if not conversation or c["id"] != conversation["id"]]
                )
                or "none yet",
            ),
            ("messages in it", message_count),
        ],
    )


def _describe_entry(project_dir: str, relative_path: str, kind: str = "dir") -> str:
    """[Pure-ish] Whether a file or folder is there in the project, looked at without following symlinks: a
    planted link is reported as one, never resolved."""
    try:
        entry = paths.lstat_within(project_dir, relative_path)
    except paths.UnsafePath:
        return "behind a symlink (not followed)"
    if entry is None:
        return "-"
    if stat.S_ISLNK(entry.st_mode):
        return "a symlink (not followed)"
    is_expected_kind = stat.S_ISDIR(entry.st_mode) if kind == "dir" else stat.S_ISREG(entry.st_mode)
    return "present" if is_expected_kind else "not a " + ("folder" if kind == "dir" else "file")


async def _get_workspace_section(project: Project, conversation_id: int) -> dict:
    project_dir = devenv.get_project_dir(project)
    workspace = str(conversation_id)
    try:
        entries = await asyncio.to_thread(paths.list_within, project_dir, workspace)
    except paths.UnsafePath:
        return _to_section(
            "Workspace", [("path", f"{project_dir}/{workspace}"), ("exists", "a symlink (not followed)")]
        )
    if not entries:
        return _to_section(
            "Workspace", [("path", f"{project_dir}/{workspace}"), ("exists", "NO -- never opened, or empty")]
        )
    uploads = await storage.list_files(project, conversation_id, storage.UPLOADS_DIR)
    artifacts = await storage.list_files(project, conversation_id, storage.ARTIFACTS_DIR)
    top_level = sorted(
        f"{name}/" if stat.S_ISDIR(entry.st_mode) else f"{name}@" if stat.S_ISLNK(entry.st_mode) else name
        for name, entry in entries
    )
    npm_lock = _describe_entry(project_dir, f"{workspace}/frontend/node_modules/.package-lock.json", "file")
    node_modules = _describe_entry(project_dir, f"{workspace}/frontend/node_modules")
    return _to_section(
        "Workspace",
        [
            ("path", f"{project_dir}/{workspace}"),
            ("top level", " ".join(top_level)),
            ("app (backend/ + frontend/)", "present" if devenv.has_app(project, conversation_id) else "-"),
            *[(f"{app_dir}/", _describe_entry(project_dir, f"{workspace}/{app_dir}")) for app_dir in DEVENV_APP_DIRS],
            *[
                (output, _describe_entry(project_dir, f"{workspace}/{output}", "file"))
                for _, output in DEVENV_ENV_FILES
            ],
            ("backend deps (.venv)", _describe_entry(project_dir, f"{workspace}/backend/.venv")),
            (
                "frontend deps (npm)",
                "installed"
                if npm_lock == "present"
                else "partial (no .package-lock.json)"
                if node_modules == "present"
                else node_modules,
            ),
            ("ecosystem.config.cjs", _describe_entry(project_dir, f"{workspace}/ecosystem.config.cjs", "file")),
            ("uploads", f"{len(uploads)} ({_format_size(sum(f['size'] for f in uploads))})"),
            ("artifacts", f"{len(artifacts)}: {', '.join(f['name'] for f in artifacts)}" if artifacts else "0"),
        ],
    )


async def _get_runner_section(project: Project, conversation_id: int) -> dict:
    socket_path = Path(runners.get_socket_path(project, conversation_id))
    name = runners.get_process_name(conversation_id)
    is_container_running = await devenv.is_container_running(project["id"])
    try:
        process = pydash.find(await _list_container_pm2(project["id"]), {"name": name})
        pm2_line = _describe_pm2_process(process) if process else f"{name} (not in pm2)"
    except CommandFailed as error:
        process, pm2_line = None, f"{name} (pm2 unreadable: {error})"
    health = await runners.get_health(project, conversation_id)
    log_path = runners.get_log_path(project, conversation_id)
    log_entry = _describe_entry(
        devenv.get_project_dir(project), os.path.relpath(log_path, devenv.get_project_dir(project)), "file"
    )
    return _to_section(
        "Agent runner",
        [
            ("session", to_session_verdict(is_container_running, process, health, time.time())),
            ("socket", f"{socket_path} ({'present' if socket_path.is_socket() else '-'})"),
            ("pm2 process (in the container)", pm2_line),
            ("log", f"{log_path} ({log_entry})"),
        ],
    )


def to_session_verdict(is_container_running: bool, pm2_process: dict | None, health: dict, now: float) -> str:
    """[Pure] How the conversation's session is, in one line, from what can be seen of it: the container,
    the runner's pm2 process (None when pm2 doesn't have it), and runners.get_health. What the runner says
    wins -- if it answers, it's alive, whatever else looks off."""
    restarts = pydash.get(pm2_process, "pm2_env.restart_time", 0)
    pm2_status = pydash.get(pm2_process, "pm2_env.status")
    if health["runner"] == "up":
        return describe_turn(health, now)
    if not is_container_running:
        return "OFF -- the project's container isn't running (opening the chat starts everything)"
    if health["runner"] == "hung":
        return "HUNG -- takes connections but doesn't answer (the next open of the chat replaces it)"
    if pm2_process is None:
        return "OFF -- no runner (opening the chat starts one)"
    if pm2_status != "online":
        return f"CRASHED -- pm2 says {pm2_status}, {restarts} restarts (see its log)"
    return f"NOT ANSWERING -- pm2 has it online but nothing listens: starting, or crash-looping ({restarts} restarts)"


def describe_turn(health: dict, now: float) -> str:
    """[Pure] A runner that answers: what its session is doing (protocol.TurnState), for how long, and
    whether its last turn failed (an API error, a rate limit, too many steps...)."""
    if "turn" not in health:
        return "up (an older runner: it doesn't say what it's doing)"
    since = _format_duration(now - health["since"])
    doing = {
        TurnState.IDLE: f"idle -- resting for {since}",
        TurnState.WORKING: f"WORKING on a turn for {since}",
        TurnState.WAITING_ON_USER: f"waiting on the user's answer for {since}",
    }.get(health["turn"], f"{health['turn']} for {since}")
    last_turn = health.get("last_turn") or {}
    return doing + (f" -- its last turn FAILED ({last_turn.get('subtype')})" if last_turn.get("is_error") else "")


async def _get_container_section(project: Project) -> dict:
    name = devenv.get_container_name(project["id"])
    try:
        inspected = json.loads(await run_command(["docker", "inspect", "--type=container", name]))[0]
    except (CommandFailed, FileNotFoundError):
        inspected = None
    try:
        processes = [("pm2", _describe_pm2_process(process)) for process in await _list_container_pm2(project["id"])]
    except CommandFailed as error:
        processes = [("pm2", f"unreadable: {error}")]
    return _to_section(
        "Container",
        [
            ("name", name),
            ("state", pydash.get(inspected, "State.Status") or "none"),
            ("image (running)", pydash.get(inspected, "Config.Image") or "-"),
            ("image (recorded on the project)", pydash.get(project, "meta.devenv_image") or "- (none yet)"),
            *(processes or [("pm2", "nothing running")]),
        ],
    )


async def _list_container_pm2(project_id: int) -> list[dict]:
    """[Pure-ish] pm2's processes in the project's container (dev servers, its Redis, runners) -- none when
    the container isn't running. The output's last line is the list: a fresh container's first pm2 call
    also prints its daemon's start-up. Raises CommandFailed if pm2 can't be read."""
    if not await devenv.is_container_running(project_id):
        return []
    listing = await devenv.run_in_container(project_id, ["pm2", "jlist"])
    return json.loads(listing.strip().splitlines()[-1])


def _describe_pm2_process(process: dict) -> str:
    """[Pure-ish] One pm2 process for showing: name, status, uptime, restarts, memory."""
    pm2_env = process.get("pm2_env", {})
    is_online = pm2_env.get("status") == "online"
    uptime = f", up {_format_duration(time.time() - pm2_env['pm_uptime'] / 1000)}" if is_online else ""
    memory = _format_size(pydash.get(process, "monit.memory", 0))
    return (
        f"{process.get('name')}: {pm2_env.get('status')}{uptime}, {pm2_env.get('restart_time', 0)} restarts, {memory}"
    )


async def _get_servers_section(project: Project, conversation_id: int) -> dict:
    """The dev servers as pm2 sees them in the project's container, their ports on the host, and the site."""
    try:
        by_name = {process["name"]: process for process in await _list_container_pm2(project["id"])}
    except CommandFailed as error:
        return _to_section("Dev servers", [("pm2", f"unreadable: {error}")])
    names = servers.get_dev_server_names(project["id"])
    ports = {
        "backend": addresses.get_dev_backend_port(project["id"]),
        "frontend": addresses.get_dev_frontend_port(project["id"]),
    }

    def _describe(app: str) -> str:
        process = by_name.get(names[app])
        return _describe_pm2_process(process) if process else f"{names[app]}: not in pm2"

    async def _describe_port(app: str) -> str:
        return f"{ports[app]} ({'listening' if await _is_listening(ports[app]) else 'NOT listening'})"

    site_path = Path(settings.nginx_sites_dir, f"biveit-project-{project['id']}.conf")
    return _to_section(
        "Dev servers",
        [
            ("backend", _describe("backend")),
            ("  port", await _describe_port("backend")),
            ("  health", await _check_health(ports["backend"])),
            ("frontend", _describe("frontend")),
            ("  port", await _describe_port("frontend")),
            ("preview url", addresses.get_dev_url(project)),
            (
                "nginx site",
                f"{site_path} ({'present' if site_path.exists() else 'MISSING'})"
                if addresses.get_dev_host(project)
                else "none (no dev host here)",
            ),
            ("logs", f"{devenv.get_conversation_dir(project, conversation_id)}/logs/"),
        ],
    )


async def _get_data_section(project: Project, conversation_id: int) -> dict:
    try:
        locks = [("lock devenv", "HELD" if await redis.flag_exists(f"devenv:{conversation_id}") else "free")]
    except Exception as error:
        locks = [("platform redis", f"unreachable: {error}")]
    return _to_section(
        "Data & locks",
        [
            *locks,
            ("mongo dev db / prod db", f"db_project_dev_{project['id']} / db_project_{project['id']}"),
            ("redis", f"its own, in its container (project_{project['id']}_redis, see Container)"),
        ],
    )


# --- the whole platform ---------------------------------------------------------------------------


async def get_platform_sections() -> list[dict]:
    return [
        await _get_platform_section(),
        await _get_infra_section(),
        await _get_runners_section(),
        await _get_containers_section(),
        await _get_locks_section(),
    ]


def _describe_dir(path: str) -> str:
    resolved = Path(path).expanduser()
    return f"{resolved} ({'exists' if resolved.is_dir() else 'MISSING'})"


async def _get_platform_section() -> dict:
    local = settings.env == "local"
    return _to_section(
        "Platform",
        [
            ("env", settings.env),
            ("api", f"port {settings.api_port}, health {await _check_health(settings.api_port, '/api/debug/health')}"),
            (
                "agent",
                "MOCK replies" if AGENT_MOCK_REPLIES else "real sessions (SDK runners in the projects' containers)",
            ),
            (
                "model keys",
                agent_api_keys.describe_current_key_set(
                    {name: getattr(settings, name.lower()) for name in AGENT_API_KEY_NAMES}
                ),
            ),
            ("dev domain", "none (local: no dev sites)" if local else settings.dev_domain or "NONE -- no dev sites"),
            (
                "project dev ports",
                f"{addresses.get_dev_backend_port(0)} / {addresses.get_dev_frontend_port(0)} for every project (local)"
                if local
                else "8000 + id (backend) / 16000 + id (frontend)",
            ),
            ("storage dir", _describe_dir(settings.storage_dir)),
            ("template dir", _describe_dir(settings.template_dir)),
            ("boilerplate dir", _describe_dir(settings.boilerplate_dir)),
            ("devenv templates", _describe_dir(settings.devenv_templates_dir)),
            *([] if local else [("nginx sites dir", _describe_dir(settings.nginx_sites_dir))]),
            (
                "secrets",
                f"SESSION_TOKEN_SECRET {'set' if settings.session_token_secret else 'NOT SET'}, "
                + f"PROJECT_SECRET_SEED {'set' if settings.project_secret_seed else 'NOT SET'}",
            ),
        ],
    )


async def _get_infra_section() -> dict:
    counts = await postgres.fetchrow(
        """
        SELECT (SELECT count(*) FROM users WHERE deleted_at IS NULL) AS users,
               (SELECT count(*) FROM projects WHERE deleted_at IS NULL) AS projects,
               (SELECT count(*) FROM ai_conversations WHERE deleted_at IS NULL) AS conversations
        """
    )
    try:
        await redis.client().ping()
        platform_redis = "reachable"
    except Exception as error:
        platform_redis = f"UNREACHABLE: {error}"
    mongo_parts = urlsplit(settings.project_mongo_uri)
    is_mongo_reachable = await _is_listening(mongo_parts.port or 27017, mongo_parts.hostname or "localhost")
    return _to_section(
        "Infra",
        [
            ("platform postgres", f"{_mask_url(settings.postgres_dsn)} (reachable)"),
            (
                "  rows",
                f"{counts['users']} users, {counts['projects']} projects, {counts['conversations']} conversations",
            ),
            ("platform redis", f"{_mask_url(settings.redis_url)} ({platform_redis})"),
            (
                "projects' mongo",
                f"{_mask_url(settings.project_mongo_uri)} ({'reachable' if is_mongo_reachable else 'NOT reachable'})",
            ),
        ],
    )


async def _get_runners_section() -> dict:
    """One row per runner socket: its conversation, project, and what its session is doing -- or that
    nothing answers there (a socket left by a runner that died)."""
    sockets_root = Path(devenv.get_host_socket_dir(0)).parent  # one folder per project
    socket_paths = sorted(sockets_root.glob("*/*.sock")) if sockets_root.is_dir() else []
    conversation_ids = [int(path.stem) for path in socket_paths if path.stem.isdigit()]
    conversations = [await ai_conversations_repo.get(conversation_id) for conversation_id in conversation_ids]
    projects = [
        pydash.head(await projects_repo.find({"id": conversation["project_id"]})) if conversation else None
        for conversation in conversations
    ]
    healths = [
        await runners.get_health(project, conversation_id) if project else None
        for conversation_id, project in zip(conversation_ids, projects, strict=True)
    ]
    rows = [
        (
            f"conversation {conversation_id} (project {project['id']})",
            describe_turn(health, time.time())
            if health["runner"] == "up"
            else f"{health['runner'].upper()} (stale socket?)",
        )
        if project
        else (f"conversation {conversation_id}", "no live project behind it")
        for conversation_id, project, health in zip(conversation_ids, projects, healths, strict=True)
    ]
    return _to_section("Agent runners", rows or [("runners", "none")])


async def _get_containers_section() -> dict:
    """Each running project container, and what pm2 runs in it."""
    try:
        project_ids = await devenv.list_running_project_ids()
    except (CommandFailed, FileNotFoundError) as error:
        return _to_section("Project containers", [("docker", f"unavailable: {error}")])
    rows = [row for project_id in project_ids for row in await _get_container_rows(project_id)]
    return _to_section("Project containers", rows or [("containers", "none running")])


async def _get_container_rows(project_id: int) -> list[tuple[str, str]]:
    label = f"project {project_id}"
    try:
        processes = await _list_container_pm2(project_id)
    except CommandFailed as error:
        return [(label, f"pm2 unreadable: {error}")]
    return [(label, _describe_pm2_process(process)) for process in processes] or [(label, "nothing in pm2")]


async def _get_locks_section() -> dict:
    try:
        held_locks = sorted([key.decode() async for key in redis.client().scan_iter(match="devenv:*", count=500)])
    except Exception as error:
        held_locks = [f"unreadable: {error}"]
    return _to_section("Locks", [("held", " ".join(held_locks) or "none")])


# --- the project list -----------------------------------------------------------------------------


async def list_projects() -> list[dict]:
    """[Pure-ish] Every live project, most recently edited first: {id, name, stage, owner: {id, username},
    is_running (its container), updated_at}. Owners without a username are guests."""
    projects = await projects_repo.find({})
    try:
        running_ids = set(await devenv.list_running_project_ids())
    except (CommandFailed, FileNotFoundError):
        running_ids = set()
    owner_ids = pydash.uniq([project["user_id"] for project in projects])
    owners = dict(
        zip(owner_ids, await asyncio.gather(*[users_repo.get(owner_id) for owner_id in owner_ids]), strict=True)
    )
    return [
        {
            "id": project["id"],
            "name": project["name"],
            "stage": project["stage"],
            "owner": {"id": project["user_id"], "username": pydash.get(owners, [project["user_id"], "username"])},
            "is_running": project["id"] in running_ids,
            "updated_at": project["updated_at"],
        }
        for project in projects
    ]
