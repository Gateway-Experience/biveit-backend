# Intentions: a conversation's agent session as the rest of the app sees it -- making sure it's running and
# talking to it, translating between our stored messages and the session's wire format. The session is a
# session runner in the project's container (runners.py); this is the part that doesn't care how it
# runs: what to say to it (introductions), and the translations.
#
# THE CHANNEL (the runner's inbox and the platform's agent endpoints, see biveit-session-runner):
#   into the session:  {content: [item]}, item = {type: "text", text} or {type: "tool_result",
#                      tool_use_id, content: [answer]} -- an answer names the call it answers, and the
#                      runner hands it to that waiting call.
#   out of it:         {content} posted back (-> receive_session_message), where tool_use items come with
#                      the call's id but NO name, just input {questions} or {actions}. It calls us with the
#                      conversation's session token (services/agent/tokens), which is also how we know
#                      which conversation it's from.
# Our side stores the API-shaped version (ids, names, tool_use_id) -- see to_stored_content and
# to_session_content for the two translations.

import json
import uuid
from datetime import UTC, datetime, timedelta

from src.constants import (
    ASK_USER_TOOL_NAME,
    CONVERSATION_INTRODUCED_KEY,
    INTRODUCTION_OVERDUE_SECONDS,
    REQUEST_ACTIONS_TOOL_NAME,
    SIGNAL_TAIL_SIZE,
    USER_LANGUAGE_NAMES,
    AiMessageRole,
)
from src.dto.projects import Project
from src.repositories import ai_conversations as ai_conversations_repo
from src.repositories import ai_messages as ai_messages_repo
from src.repositories import users as users_repo
from src.services.agent import runners, signals


class SessionUnavailable(Exception):
    """The session couldn't be reached or refused the message. The user's message is already stored; it
    just didn't get through. 503."""


async def ensure_session(project: Project, conversation_id: int) -> None:
    """Make sure the conversation's session is up. Idempotent -- safe on every visit and before every send.
    Raises SessionUnavailable if it can't be brought up."""
    try:
        await runners.ensure_session(project, conversation_id)
    except runners.RunnerUnavailable as error:
        raise SessionUnavailable(str(error), {"conversation_id": conversation_id}) from error


async def send(project: Project, conversation_id: int, content: list[dict]) -> None:
    """Push a message (channel items) into the conversation's session, bringing it up first if it isn't.
    Raises SessionUnavailable if it can't be reached, or refuses the message."""
    await ensure_session(project, conversation_id)
    try:
        await runners.send(project, conversation_id, content)
    except runners.RunnerUnavailable as error:
        raise SessionUnavailable(str(error), {"conversation_id": conversation_id}) from error


async def stop_session(project: Project, conversation_id: int) -> dict:
    """Turn the conversation's session off and end any open build with a BUILDING_DONE (nobody's building
    anything now). Idempotent. Returns what was stopped: {runner: whether one was running}."""
    runner_stopped = await runners.stop_session(project, conversation_id)
    await signals.end_open_build(conversation_id, "The session was stopped")
    return {"runner": runner_stopped}


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


def to_session_content(user_message: dict) -> list[dict]:
    """[Pure] A stored user message as what to push into the session, in the channel's format.

    Text goes as-is. An attachment becomes a text line with its path: the session works IN the
    conversation folder, so "uploads/..." is a path it can open directly. Answers go as one tool_result
    item each, naming the call they answer."""
    return [_to_session_item(block) for block in user_message["content"]]


def _to_session_item(block: dict) -> dict:
    """[Pure] One of our user-message blocks as a channel item. An answer keeps the id of the call it
    answers: the runner matches answers to its waiting questions by it."""
    if block["type"] == "tool_result":
        return {"type": "tool_result", "tool_use_id": block["tool_use_id"], "content": block["content"]}
    if block["type"] == "attachment":
        return {
            "type": "text",
            "text": f"[The user attached a file: {block['path']} ({block['name']}, {block['mime']}, {block['size']} bytes)]",
        }
    return {"type": "text", "text": block.get("text", "")}


def to_stored_content(content: list[dict]) -> list[dict]:
    """[Pure-ish] A message from the session, as we store it: its tool_use items get the `name` the channel
    leaves out -- ask_user for questions, request_actions for actions -- and an `id` if they came without
    one (the runner sends Claude's own, to match the answer back), since answering and pending-detection
    both work by them. Everything else is kept as sent."""
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
