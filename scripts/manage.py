# Intentions: running the platform from the command line, all in one place. Uses this backend's own config
# (.env.local, or .env.<ENV>), so run it from the repo root, on the box and in the env you mean.
#
#   uv run python -m scripts.manage status                                   # the whole platform
#   uv run python -m scripts.manage status <project id> [--conversation <id>]  # one project
#   uv run python -m scripts.manage stop <project id>                        # turn a project off
#   uv run python -m scripts.manage keys [--env prod]                        # the model key sets
#   uv run python -m scripts.manage keys use <set> [--env prod] [--apply]    # switch to one
#
# status is read-only, and never creates a conversation if there's none yet. The system view: platform
# config, the infra everything shares, and what's running across all projects. The project view: its
# workspace on disk, its agent's runner (services/agent/runners), its container, its dev servers, and its
# own slice of the infra. Each check that can fail (a dead redis, a stopped container...) says so in its own section
# instead of stopping the rest.
#
# stop turns off everything running for a project -- each conversation's runner, its dev servers, then its
# container -- via services/projects.close_project. Nothing on disk is touched; opening the project brings
# it all back. Safe to run again.
#
# keys: the model key sets are .env-format files in AGENT_API_KEYS_DIR, one per set (its name is the file
# name). use writes a set's variables into .env.<env>, replacing every AGENT_API_KEY_NAMES line there; a
# dry run unless --apply. The backend reads them at start, so restart it afterwards.

import argparse
import asyncio
import json
import os
import re
import socket
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pydash
from dotenv import dotenv_values

from src.config import settings
from src.constants import (
    AGENT_API_KEY_NAMES,
    AGENT_API_KEYS_DIR,
    AGENT_MOCK_REPLIES,
    DEVENV_APP_DIRS,
    DEVENV_BACKEND_HEALTH_PATH,
    DEVENV_ENV_FILES,
)
from src.dto.projects import Project
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import projects as projects_repo
from src.services import devenv
from src.services import projects as projects_service
from src.services.agent import runners, storage
from src.services.devenv import addresses, servers
from src.utils import http, postgres, redis
from src.utils.processes import CommandFailed, run_command


def _print_section(title: str, rows: list[tuple[str, object]]) -> None:
    width = max([len(label) for label, _ in rows], default=0)
    print(f"\n== {title} ==")
    print("\n".join(f"  {label.ljust(width)}  {value}" for label, value in rows))


