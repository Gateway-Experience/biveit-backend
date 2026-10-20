# Intentions: a conversation's agent session as the rest of the app sees it -- making sure it's running and
# talking to it, translating between our stored messages and the channel's wire format. Two runtimes sit
# behind it, picked by AGENT_RUNTIME; callers never know which:
#   tmux  Claude Code in tmux (the OS side is adapter.py), projects.current_port pointing at its channel
#   sdk   a Claude Agent SDK runner per conversation (services/agent2), on a unix socket
# Both take the same channel format below and post back to the same endpoints. Stopping stops both.
#
# THE CHANNEL (the session's `communication` MCP server, mcps/src/services/communication-mcp):
#   into the session:  POST http://127.0.0.1:<port>/ {content: [item]}, item = {type: "text", text} or
#                      {type: "tool_result", content: [answer]}. Answers carry NO tool_use_id -- they're
#                      matched BY ORDER to the tool_use items of the session's last message, and must all
#                      come in one message (which our submissions guarantee: the FE answers a whole
#                      message's questions at once). Answer statuses: submitted | aborted | skipped.
#   out of it:         its send_message tool posts {content} back (-> receive_session_message), where
#                      tool_use items have NO id or name, just input {questions} or {actions}. It calls
#                      us with the conversation's session token (services/agent/tokens), which is
#                      also how we know which conversation it's from.
# Our side stores the API-shaped version (ids, names, tool_use_id) -- see to_stored_content and
# to_session_content for the two translations.

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from src.constants import (
    AGENT_RUNTIME,
    ASK_USER_TOOL_NAME,
    CONVERSATION_INTRODUCED_KEY,
    INTRODUCTION_OVERDUE_SECONDS,
    REQUEST_ACTIONS_TOOL_NAME,
    SESSION_SEND_ATTEMPTS,
    SIGNAL_TAIL_SIZE,
    USER_LANGUAGE_NAMES,
    AgentRuntime,
    AiMessageRole,
    InteractionStatus,
)
from src.dto.projects import Project
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import projects as projects_repo
from src.repositories import users as users_repo
from src.services import agent2, devenv
from src.services.agent import adapter, signals, tokens
from src.utils import http
from src.utils.common import exponential_backoff, with_retries


class SessionUnavailable(Exception):
    """The session couldn't be reached or refused the message. The user's message is already stored; it
    just didn't get through. 503."""


async def ensure_session(project: Project, conversation_id: int) -> None:
    """Make sure the conversation's session is up, on AGENT_RUNTIME. Idempotent -- safe on every visit and
    before every send. Raises SessionUnavailable if it can't be brought up."""
    if AGENT_RUNTIME == AgentRuntime.SDK:
        await _ensure_runner(project, conversation_id)
    else:
        await _ensure_tmux_session(project, conversation_id)


async def send(project: Project, conversation_id: int, content: list[dict]) -> None:
    """Push a message (channel items) into the conversation's session, bringing it up first if it isn't.
    Raises SessionUnavailable if it can't be reached, or refuses the message."""
    if AGENT_RUNTIME == AgentRuntime.SDK:
        await _ensure_runner(project, conversation_id)
        try:
            await agent2.send(project, conversation_id, content)
        except agent2.RunnerUnavailable as error:
            raise SessionUnavailable(str(error), {"conversation_id": conversation_id}) from error
    else:
        await _send_to_port(conversation_id, await _ensure_tmux_session(project, conversation_id), content)


async def _ensure_runner(project: Project, conversation_id: int) -> None:
    try:
        await agent2.ensure_session(project, conversation_id)
    except agent2.RunnerUnavailable as error:
        raise SessionUnavailable(str(error), {"conversation_id": conversation_id}) from error


