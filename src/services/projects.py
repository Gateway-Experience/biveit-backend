# Intentions: project business logic that spans more than the projects row -- for now, getting a project
# ready when the user opens it: its current conversation's workspace and agent session. The workspace is
# per CONVERSATION (see services/devenv); keeping current_port up to date is services/agent's job.
# Plain CRUD skips this layer and goes straight to repositories/projects.

from src.constants import AGENT_MOCK_REPLIES
from src.dto.projects import Project
from src.services import agent as agent_service
from src.services import devenv


async def open_project(project: Project) -> None:
    """Get a project ready for the user who just opened it, so the agent doesn't have to cold-start on
    their first message. Idempotent -- every visit (or tab) can call it.

    It prepares the project's CURRENT conversation (created on first use). While AGENT_MOCK_REPLIES is
    on there's no real agent to start, so this only seeds its workspace; otherwise it brings up the
    session too. It's a warm-up, not a guarantee: a session can still die mid-visit, so sending a
    message to the agent must ensure the session again on its own."""
    conversation = await agent_service.get_current_conversation(project["id"], project["user_id"])
    if AGENT_MOCK_REPLIES:
        await devenv.ensure_workspace(project, conversation["id"])
        return
    await agent_service.ensure_session(project, conversation["id"])