def _mask_url(url: str) -> str:
    """[Pure] The URL with its password replaced by ***, so the output can be pasted anywhere."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
    return urlunsplit(parts._replace(netloc=netloc))


def _is_listening(port: int | None, host: str = "127.0.0.1") -> bool:
    if port is None:
        return False
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def _format_size(size: int) -> str:
    units = [("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)]
    return next((f"{size / unit:.1f} {name}" for name, unit in units if size >= unit), f"{size} B")


# --- sections --------------------------------------------------------------------------------------


async def _report_project(project: Project, conversations: list[dict], conversation: dict | None) -> None:
    message_count = len(await ai_messages_repo.find(conversation["id"])) if conversation else 0
    _print_section(
        "Project",
        [
            ("id / hash", f"{project['id']} / {project['hash']}"),
            ("name", project["name"]),
            ("stage", project["stage"]),
            ("owner (user id / hash)", f"{project['user_id']} / {project['user_hash']}"),
            # The one reported on below first (marked *), then the rest.
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


async def _report_directory(project: Project, conversation_id: int) -> None:
    workspace = Path(devenv.get_conversation_dir(project, conversation_id))
    if not workspace.is_dir():
        _print_section("Workspace", [("path", workspace), ("exists", "NO -- never opened, or seeded elsewhere")])
        return
    uploads = await storage.list_files(project, conversation_id, storage.UPLOADS_DIR)
    artifacts = await storage.list_files(project, conversation_id, storage.ARTIFACTS_DIR)
    top_level = sorted(f"{entry.name}/" if entry.is_dir() else entry.name for entry in workspace.iterdir())
    frontend = workspace / "frontend"
    _print_section(
        "Workspace",
        [
            ("path", workspace),
            ("top level", " ".join(top_level)),
            ("app (backend/ + frontend/)", "present" if devenv.has_app(project, conversation_id) else "-"),
            *[(f"{app_dir}/", "present" if (workspace / app_dir).is_dir() else "-") for app_dir in DEVENV_APP_DIRS],
            *[(output, "present" if (workspace / output).exists() else "-") for _, output in DEVENV_ENV_FILES],
            ("backend deps (.venv)", "present" if (workspace / "backend/.venv").is_dir() else "-"),
            (
                "frontend deps (npm)",
                "installed"
                if (frontend / "node_modules/.package-lock.json").is_file()
                else "partial (no .package-lock.json)"
                if (frontend / "node_modules").is_dir()
                else "-",
            ),
            ("ecosystem.config.cjs", "present" if (workspace / "ecosystem.config.cjs").is_file() else "-"),
            ("uploads", f"{len(uploads)} ({_format_size(sum(f['size'] for f in uploads))})"),
            ("artifacts", f"{len(artifacts)}: {', '.join(f['name'] for f in artifacts)}" if artifacts else "0"),
        ],
    )


async def _report_runner(project: Project, conversation_id: int) -> None:
    socket_path = Path(runners.get_socket_path(project, conversation_id))
    log_path = Path(runners.get_log_path(project, conversation_id))
    _print_section(
        "Agent runner",
        [
            ("up (answers on its socket)", "yes" if await runners.is_session_up(project, conversation_id) else "no"),
            ("socket", f"{socket_path} ({'present' if socket_path.exists() else '-'})"),
            ("pm2 process (in the container)", await _describe_runner_process(project, conversation_id)),
            ("log", f"{log_path} ({'present' if log_path.exists() else '-'})"),
        ],
    )


async def _report_container(project: Project) -> None:
    name = devenv.get_container_name(project["id"])
    try:
        inspected = json.loads(await run_command(["docker", "inspect", "--type=container", name]))[0]
    except (CommandFailed, FileNotFoundError):
        inspected = None
    try:
        processes = [("pm2", _describe_pm2_process(process)) for process in await _list_container_pm2(project["id"])]
    except CommandFailed as error:
        processes = [("pm2", f"unreadable: {error}")]
    _print_section(
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
    """[Pure-ish] One pm2 process for printing: name, status, uptime, restarts, memory."""
    pm2_env = process.get("pm2_env", {})
    is_online = pm2_env.get("status") == "online"
    uptime = f", up {(time.time() * 1000 - pm2_env['pm_uptime']) / 60000:.0f} min" if is_online else ""
    memory = _format_size(pydash.get(process, "monit.memory", 0))
    return (
        f"{process.get('name')}: {pm2_env.get('status')}{uptime}, {pm2_env.get('restart_time', 0)} restarts, {memory}"
    )


async def _describe_runner_process(project: Project, conversation_id: int) -> str:
    """The runner's pm2 process in the project's container and its state -- or why there's none."""
    name = runners.get_process_name(conversation_id)
    if not await devenv.is_container_running(project["id"]):
        return f"{name} (container not running)"
    try:
        process = pydash.find(await _list_container_pm2(project["id"]), {"name": name})
    except CommandFailed as error:
        return f"{name} (pm2 unreadable: {error})"
    return _describe_pm2_process(process) if process else f"{name} (not in pm2)"


