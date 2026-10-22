# Intentions: one look at the platform as it currently is. Read-only: it never starts, stops or writes
# anything (so no --apply), and it never creates a conversation if there's none yet. Uses this backend's
# own config (.env.local / ENV), so run it on the box and in the env you're asking about.
#
#   uv run python -m scripts.project_status                                   # the whole system
#   uv run python -m scripts.project_status <project id> [--conversation <id>]  # one project
#
# The system view: platform config, the infra everything shares, the tools the setup needs, and what's
# running across all projects. The project view: its workspace on disk, its agent's runner (services/agent/runners),
# its container, its dev servers, and its own slice of the infra.
#
# Each check that can fail (a dead redis, no pm2...) says so in its own section instead of stopping the rest.

import argparse
import asyncio
import json
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pydash
import redis.asyncio as redis_lib

from src.config import settings
from src.constants import (
    AGENT_MOCK_REPLIES,
    DEVENV_APP_DIRS,
    DEVENV_BACKEND_HEALTH_PATH,
    DEVENV_ENV_FILES,
    PROJECT_REDIS_DATABASES_NEEDED,
)
from src.dto.projects import Project
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import projects as projects_repo
from src.services import devenv
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
    _print_section(
        "Container",
        [
            ("name", name),
            ("state", pydash.get(inspected, "State.Status") or "none"),
            ("image (running)", pydash.get(inspected, "Config.Image") or "-"),
            ("image (recorded on the project)", pydash.get(project, "meta.devenv_image") or "- (none yet)"),
        ],
    )


async def _describe_runner_process(project: Project, conversation_id: int) -> str:
    """The runner's pm2 process in the project's container and its state -- or why there's none."""
    name = runners.get_process_name(conversation_id)
    if not await devenv.is_container_running(project["id"]):
        return f"{name} (container not running)"
    try:
        listing = await devenv.run_in_container(project["id"], ["pm2", "jlist"])
    except CommandFailed as error:
        return f"{name} (pm2 unreadable: {error})"
    processes = {process["name"]: process for process in json.loads(listing.strip().splitlines()[-1])}
    process = processes.get(name)
    if not process:
        return f"{name} (not in pm2)"
    return f"{name}: {pydash.get(process, 'pm2_env.status')}, {pydash.get(process, 'pm2_env.restart_time', 0)} restarts"


async def _report_servers(project: Project, conversation_id: int) -> None:
    try:
        pm2_processes = json.loads(await run_command(["pm2", "jlist"]))
    except (CommandFailed, FileNotFoundError) as error:
        _print_section("Dev servers", [("pm2", f"unavailable: {error}")])
        return
    by_name = {process["name"]: process for process in pm2_processes}
    names = servers.get_dev_server_names(project["id"])
    ports = {
        "backend": addresses.get_dev_backend_port(project["id"]),
        "frontend": addresses.get_dev_frontend_port(project["id"]),
    }
    now_ms = time.time() * 1000

    def _describe(app: str) -> str:
        process = by_name.get(names[app])
        if process is None:
            return f"{names[app]}: not in pm2"
        pm2_env = process.get("pm2_env", {})
        uptime = f"up {(now_ms - pm2_env['pm_uptime']) / 60000:.0f} min" if pm2_env.get("status") == "online" else ""
        memory = _format_size(pydash.get(process, "monit.memory", 0))
        return f"{names[app]}: {pm2_env.get('status')} {uptime}, pid {process.get('pid')}, {pm2_env.get('restart_time', 0)} restarts, {memory}"

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
    redis_db = addresses.get_dev_redis_db(project["id"])
    project_redis = await _count_db_keys(f"{settings.project_redis_url}/{redis_db}")
    _print_section(
        "Data & locks",
        [
            *locks,
            ("mongo dev db / prod db", f"db_project_dev_{project['id']} / db_project_{project['id']}"),
            ("redis dev db", f"{redis_db} -- {project_redis}"),
        ],
    )


async def _count_db_keys(redis_url: str) -> str:
    client = redis_lib.from_url(redis_url, socket_timeout=2)
    try:
        return f"{await client.dbsize()} key(s)"
    except Exception as error:
        return f"unreachable: {error}"
    finally:
        await client.aclose()


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
            (
                "projects' redis",
                f"{_mask_url(settings.project_redis_url)} ({await _ping_redis(settings.project_redis_url)}, "
                + f"{await _get_redis_database_count(settings.project_redis_url)} databases -- needs {PROJECT_REDIS_DATABASES_NEEDED})",
            ),
        ],
    )


async def _get_redis_database_count(redis_url: str) -> str:
    client = redis_lib.from_url(redis_url, socket_timeout=2)
    try:
        return (await client.config_get("databases")).get("databases", "?")
    except Exception:
        return "?"
    finally:
        await client.aclose()


async def _ping_redis(redis_url: str) -> str:
    client = redis_lib.from_url(redis_url, socket_timeout=2)
    try:
        await client.ping()
        return "reachable"
    except Exception as error:
        return f"UNREACHABLE: {error}"
    finally:
        await client.aclose()


async def _report_running() -> None:
    """What's running for us across all projects: the agents' runners (by their sockets), and the dev
    servers as pm2 prints them -- only our own entries (project_<id>_be/_fe), never anything else on the
    box. Plus the locks held in redis."""
    try:
        pm2_lines = (await run_command(["pm2", "ls", "--no-color"])).splitlines()
        # The table's rows start with │; borders don't. Keep the borders, the header row and our rows.
        table_rows = [line for line in pm2_lines if line.startswith("│")]
        our_rows = [line for line in table_rows[1:] if re.search(r"│ project_\d+_(be|fe)\s", line)]
        pm2_table = (
            [line for line in pm2_lines if not line.startswith("│") or line == table_rows[0] or line in our_rows]
            if our_rows
            else []
        )
    except (CommandFailed, FileNotFoundError) as error:
        pm2_table = [f"pm2 unavailable: {error}"]
    try:
        held_locks = sorted([key.decode() async for key in redis.client().scan_iter(match="devenv:*", count=500)])
    except Exception as error:
        held_locks = [f"unreadable: {error}"]
    print("\n== agent runners ==")
    print("\n".join(await _list_runners()) or "  none")
    print("\n== pm2 ls (dev servers) ==")
    print("\n".join(pm2_table) or "  none")
    _print_section("Locks", [("held", " ".join(held_locks) or "none")])


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


async def main() -> None:
    parser = argparse.ArgumentParser(description="The platform's status, or one project's. Read-only.")
    parser.add_argument("project_id", type=int, nargs="?", help="leave out for the whole system")
    parser.add_argument("--conversation", type=int, help="default: the project's newest conversation")
    args = parser.parse_args()

    await postgres.connect()
    await redis.connect()
    await http.connect()
    try:
        if args.project_id is None:
            await _report_platform()
            await _report_shared_infra()
            await _report_running()
        else:
            await _report_project_status(args.project_id, args.conversation)
    finally:
        await http.close()
        await redis.close()
        await postgres.close()


if __name__ == "__main__":
    asyncio.run(main())
