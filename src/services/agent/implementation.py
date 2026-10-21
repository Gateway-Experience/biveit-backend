# Intentions: the project's implementing milestones, driven by the user's approvals (the agent asks with
# the APPROVE_SPEC / APPROVE_IMPLEMENTATION actions, the user clicks, conversations.post_user_message
# hands us the answer). The agent can't start implementing or sign anything off itself -- it can only
# ask. Each milestone is recorded twice on purpose, in one go: the project's columns (stage,
# implementation_done_at -- what lists and payment read) and a signal in the log (when it happened, for
# the chat).
#   approving the spec            -> stage implementing, IMPLEMENTATION_STARTED, then the dev environment
#                                    set up in the background; BUILDING once it's up (the first build
#                                    starts right away), or DEVENV_FAILED. Either way the session hears.
#   approving the implementation  -> implementation_done_at, IMPLEMENTATION_DONE, any open build ended.
# References: AgentAction in constants.py, services/agent/signals.

from datetime import UTC, datetime

from src.constants import AgentAction, AiSignal, ProjectStage
from src.dto.projects import Project
from src.repositories import projects as projects_repo
from src.services import devenv
from src.services.agent import sessions, signals
from src.services.agent.wakeups import publish_devenv_changed
from src.utils.common import spawn
from src.utils.logging import log_error


async def apply_approvals(project: Project, conversation_id: int, approved_actions: list[AgentAction]) -> None:
    """Carry out what the user just approved (interactions.get_approved_actions). Both milestones are
    idempotent, so approving twice is harmless. An implementation approval while still spec-ing does
    nothing: the agent can't even ask for one then (interactions.validate_session_content)."""
    if AgentAction.APPROVE_SPEC in approved_actions:
        await start_implementation(project, conversation_id)
    if AgentAction.APPROVE_IMPLEMENTATION in approved_actions and project["stage"] == ProjectStage.IMPLEMENTING:
        await finish_implementation(project, conversation_id)


async def start_implementation(project: Project, conversation_id: int) -> None:
    """Move the project to implementing, kick off its dev environment setup, and return right away -- a
    first setup takes minutes. IMPLEMENTATION_STARTED is written only by the call that actually moves the
    stage. Calling it again (re-approving, or after a failed setup) is fine: the stage stays, and the
    setup waits for any running one, then reports too.

    The stage goes first, before anything is set up: it's what the project should be, and every later
    open re-runs the setup until the workspace matches it (services/projects.open_project) -- which is
    also how a failed setup gets retried."""
    if project["stage"] == ProjectStage.SPECING:
        # Compare-and-set: of two racing calls only one writes; the other finds it already moved.
        moved = await projects_repo.update(
            project["id"], {"stage": ProjectStage.IMPLEMENTING}, {"stage": ProjectStage.SPECING}
        )
        if moved:
            await signals.append_signal(conversation_id, AiSignal.IMPLEMENTATION_STARTED)
        await publish_devenv_changed(conversation_id)
    spawn(_set_up_and_report(project, conversation_id))


async def _set_up_and_report(project: Project, conversation_id: int) -> None:
    """Run the setup, record how it went (BUILDING on success -- the first build starts right after --
    or DEVENV_FAILED), and push it into the session as a text message. A failure is logged here (this
    is where it stops bubbling) and reported to the agent, so it can tell the user."""
    try:
        description = await devenv.ensure_devenv(project, conversation_id)
        await signals.append_signal(conversation_id, AiSignal.BUILDING, "Building the first version")
        report = _describe_ready(description)
    except Exception as error:
        log_error(error, meta={"conversation_id": conversation_id, "source": "start_implementation"})
        # args[0] is the message; str() of an error raised with a meta dict too would be the tuple.
        reason = f"{type(error).__name__}: {error.args[0] if error.args else error}"
        await signals.append_signal(conversation_id, AiSignal.DEVENV_FAILED, reason)
        report = "\n".join(
            [
                "[Dev environment failed]",
                reason,
                "Tell the user it couldn't be set up. It's retried every time they open the project; whatever "
                + "was already set up is kept.",
            ]
        )
    await publish_devenv_changed(conversation_id)
    await sessions.send(project, conversation_id, [{"type": "text", "text": report}])


def _describe_ready(description: dict) -> str:
    """[Pure] The ready message for the agent: where things are and what's now its job."""
    backend, frontend = description["backend"], description["frontend"]
    return "\n".join(
        [
            "[Dev environment ready] backend/ and frontend/ are set up, and both dev servers are running:",
            f"- backend: port {backend['port']}, pm2 process {backend['process']}, log {backend['log']}",
            f"- frontend: port {frontend['port']}, pm2 process {frontend['process']}, log {frontend['log']}",
            f"- the site: {description['url']}",
            "- Redis: the project's own, at 127.0.0.1:6379 (REDIS_URL in backend/.env.local)",
            "Both servers reload on save. After changing dependencies, run that app's ./setup.sh (in backend/ or "
            + "frontend/) -- it installs whatever's missing.",
            "Use the dev_servers tool to check, restart or stop the servers -- never pm2.",
            "The building status is now ON (the user sees the site being built). Send the BUILDING_DONE signal "
            + "(send_signal tool) once this first version is done.",
        ]
    )


async def finish_implementation(project: Project, conversation_id: int) -> None:
    """The user signed off the implemented site. Records it on the project (implementation_done_at, the
    first time only -- it's a milestone, kept even if the user goes on iterating), writes
    IMPLEMENTATION_DONE (every sign-off: each is a version the user approved), ends any open build, and
    wakes the FE, which starts offering payment/deployment."""
    if not project["implementation_done_at"]:
        await projects_repo.update(project["id"], {"implementation_done_at": datetime.now(UTC)})
    await signals.append_signal(conversation_id, AiSignal.IMPLEMENTATION_DONE)
    await signals.end_open_build(conversation_id, "The user signed off the site")
    await publish_devenv_changed(conversation_id)