async def _report_servers(project: Project, conversation_id: int) -> None:
    """The dev servers as pm2 sees them in the project's container, their ports on the host, and the site."""
    try:
        by_name = {process["name"]: process for process in await _list_container_pm2(project["id"])}
    except CommandFailed as error:
        _print_section("Dev servers", [("pm2", f"unreadable: {error}")])
        return
    names = servers.get_dev_server_names(project["id"])
    ports = {
        "backend": addresses.get_dev_backend_port(project["id"]),
        "frontend": addresses.get_dev_frontend_port(project["id"]),
    }

    def _describe(app: str) -> str:
        process = by_name.get(names[app])
        return _describe_pm2_process(process) if process else f"{names[app]}: not in pm2"

    health = await _check_health(ports["backend"])
    dev_host = addresses.get_dev_host(project)
    site_path = Path(settings.nginx_sites_dir, f"biveit-project-{project['id']}.conf")
    _print_section(
        "Dev servers",
        [
            ("backend", _describe("backend")),
            ("  port", f"{ports['backend']} ({'listening' if _is_listening(ports['backend']) else 'NOT listening'})"),
            ("  health", health),
            ("frontend", _describe("frontend")),
            ("  port", f"{ports['frontend']} ({'listening' if _is_listening(ports['frontend']) else 'NOT listening'})"),
            ("preview url", addresses.get_dev_url(project)),
            (
                "nginx site",
                (f"{site_path} ({'present' if site_path.exists() else 'MISSING'})")
                if dev_host
                else "none (no dev host here)",
            ),
            ("logs", f"{devenv.get_conversation_dir(project, conversation_id)}/logs/"),
        ],
    )


async def _check_health(port: int, path: str = DEVENV_BACKEND_HEALTH_PATH) -> str:
    try:
        status, body, _ = await http.request("GET", f"http://127.0.0.1:{port}{path}", timeout_seconds=3)
        return f"{status} {json.dumps(body)}"
    except Exception as error:
        return f"no answer ({type(error).__name__})"


async def _report_infra(project: Project, conversation_id: int) -> None:
    async def _redis_flag(key: str) -> str:
        return "HELD" if await redis.flag_exists(key) else "free"

    try:
        locks = [("lock devenv", await _redis_flag(f"devenv:{conversation_id}"))]
    except Exception as error:
        locks = [("platform redis", f"unreachable: {error}")]
    _print_section(
        "Data & locks",
        [
            *locks,
            ("mongo dev db / prod db", f"db_project_dev_{project['id']} / db_project_{project['id']}"),
            ("redis", f"its own, in its container (project_{project['id']}_redis, see Container)"),
        ],
    )


# --- the system view --------------------------------------------------------------------------------


def _describe_dir(path: str) -> str:
    resolved = Path(path).expanduser()
    return f"{resolved} ({'exists' if resolved.is_dir() else 'MISSING'})"


async def _report_platform() -> None:
    local = settings.env == "local"
    _print_section(
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
                _describe_current_key_set({name: getattr(settings, name.lower()) for name in AGENT_API_KEY_NAMES}),
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


async def _report_shared_infra() -> None:
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
    mongo_reachable = _is_listening(mongo_parts.port or 27017, mongo_parts.hostname or "localhost")
    _print_section(
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
                f"{_mask_url(settings.project_mongo_uri)} ({'reachable' if mongo_reachable else 'NOT reachable'})",
            ),
        ],
    )


async def _report_running() -> None:
    """What's running for us across all projects: the agents' runners (by their sockets), each running
    project container with what pm2 runs in it, and the locks held in redis."""
    try:
        container_lines = [
            line
            for project_id in await devenv.list_running_project_ids()
            for line in await _list_container_lines(project_id)
        ]
    except (CommandFailed, FileNotFoundError) as error:
        container_lines = [f"  docker unavailable: {error}"]
    try:
        held_locks = sorted([key.decode() async for key in redis.client().scan_iter(match="devenv:*", count=500)])
    except Exception as error:
        held_locks = [f"unreadable: {error}"]
    print("\n== agent runners ==")
    print("\n".join(await _list_runners()) or "  none")
    print("\n== project containers (pm2 inside) ==")
    print("\n".join(container_lines) or "  none running")
    _print_section("Locks", [("held", " ".join(held_locks) or "none")])


