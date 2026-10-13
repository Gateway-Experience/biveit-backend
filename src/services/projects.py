# Intentions: project business logic that spans more than the projects row -- for now, bringing up a
# project's agent session and recording where it's listening. Plain CRUD skips this layer and goes
# straight to repositories/projects.

import asyncio

from src.dto.projects import Project
from src.repositories import projects as projects_repo
from src.services import agent as agent_service


async def ensure_session(project: Project) -> int:
    """Make sure the project's agent session is up and return its port, recording it as the project's
    current_port.

    Goes through agent_service.ensure_workspace rather than ensure_session, so a project whose workspace
    was never set up (nothing does that at creation) gets its directory seeded before the session starts
    in it. Both steps are idempotent, so this is safe to call on every visit.

    current_port is only a hint -- the running process is the truth. It's rewritten whenever the live
    port differs, which corrects a stale value on the next call. Any OTHER project still holding this
    port (its session died and the OS handed the port out again) gets it cleared, so a port maps to at
    most one project -- which is how a session's messages find their project (services/agent)."""
    port = await agent_service.ensure_workspace(project)
    stale_holders = [held for held in await projects_repo.find({"current_port": port}) if held["id"] != project["id"]]
    await asyncio.gather(*[projects_repo.update(held["id"], {"current_port": None}) for held in stale_holders])
    if port != project.get("current_port"):
        await projects_repo.update(project["id"], {"current_port": port})
    return port
