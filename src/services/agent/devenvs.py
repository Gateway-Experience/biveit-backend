# Intentions: a conversation's dev environment (services/devenv) as the chat sees it.
# For the agent's session (through its channel's tools, so always about its OWN conversation's project):
#   start_implementation  the agent is done spec-ing: set up the dev environment in the background, then
#                         tell the session over its channel when it's ready, or why it failed. The agent
#                         waits for that message before it starts building.
#   control_dev_servers   start / restart / stop / check the project's dev servers. The agent can't run
#                         pm2 itself: there's one pm2 for every project on the box.
# For the user's tabs:
#   get_devenv_status     the stage, the preview URL and the dev servers, for the preview screen
#   restore_devenv        bring an implementing project's servers back on a visit (services/projects)
#   stop_devenv           turn the dev servers off (services/projects.close_project)
#   set_building_status   the agent's building flag on/off (see building.py)
#   mark_implementing_done  the user signed off the implemented site (projects.implementation_done_at)
# Whatever may change the environment ends with publish_devenv_changed, so open tabs re-read the status.

from datetime import UTC, datetime

from src.constants import DevServerAction, ProjectStage
from src.dto.ai_conversations import AiConversation
from src.dto.projects import Project
from src.repositories import projects as projects_repo
from src.services import devenv
from src.services.agent import building, sessions
from src.services.agent.conversations import (
    get_conversation_project,
    get_live_conversation,
)
from src.services.agent.wakeups import publish_devenv_changed
from src.utils.common import spawn
from src.utils.logging import log_error


class ImplementationNotStarted(Exception):
    """The dev servers were asked for before start_implementation set the app up, or implementing was
    called done before it started. 409."""


async def start_implementation(conversation_id: int) -> None:
    """Move the conversation's project to implementing, kick off its dev environment setup, and return
    right away -- a first setup takes minutes. The session hears how it went over its channel. Raises
    ConversationNotFound. Calling it again (while a setup runs, or after one failed) is fine: the stage
    stays, and the setup waits for any running one, then reports too.

    The stage goes first, before anything is set up: it's what the project should be, and every later
    open re-runs the setup until the workspace matches it (services/projects.open_project)."""
    conversation = await get_live_conversation(conversation_id)
    project = await get_conversation_project(conversation)
    if project["stage"] == ProjectStage.SPECING:
        # Compare-and-set: of two racing calls only one writes; the other finds it already moved.
        await projects_repo.update(project["id"], {"stage": ProjectStage.IMPLEMENTING}, {"stage": ProjectStage.SPECING})
        await publish_devenv_changed(conversation_id)
    spawn(_set_up_and_report(project, conversation_id))


async def _set_up_and_report(project: Project, conversation_id: int) -> None:
    """Run the setup and push the outcome into the session as a text message. A failure is logged here
    (this is where it stops bubbling) and reported to the agent, so it can tell the user."""
    try:
        description = await devenv.ensure_devenv(project, conversation_id)
        report = _describe_ready(description)
        # The first build starts right after this message, so the flag goes on without the agent asking.
        await building.set_building(conversation_id, "Building the first version")
    except Exception as error:
        log_error(error, meta={"conversation_id": conversation_id, "source": "start_implementation"})
        # args[0] is the message; str() of an error raised with a meta dict too would be the tuple.
        reason = f"{type(error).__name__}: {error.args[0] if error.args else error}"
        report = f"[Dev environment failed]\n{reason}\nTell the user it couldn't be set up. Calling start_implementation again retries; whatever was already set up is kept."
    await publish_devenv_changed(conversation_id)
    port = await sessions.ensure_session(project, conversation_id)
    await sessions.send(port, [{"type": "text", "text": report}])


def _describe_ready(description: dict) -> str:
    """[Pure] The ready message for the agent: where things are and what's now its job."""
    backend, frontend = description["backend"], description["frontend"]
    return "\n".join(
        [
            "[Dev environment ready] backend/ and frontend/ are set up, and both dev servers are running:",
            f"- backend: port {backend['port']}, pm2 process {backend['process']}, log {backend['log']}",
            f"- frontend: port {frontend['port']}, pm2 process {frontend['process']}, log {frontend['log']}",
            f"- the site: {description['url']}",
            "Both reload on save. Install dependencies yourself whenever you change them "
            + "(uv sync in backend/, npm install in frontend/).",
            "Use the dev_servers tool to check, restart or stop the servers -- never pm2.",
            "The building status is now ON (the user sees the site being built). Call the building tool with "
            + "active: false once this first version is done.",
        ]
    )


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
    implementation_done_at, the agent's building flag (building.get_building), plus
    devenv.get_devenv_status (busy, preview_url, servers)."""
    project = await get_conversation_project(conversation)
    return {
        "stage": project["stage"],
        "implementation_done_at": project["implementation_done_at"],
        "building": await building.get_building(conversation["id"]),
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


async def set_building_status(conversation_id: int, active: bool, summary: str | None = None) -> None:
    """The agent's `building` tool: turn its building flag on (or refresh it) or off. Raises
    ConversationNotFound."""
    await get_live_conversation(conversation_id)
    if active:
        await building.set_building(conversation_id, summary)
    else:
        await building.clear_building(conversation_id)


async def mark_implementing_done(conversation_id: int) -> dict:
    """The agent's implementing_done tool: the user is happy with the implemented site. Records it on the
    project (implementation_done_at, the first time only -- it's a milestone, kept even if the user
    goes back to more spec-ing or implementing), turns the building flag off (nothing's being built
    now), and wakes the FE, which starts offering payment/deployment. Idempotent. Returns
    {implementation_done_at}. Raises ConversationNotFound, or ImplementationNotStarted while still
    spec-ing."""
    conversation = await get_live_conversation(conversation_id)
    project = await get_conversation_project(conversation)
    if project["stage"] != ProjectStage.IMPLEMENTING:
        raise ImplementationNotStarted(f"project {project['id']} is {project['stage']}", {"project_id": project["id"]})
    done_at = (
        project["implementation_done_at"]
        or (await projects_repo.update(project["id"], {"implementation_done_at": datetime.now(UTC)}))[
            "implementation_done_at"
        ]
    )
    await building.clear_building(conversation_id)  # wakes the FE too
    return {"implementation_done_at": done_at}