async def _list_container_lines(project_id: int) -> list[str]:
    """[Pure-ish] A running project container and each pm2 process in it, as printed lines."""
    try:
        processes = [f"    {_describe_pm2_process(process)}" for process in await _list_container_pm2(project_id)]
    except CommandFailed as error:
        processes = [f"    pm2 unreadable: {error}"]
    return [f"  project {project_id} ({devenv.get_container_name(project_id)})", *(processes or ["    nothing in pm2"])]


async def _list_runners() -> list[str]:
    """One line per runner socket: its conversation, project, and whether anything answers there (a socket
    left by a runner that died doesn't)."""
    sockets_root = Path(devenv.get_host_socket_dir(0)).parent  # one folder per project
    socket_paths = sorted(sockets_root.glob("*/*.sock")) if sockets_root.is_dir() else []
    conversation_ids = [int(path.stem) for path in socket_paths if path.stem.isdigit()]
    conversations = [await ai_conversations_repo.get(conversation_id) for conversation_id in conversation_ids]
    projects = [
        pydash.head(await projects_repo.find({"id": conversation["project_id"]})) if conversation else None
        for conversation in conversations
    ]
    return [
        f"  conversation {conversation_id} -- project {project['id']}: "
        + ("up" if await runners.is_session_up(project, conversation_id) else "NOT answering (stale socket)")
        if project
        else f"  conversation {conversation_id} -- no live project behind it"
        for conversation_id, project in zip(conversation_ids, projects, strict=True)
    ]


async def _report_project_status(project_id: int, conversation_id: int | None) -> None:
    project = pydash.head(await projects_repo.find({"id": project_id}))
    if project is None:
        raise SystemExit(f"no live project {project_id}")
    conversations = await ai_conversations_repo.find({"project_id": project["id"]})
    conversation = (
        pydash.find(conversations, {"id": conversation_id}) if conversation_id else pydash.head(conversations)
    )
    await _report_project(project, conversations, conversation)
    if conversation is None:
        print("\n(no conversation yet -- nothing on disk or running to report)")
        return
    await _report_directory(project, conversation["id"])
    await _report_runner(project, conversation["id"])
    await _report_container(project)
    await _report_servers(project, conversation["id"])
    await _report_infra(project, conversation["id"])


# --- stop ------------------------------------------------------------------------------------------


async def _stop_project(project_id: int) -> None:
    project = pydash.head(await projects_repo.find({"id": project_id}))
    if project is None:
        raise SystemExit(f"no live project {project_id}")
    stopped = await projects_service.close_project(project)
    print(
        "\n".join(
            [
                *[
                    f"conversation {conversation_id}: runner {'stopped' if session['runner'] else 'was not running'}"
                    for conversation_id, session in stopped["sessions"].items()
                ],
                f"dev servers removed from pm2: {', '.join(stopped['dev_servers']) or 'none'}",
                f"container {'stopped' if stopped['container'] else 'was not running'}",
            ]
        )
    )


# --- keys ------------------------------------------------------------------------------------------


def _read_env_file(path: Path) -> dict[str, str]:
    """[Pure-ish] An .env-format file's variables (a bare `NAME` counts as empty), or none if there's no file."""
    return {name: value or "" for name, value in dotenv_values(path).items()} if path.is_file() else {}


def _find_key_sets() -> dict[str, dict[str, str]]:
    """[Pure-ish] The key sets in AGENT_API_KEYS_DIR by name (the file name, minus a .env suffix), each as its
    variables. Hidden files (an editor's swap file...) are skipped."""
    folder = Path(AGENT_API_KEYS_DIR)
    paths = (
        sorted(path for path in folder.iterdir() if path.is_file() and not path.name.startswith("."))
        if folder.is_dir()
        else []
    )
    return {path.name.removesuffix(".env"): _read_env_file(path) for path in paths}


