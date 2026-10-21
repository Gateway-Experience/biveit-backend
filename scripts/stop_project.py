# Intentions: turn off everything running for a project -- each conversation's agent session and its dev
# servers -- via services/projects.close_project. Sessions are stopped on BOTH runtimes (tmux and agent2's
# runner), so there's no guessing which one a project is on. Nothing on disk is touched; opening the
# project brings it all back. Safe to run again: what's off stays off.
#
#   uv run python -m scripts.stop_project <project id>

import argparse
import asyncio

import pydash

from src.repositories import projects as projects_repo
from src.services import projects as projects_service
from src.utils import postgres, redis


async def main() -> None:
    parser = argparse.ArgumentParser(description="Turn off a project's agent sessions and dev servers.")
    parser.add_argument("project_id", type=int)
    args = parser.parse_args()

    await postgres.connect()
    await redis.connect()
    try:
        project = pydash.head(await projects_repo.find({"id": args.project_id}))
        if project is None:
            raise SystemExit(f"no live project {args.project_id}")
        stopped = await projects_service.close_project(project)
        for conversation_id, session in stopped["sessions"].items():
            print(
                f"conversation {conversation_id}: tmux session "
                + f"{'killed' if session['tmux_session'] else 'was not running'}, "
                + f"session pids stopped {session['pids'] or 'none'}, "
                + f"agent2 runner {'stopped' if session['runner'] else 'was not running'}"
            )
        print(f"dev servers removed from pm2: {', '.join(stopped['dev_servers']) or 'none'}")
        print(f"container {'stopped' if stopped['container'] else 'was not running'}")
    finally:
        await redis.close()
        await postgres.close()


if __name__ == "__main__":
    asyncio.run(main())
