# Intentions: a conversation's agent session as the rest of the app sees it -- making sure it's running
# (the OS side is adapter.py) with projects.current_port pointing at it, and talking to it over its
# event channel, translating between our stored messages and the channel's wire format.
#
# THE CHANNEL (the session's `event-channel` MCP server, mcps/src/services/communication-mcp):
#   into the session:  POST http://127.0.0.1:<port>/ {content: [item]}, item = {type: "text", text} or
#                      {type: "tool_result", content: [answer]}. Answers carry NO tool_use_id -- they're
#                      matched BY ORDER to the tool_use items of the session's last message, and must all
#                      come in one message (which our submissions guarantee: the FE answers a whole
#                      message's questions at once). Answer statuses: submitted | aborted | skipped.
#   out of it:         its send_message tool posts {content} back (-> receive_session_message), where
#                      tool_use items have NO id or name, just input {questions} or {actions}.
# Our side stores the API-shaped version (ids, names, tool_use_id) -- see to_stored_content and
# to_session_content for the two translations.

import asyncio
import uuid

from src.constants import (
    ASK_USER_TOOL_NAME,
    REQUEST_ACTIONS_TOOL_NAME,
    SESSION_SEND_ATTEMPTS,
    InteractionStatus,
)
from src.dto.projects import Project
from src.repositories import projects as projects_repo
from src.services.agent import adapter
from src.utils import http
from src.utils.common import exponential_backoff, with_retries


class SessionUnavailable(Exception):
    """The session couldn't be reached or refused the message. The user's message is already stored; it
    just didn't get through. 503."""


async def ensure_session(project: Project, conversation_id: int) -> int:
    """Make sure the conversation's session is up and return its port, recording it as the project's
    current_port. Raises SessionUnavailable if it can't be brought up.

    Goes through adapter.ensure_workspace, so a workspace that was never set up gets seeded before the
    session starts in it. Idempotent -- safe on every visit and before every send.

    current_port is only a hint -- the running process is the truth. It's rewritten whenever the live
    port differs, and any OTHER project still holding this port (its session died and the OS handed the
    port out again) gets it cleared, so a port maps to at most one project -- which is how the session's
    messages find their way back (receive_session_message). It lives on the project only because a
    project has one conversation for now; it moves to the conversation once there can be several."""
    try:
        port = await adapter.ensure_workspace(project, conversation_id)
    except adapter.SessionStartFailed as error:
        raise SessionUnavailable(str(error), {"conversation_id": conversation_id}) from error
    stale_holders = [held for held in await projects_repo.find({"current_port": port}) if held["id"] != project["id"]]
    await asyncio.gather(*[projects_repo.update(held["id"], {"current_port": None}) for held in stale_holders])
    if port != project.get("current_port"):
        await projects_repo.update(project["id"], {"current_port": port})
    return port


async def send(port: int, content: list[dict]) -> None:
    """Push a message into the session on `port`. Retries connection failures for a few seconds, since a
    session that was just started takes a moment to begin listening. Raises SessionUnavailable if it
    never answers, or answers with anything but success."""
    try:
        status, _, _ = await with_retries(
            lambda: http.request("POST", f"http://127.0.0.1:{port}/", json={"content": content}),
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
    """[Pure] One of our user-message blocks as a channel item."""
    if block["type"] == "tool_result":
        return {"type": "tool_result", "content": [_to_session_answer(answer) for answer in block["content"]]}
    if block["type"] == "attachment":
        return {
            "type": "text",
            "text": f"[The user attached a file: {block['path']} ({block['name']}, {block['mime']}, {block['size']} bytes)]",
        }
    return {"type": "text", "text": block.get("text", "")}


def _to_session_answer(answer: dict) -> dict:
    return {**answer, "status": "skipped"} if answer["status"] == InteractionStatus.UNTOUCHED else answer


def to_stored_content(content: list[dict]) -> list[dict]:
    """[Pure-ish] A message from the session, as we store it: its tool_use items get the `id` and `name`
    the channel leaves out -- ask_user for questions, request_actions for actions -- since answering and
    pending-detection both work by them. Everything else is kept as sent."""
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