def _check_key_set(key_set: dict[str, str]) -> list[str]:
    """[Pure] What's wrong with a key set, if anything: only AGENT_API_KEY_NAMES, nothing empty, and a key --
    Anthropic's API key, or another endpoint's URL and token together."""
    unknown = [name for name in key_set if name not in AGENT_API_KEY_NAMES]
    empty = [name for name, value in key_set.items() if not value]
    has_key = bool(key_set.get("ANTHROPIC_API_KEY")) or (
        bool(key_set.get("ANTHROPIC_BASE_URL")) and bool(key_set.get("ANTHROPIC_AUTH_TOKEN"))
    )
    return [
        *[f"{name} isn't a model key variable (only {', '.join(AGENT_API_KEY_NAMES)})" for name in unknown],
        *[f"{name} is empty" for name in empty],
        *([] if has_key else ["needs ANTHROPIC_API_KEY, or ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN"]),
    ]


def to_env_text_with_key_set(env_text: str, key_set: dict[str, str]) -> str:
    """[Pure] An env file's text with its model keys replaced by key_set's: every AGENT_API_KEY_NAMES line
    goes (so nothing of the previous provider lingers -- a base URL left behind would send the new key to the
    old endpoint), the set's lines are appended, everything else stays as it was."""
    key_line = re.compile(rf"^\s*(export\s+)?({'|'.join(AGENT_API_KEY_NAMES)})\s*=")
    kept_lines = [line for line in env_text.splitlines() if not key_line.match(line)]
    return "\n".join([*kept_lines, *[f"{name}={value}" for name, value in key_set.items()]]) + "\n"


def _mask_secret(name: str, value: str) -> str:
    """[Pure] A key variable's value for printing: the base URL as is, keys down to their last 4 characters."""
    return value if name == "ANTHROPIC_BASE_URL" or not value else f"...{value[-4:]}"


def _describe_key_set(key_set: dict[str, str]) -> str:
    return ", ".join(f"{name}={_mask_secret(name, value)}" for name, value in key_set.items()) or "nothing"


def _describe_current_key_set(current_keys: dict[str, str]) -> str:
    """[Pure-ish] Which key set the given key variables are (missing = empty), for printing."""
    set_keys = {name: current_keys.get(name) or "" for name in AGENT_API_KEY_NAMES}
    matching = [
        name
        for name, key_set in _find_key_sets().items()
        if set_keys == {key: key_set.get(key, "") for key in AGENT_API_KEY_NAMES}
    ]
    if not any(set_keys.values()):
        return "NONE SET -- agents can't start"
    described = _describe_key_set({name: value for name, value in set_keys.items() if value})
    return f"{matching[0]} ({described})" if matching else f"not one of {AGENT_API_KEYS_DIR}/ ({described})"


def _write_env_file(path: Path, text: str) -> None:
    """Replace the env file in one go, keeping its permissions: a temp file created with the original's mode
    (so the secrets in it are never more readable than before), then renamed over it."""
    temp_path = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(path.stat().st_mode))
    with os.fdopen(fd, "w") as temp_file:
        temp_file.write(text)
    os.replace(temp_path, path)


def _list_keys(env: str) -> None:
    """Print the key sets (any problems with them), which one .env.<env> has, and key files others can read."""
    key_sets = _find_key_sets()
    env_path = Path(f".env.{env}")
    keys_dir = Path(AGENT_API_KEYS_DIR)
    readable_by_others = (
        [path.name for path in [keys_dir, *keys_dir.glob("*")] if stat.S_IMODE(path.stat().st_mode) & 0o077]
        if keys_dir.is_dir()
        else []
    )
    set_lines = [
        f"  {name}: {_describe_key_set(key_set)}" + "".join(f"  [{problem}]" for problem in _check_key_set(key_set))
        for name, key_set in key_sets.items()
    ]
    current = _describe_current_key_set(_read_env_file(env_path)) if env_path.is_file() else "no such file"
    print(
        "\n".join(
            [
                f"key sets in {AGENT_API_KEYS_DIR}/:"
                if key_sets
                else f"no key sets: one .env file per set in {AGENT_API_KEYS_DIR}/",
                *set_lines,
                f"{env_path}: {current}",
                *(
                    [f"readable by others (chmod 600, the folder 700): {', '.join(readable_by_others)}"]
                    if readable_by_others
                    else []
                ),
            ]
        )
    )