async def _ensure_tmux_session(project: Project, conversation_id: int) -> int:
    """Make sure the conversation's tmux session is up and return its port, recording it as the project's
    current_port. Raises SessionUnavailable if it can't be brought up.

    Ensures the workspace first, so one that was never set up gets seeded before the session starts in
    it. Idempotent -- safe on every visit and before every send.

    current_port is only a hint -- the running process is the truth. It's rewritten whenever the live
    port differs, and any OTHER project still holding this port (its session died and the port was
    handed out again) gets it cleared, so a port maps to at most one project. It's bookkeeping for
    humans now (which project is on which port) -- sessions identify themselves by their token
    (services/agent/tokens). It lives on the project only because a project has one conversation for
    now; it moves to the conversation once there can be several."""
    try:
        await devenv.ensure_workspace(project, conversation_id)
        port = await adapter.ensure_session(project, conversation_id)
    except (adapter.SessionStartFailed, tokens.SessionTokenSecretMissing) as error:
        raise SessionUnavailable(str(error), {"conversation_id": conversation_id}) from error
    stale_holders = [held for held in await projects_repo.find({"current_port": port}) if held["id"] != project["id"]]
    await asyncio.gather(*[projects_repo.update(held["id"], {"current_port": None}) for held in stale_holders])
    if port != project.get("current_port"):
        await projects_repo.update(project["id"], {"current_port": port})
    return port


async def stop_session(project: Project, conversation_id: int) -> dict:
    """Turn the conversation's session off on BOTH runtimes (whichever is running -- after a switch, maybe
    the other one), clear the project's current_port (it would otherwise keep naming a port nobody listens
    on), and end any open build with a BUILDING_DONE (nobody's building anything now). Idempotent. Returns
    what was stopped: adapter.stop_session's {tmux_session, pids} plus {runner: whether one was running}."""
    stopped = await adapter.stop_session(project, conversation_id)
    runner_stopped = await agent2.stop_session(project, conversation_id)
    await signals.end_open_build(conversation_id, "The session was stopped")
    if project.get("current_port") is not None:
        await projects_repo.update(project["id"], {"current_port": None})
    return {**stopped, "runner": runner_stopped}


async def ensure_introduced(project: Project, conversation_id: int) -> None:
    """Make sure the conversation's agent has been started off on the project (introduce_project) --
    once, unless that once didn't take. Safe to call on every open, even from two tabs at once: the
    conversation's CONVERSATION_INTRODUCED_KEY is claimed first, and only the caller that wins it sends
    anything. Raises SessionUnavailable.

    Two ways an introduction doesn't take, both retried by the next open: sending it fails (the claim is
    given back right away), or it got through but the agent never said anything -- its first turn died
    (an API error, a crash). The latter is only knowable by waiting, so the claim can be taken again once
    it's INTRODUCTION_OVERDUE_SECONDS old with no agent message in the log yet."""
    if not await _claim_introduction(conversation_id):
        return
    try:
        await introduce_project(project, conversation_id)
    except BaseException:
        await ai_conversations_repo.delete_meta_key(conversation_id, CONVERSATION_INTRODUCED_KEY)
        raise


async def _claim_introduction(conversation_id: int) -> bool:
    """Claim the right to introduce the agent: the first time, or again when the introduction is overdue
    (see ensure_introduced). Reads the log only when the claim is already taken. Signals don't count as the
    agent having said something."""
    if await ai_conversations_repo.claim_meta_key(conversation_id, CONVERSATION_INTRODUCED_KEY):
        return True
    tail = await ai_messages_repo.find_before(conversation_id, None, SIGNAL_TAIL_SIZE)
    has_agent_spoken = any(
        message["role"] == AiMessageRole.ASSISTANT and not signals.is_signal_message(message) for message in tail
    )
    if has_agent_spoken:
        return False
    stale_before = datetime.now(UTC) - timedelta(seconds=INTRODUCTION_OVERDUE_SECONDS)
    return await ai_conversations_repo.claim_meta_key(conversation_id, CONVERSATION_INTRODUCED_KEY, stale_before)


