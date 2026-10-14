# Intentions: project business logic that spans more than the projects row -- getting a project ready
# when the user opens it (open_project: its current conversation's workspace, agent session -- started
# off with the starting form's answers the first time -- and, once implementing, dev environment, as
# its stage says), and turning it all off again (close_project).
# The workspace is per CONVERSATION (see services/devenv); keeping current_port up to date is
# services/agent's job. Plain CRUD skips this layer and goes straight to repositories/projects.

from src.constants import AGENT_MOCK_REPLIES, ProjectStage
from src.dto.projects import Project
from src.repositories import ai_conversations as ai_conversations_repo
from src.services import agent as agent_service
from src.services import devenv
from src.utils.common import spawn


async def open_project(project: Project) -> None:
    """Get a project ready for the user who just opened it, so the agent doesn't have to cold-start on
    their first message. Idempotent -- every visit (or tab) can call it.

    It prepares the project's CURRENT conversation (created on first use). While AGENT_MOCK_REPLIES is
    on there's no real agent to start, so this only seeds its workspace; otherwise it brings up the
    session, and the first time, hands the agent the starting form's answers so it opens the chat
    (ensure_introduced -- once only, and retried by the next open if it failed). It returns once the
    agent has them, so the FE can show the chat right after. It's a warm-up, not a guarantee: a session
    can still die mid-visit, so sending a message to the agent must ensure the session again on its own.

    Once implementing, the dev environment is ensured too -- in the background, since even a quick
    check of it takes seconds. That also finishes a setup that never completed and brings back dev
    servers a reboot (or a crash) took down. Its failures are only logged: the agent finds out on its
    own when it next uses them."""
    conversation = await agent_service.get_current_conversation(project["id"], project["user_id"])
    if AGENT_MOCK_REPLIES:
        await devenv.ensure_workspace(project, conversation["id"])
    else:
        await agent_service.ensure_session(project, conversation["id"])
        await agent_service.ensure_introduced(project, conversation["id"])
    if project["stage"] == ProjectStage.IMPLEMENTING:
        spawn(agent_service.restore_devenv(project, conversation["id"]))


async def close_project(project: Project) -> dict:
    """Turn off everything running for the project: each conversation's agent session (Claude and its
    communication MCP) and the project's dev servers. Nothing on disk is touched, so open_project brings
    it all back. Idempotent: what's already off stays off. Returns what was stopped, as
    {sessions: {conversation_id: adapter.stop_session's result}, dev_servers: [pm2 names removed]}.

    The dev servers belong to the project, but their lock is per conversation -- the current one's is
    used, the same one setting them up takes."""
    conversations = await ai_conversations_repo.find({"project_id": project["id"]})
    sessions = {
        conversation["id"]: await agent_service.stop_session(project, conversation["id"])
        for conversation in conversations
    }
    dev_servers = await agent_service.stop_devenv(project, conversations[0]["id"]) if conversations else []
    return {"sessions": sessions, "dev_servers": dev_servers}
