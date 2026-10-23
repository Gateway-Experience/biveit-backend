# Intentions: running the platform from the command line, all in one place. Uses this backend's own config
# (.env.local, or .env.<ENV>), so run it from the repo root, on the box and in the env you mean.
#
#   uv run python -m scripts.manage status                                   # the whole platform
#   uv run python -m scripts.manage status <project id> [--conversation <id>]  # one project
#   uv run python -m scripts.manage stop <project id>                        # turn a project off
#   uv run python -m scripts.manage keys [--env prod]                        # the model key sets
#   uv run python -m scripts.manage keys use <set> [--env prod] [--apply]    # switch to one
#
# status prints services/platform_status's sections -- the same ones the admin dashboard shows. stop turns off
# everything running for a project (services/projects.close_project): each conversation's runner, its dev
# servers, then its container; nothing on disk is touched, opening the project brings it all back. keys
# lists the model key sets and switches .env.<env> to one (services/agent_api_keys) -- a dry run unless
# --apply; restart the backend afterwards.

import argparse
import asyncio
import os
import stat
from pathlib import Path

import pydash

from src.constants import AGENT_API_KEYS_DIR
from src.dto.projects import Project
from src.repositories import projects as projects_repo
from src.services import agent_api_keys, platform_status
from src.services import projects as projects_service
from src.utils import http, postgres, redis


def _print_sections(sections: list[dict]) -> None:
    def _to_lines(section: dict) -> list[str]:
        width = max([len(row["label"]) for row in section["rows"]], default=0)
        return [
            f"\n== {section['title']} ==",
            *[f"  {row['label'].ljust(width)}  {row['value']}" for row in section["rows"]],
        ]

    print("\n".join(line for section in sections for line in _to_lines(section)))


async def _get_project(project_id: int) -> Project:
    project = pydash.head(await projects_repo.find({"id": project_id}))
    if project is None:
        raise SystemExit(f"no live project {project_id}")
    return project


async def _report_status(project_id: int | None, conversation_id: int | None) -> None:
    if project_id is None:
        _print_sections(await platform_status.get_platform_sections())
    else:
        _print_sections(await platform_status.get_project_sections(await _get_project(project_id), conversation_id))


async def _stop_project(project_id: int) -> None:
    stopped = await projects_service.close_project(await _get_project(project_id))
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


def _list_keys(env: str) -> None:
    """Print the key sets (any problems with them), which one .env.<env> has, and key files others can read."""
    key_sets = agent_api_keys.find_key_sets()
    env_path = Path(f".env.{env}")
    keys_dir = Path(AGENT_API_KEYS_DIR)
    readable_by_others = (
        [path.name for path in [keys_dir, *keys_dir.glob("*")] if stat.S_IMODE(path.stat().st_mode) & 0o077]
        if keys_dir.is_dir()
        else []
    )
    set_lines = [
        f"  {name}: {agent_api_keys.describe_key_set(key_set)}"
        + "".join(f"  [{problem}]" for problem in agent_api_keys.check_key_set(key_set))
        for name, key_set in key_sets.items()
    ]
    current = (
        agent_api_keys.describe_current_key_set(agent_api_keys.read_env_file(env_path))
        if env_path.is_file()
        else "no such file"
    )
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
    key_set = agent_api_keys.find_key_sets().get(name)
    env_path = Path(f".env.{env}")
    if key_set is None:
        raise SystemExit(f"no key set {name!r} in {AGENT_API_KEYS_DIR}/ (see: keys)")
    if problems := agent_api_keys.check_key_set(key_set):
        raise SystemExit(f"key set {name!r}: " + "; ".join(problems))
    if not env_path.is_file():
        raise SystemExit(f"no {env_path} here -- run from the repo root, and check --env")
    current = agent_api_keys.describe_current_key_set(agent_api_keys.read_env_file(env_path))
    print(f"{env_path}: {current}\n  -> {name} ({agent_api_keys.describe_key_set(key_set)})")
    if not is_applying:
        print("dry run: add --apply to write it")
        return
    agent_api_keys.write_env_file(env_path, agent_api_keys.to_env_text_with_key_set(env_path.read_text(), key_set))
    print(
        "written. Restart the backend to use it (sudo systemctl restart biveit-backend, or your local server).\n"
        + "Runners already running keep the model names they started with: when switching between Anthropic\n"
        + "and another endpoint, stop the open projects too (stop <id>), or they pick it up at their next start."
    )


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

    if args.command == "status":
        asyncio.run(_run_with_connections(lambda: _report_status(args.project_id, args.conversation)))
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