async def introduce_project(project: Project, conversation_id: int) -> None:
    """Start the agent off on a new project: make sure its session is up, and push it the starting
    form's answers (projects.bootstrap_data) and the owner's language (users.language) with the cue to
    begin spec-ing -- the agent opens the chat. Raises SessionUnavailable. Call ensure_introduced
    instead, which makes it happen once.

    Gotcha: the language is only told here, once. If the user switches it later, the agent hears it
    from how they write, not from us."""
    owner = await users_repo.get(project["user_id"])
    language = (owner or {}).get("language", "en")
    text = "\n".join(
        [
            "[New project] The user just created this project by filling in its bootstrap form.",
            f"Project name: {project['name']}",
            f"Description: {project.get('description') or '(none)'}",
            "Their answers:",
            json.dumps(project.get("bootstrap_data") or {}, indent=2, ensure_ascii=False),
            f"Their language: {USER_LANGUAGE_NAMES.get(language, language)} ({language}). Talk to them in it.",
            "Read CLAUDE.md, start the spec-ing stage now, and open the chat with a message along these lines:\n",
            "Welcome to your new project *<project name>*. I am your assistant and my role is ",
            "to help you build the website the way you envisioned. This will be an easy and fun ",
            "journey, just take it easy and follow along with me, I promise you will be happy with the end result.",
            "First, before building the website, we'll go through the process of collecting requirements first, ",
            "so I know exactly what kind of website you want to create. This might feel like an interview at times, ",
            "but please bear with me as the more I understand about your website, down to the specific details, ",
            "the more it will turn out the way you want it to be.\n",
            "Let's get started!",
            "<then ask them the first open-ended question to get the whole process rolling>.",
        ]
    )
    await send(project, conversation_id, [{"type": "text", "text": text}])


async def _send_to_port(conversation_id: int, port: int, content: list[dict]) -> None:
    """Push a message into the conversation's tmux session, its channel listening on `port`. It goes with the session's
    token, which its inbox insists on (src/mcps/communication). Retries connection failures for a few
    seconds, since a session that was just started takes a moment to begin listening. Raises
    SessionUnavailable if it never answers, or answers with anything but success (a 401 included: a
    session whose token doesn't match isn't the conversation's)."""
    headers = {"Authorization": f"Bearer {tokens.to_session_token(conversation_id)}"}
    try:
        status, _, _ = await with_retries(
            lambda: http.request("POST", f"http://127.0.0.1:{port}/", json={"content": content}, headers=headers),
            retries=SESSION_SEND_ATTEMPTS,
            calculate_backoff=exponential_backoff(0.2, cap=2),
            # Connection refused/reset are OSErrors (aiohttp's included); anything else isn't worth a retry.
            should_retry=lambda error, _: isinstance(error, OSError),
        )
    except OSError as error:
        raise SessionUnavailable(f"session on port {port} unreachable", {"port": port}) from error
    if not 200 <= status < 300:
        raise SessionUnavailable(f"session on port {port} answered {status}", {"port": port, "status": status})


def to_session_content(user_message: dict) -> list[dict]:
    """[Pure] A stored user message as what to push into the session, in the channel's format.

    Text goes as-is. An attachment becomes a text line with its path: the session works IN the
    conversation folder, so "uploads/..." is a path it can open directly. Answers go as one tool_result
    item each, keeping their order -- the message already answers every question of the agent's message,
    in the order asked (post_user_message makes sure), which is exactly how the channel matches them up,
    since its items carry no ids. Our `untouched` becomes the channel's `skipped`."""
    return [_to_session_item(block) for block in user_message["content"]]


def _to_session_item(block: dict) -> dict:
    """[Pure] One of our user-message blocks as a channel item. An answer keeps the id of the call it
    answers: agent2 matches answers to its waiting questions by it (the first runtime goes by order)."""
    if block["type"] == "tool_result":
        return {
            "type": "tool_result",
            "tool_use_id": block["tool_use_id"],
            "content": [_to_session_answer(answer) for answer in block["content"]],
        }
    if block["type"] == "attachment":
        return {
            "type": "text",
            "text": f"[The user attached a file: {block['path']} ({block['name']}, {block['mime']}, {block['size']} bytes)]",
        }
    return {"type": "text", "text": block.get("text", "")}


def _to_session_answer(answer: dict) -> dict:
    return {**answer, "status": "skipped"} if answer["status"] == InteractionStatus.UNTOUCHED else answer


def to_stored_content(content: list[dict]) -> list[dict]:
    """[Pure-ish] A message from the session, as we store it: its tool_use items get the `name` the channel
    leaves out -- ask_user for questions, request_actions for actions -- and an `id` unless they came with
    one (agent2 sends Claude's own, to match the answer back), since answering and pending-detection both
    work by them. Everything else is kept as sent."""
    return [
        {
            "id": f"toolu_{uuid.uuid4().hex[:24]}",
            "name": ASK_USER_TOOL_NAME if "questions" in block.get("input", {}) else REQUEST_ACTIONS_TOOL_NAME,
            **block,
        }
        if block.get("type") == "tool_use"
        else block
        for block in content
    ]