def _use_keys(name: str, env: str, is_applying: bool) -> None:
    key_set = _find_key_sets().get(name)
    env_path = Path(f".env.{env}")
    if key_set is None:
        raise SystemExit(f"no key set {name!r} in {AGENT_API_KEYS_DIR}/ (see: keys)")
    if problems := _check_key_set(key_set):
        raise SystemExit(f"key set {name!r}: " + "; ".join(problems))
    if not env_path.is_file():
        raise SystemExit(f"no {env_path} here -- run from the repo root, and check --env")
    print(
        f"{env_path}: {_describe_current_key_set(_read_env_file(env_path))}\n  -> {name} ({_describe_key_set(key_set)})"
    )
    if not is_applying:
        print("dry run: add --apply to write it")
        return
    _write_env_file(env_path, to_env_text_with_key_set(env_path.read_text(), key_set))
    print(
        "written. Restart the backend to use it (sudo systemctl restart biveit-backend, or your local server).\n"
        + "Runners already running keep the model names they started with: when switching between Anthropic\n"
        + "and another endpoint, stop the open projects too (stop <id>), or they pick it up at their next start."
    )


# --- entry point -----------------------------------------------------------------------------------


async def _run_with_connections(command) -> None:
    """Run an async command with the platform's connections open (postgres, redis, http)."""
    await postgres.connect()
    await redis.connect()
    await http.connect()
    try:
        await command()
    finally:
        await http.close()
        await redis.close()
        await postgres.close()


async def _report_system() -> None:
    await _report_platform()
    await _report_shared_infra()
    await _report_running()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the platform: status, stopping projects, model keys.")
    commands = parser.add_subparsers(dest="command", required=True)
    status_parser = commands.add_parser("status", help="the platform's status, or one project's (read-only)")
    status_parser.add_argument("project_id", type=int, nargs="?", help="leave out for the whole platform")
    status_parser.add_argument("--conversation", type=int, help="default: the project's newest conversation")
    stop_parser = commands.add_parser("stop", help="turn a project's runners, dev servers and container off")
    stop_parser.add_argument("project_id", type=int)
    keys_parser = commands.add_parser("keys", help="list the model key sets, or switch with: keys use <set>")
    keys_parser.add_argument("action", nargs="?", choices=["use"])
    keys_parser.add_argument("key_set", nargs="?", help=f"a file name in {AGENT_API_KEYS_DIR}/ (without .env)")
    keys_parser.add_argument(
        "--env",
        default=os.environ.get("ENV", "local"),
        help="the .env.<env> to read or write (default: $ENV, else local)",
    )
    keys_parser.add_argument("--apply", action="store_true", help="write it (default: a dry run)")
    args = parser.parse_args()

    if args.command == "status" and args.project_id is None:
        asyncio.run(_run_with_connections(_report_system))
    elif args.command == "status":
        asyncio.run(_run_with_connections(lambda: _report_project_status(args.project_id, args.conversation)))
    elif args.command == "stop":
        asyncio.run(_run_with_connections(lambda: _stop_project(args.project_id)))
    elif args.action == "use" and args.key_set:
        _use_keys(args.key_set, args.env, args.apply)
    elif args.action == "use":
        parser.error("keys use needs a key set's name")
    else:
        _list_keys(args.env)


if __name__ == "__main__":
    main()
