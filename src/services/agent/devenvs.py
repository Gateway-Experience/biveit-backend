# Intentions: a conversation's dev environment (services/devenv) as the chat sees it.
# For the agent's session (through its channel's tools, so always about its OWN conversation's project):
#   control_dev_servers   start / restart / stop / check the project's dev servers. The agent can't run
#                         pm2 itself: there's one pm2 for every project on the box.
# For the user's tabs:
#   get_devenv_status     the stage, the preview URL and the dev servers, for the preview screen
#   restore_devenv        bring an implementing project's servers back on a visit (services/projects)
#   stop_devenv           turn the dev servers off (services/projects.close_project)
# Setting the environment up in the first place follows the user approving the spec (implementation.py).
# Whatever may change the environment ends with publish_devenv_changed, so open tabs re-read the status.

from src.constants import DevServerAction
from src.dto.ai_conversations import AiConversation
from src.dto.projects import Project
from src.services import devenv
from src.services.agent.conversations import (
    get_conversation_project,
    get_live_conversation,
)
from src.services.agent.wakeups import publish_devenv_changed


class ImplementationNotStarted(Exception):
    """The dev servers were asked for before there's an app to run (the spec isn't approved yet). 409."""


async def control_dev_servers(conversation_id: int, action: DevServerAction) -> list[dict]:
    """Do `action` to the conversation project's dev servers; returns their statuses. Raises
    ConversationNotFound, or ImplementationNotStarted if there's no app to run yet."""
    conversation = await get_live_conversation(conversation_id)
    project = await get_conversation_project(conversation)
    if not devenv.has_app(project, conversation_id):
        raise ImplementationNotStarted(f"conversation {conversation_id}", {"conversation_id": conversation_id})
    statuses = await devenv.control_dev_servers(project, conversation_id, action)
    await publish_devenv_changed(conversation_id)
    return statuses


async def stop_devenv(project: Project, conversation_id: int) -> list[str]:
    """Turn the project's dev servers off (devenv.stop_dev_servers) and let open tabs know. Idempotent;
    returns the pm2 names it removed."""
    removed = await devenv.stop_dev_servers(project, conversation_id)
    await publish_devenv_changed(conversation_id)
    return removed


async def get_devenv_status(conversation: AiConversation) -> dict:
    """The conversation's dev environment for the preview screen: the project's stage and
    implementation_done_at, plus devenv.get_devenv_status (busy, preview_url, servers). Whether the agent
    is building isn't here -- that's a signal in the message log (services/agent/signals)."""
    project = await get_conversation_project(conversation)
    return {
        "stage": project["stage"],
        "implementation_done_at": project["implementation_done_at"],
        **await devenv.get_devenv_status(project, conversation["id"]),
    }


async def restore_devenv(project: Project, conversation_id: int) -> None:
    """Re-run the setup of a project that's implementing (a visit's warm-up: it brings back servers a
    reboot took down), then let open tabs know. No report to the agent -- it didn't ask. Raises like
    devenv.ensure_devenv; the tabs are told either way, since a failure changes the status too."""
    try:
        await devenv.ensure_devenv(project, conversation_id)
    finally:
        await publish_devenv_changed(conversation_id)
