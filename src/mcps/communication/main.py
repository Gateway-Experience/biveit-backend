# Intentions: the communication MCP -- how an agent session talks to the user and to the platform. Claude
# loads it as a development channel (SESSION_COMMAND), which lets it do two things over one stdio
# connection:
#   out of the session:  tools the agent calls. send_message posts its messages for the user (text, plus
#                        questions or actions for them to answer), send_signal records a signal in the
#                        chat's log (BUILDING...), dev_servers controls the project's dev servers. Each
#                        posts to an /api/agent/... endpoint (controllers/agent) with the session's token.
#   into the session:    an HTTP listener on COMMUNICATION_PORT. Whatever the backend POSTs there
#                        ({content: [...]}, with the session's token as bearer) is pushed into Claude as
#                        a channel notification, which it treats like a user message
#                        (services/agent/sessions.send). Anything without the token is refused.
#
# Run by the session's MCP config (services/agent/adapter writes it): python -m src.mcps.communication.main
# with this repo on PYTHONPATH. Reads COMMUNICATION_PORT, COMMUNICATION_OUTBOX_URL, COMMUNICATION_TOKEN.
# Exits when Claude closes stdio.

import asyncio
import hmac
import json
import os
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

import anyio
import mcp.types as mcp_types
from aiohttp import web
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from pydantic import BaseModel, ConfigDict, Field

from src.constants import (
    AGENT_SIGNALS,
    COMMUNICATION_MCP_NAME,
    SESSION_OUTBOX_URL_ENV,
    SESSION_PORT_ENV,
    SESSION_TOKEN_ENV,
    AgentAction,
    DevServerAction,
)
from src.utils import http

_DEFAULT_OUTBOX_URL = "http://localhost:3001"
# Claude Code's development-channel extension: the capability that marks this server as a channel, and
# the notification that pushes a message into the session.
_CHANNEL_CAPABILITY = "claude/channel"
_CHANNEL_NOTIFICATION = "notifications/claude/channel"
# The 2026-07-28 protocol's opening probe -- refused, see serve().
_DISCOVER_METHOD = "server/discover"

_INSTRUCTIONS = (
    "External events and messages are pushed into this session, treat it as a normal user message. You can "
    "then send the message back to the user by using the send message tool. During a turn you can do a lot of "
    "tool calling etc, but only send the user the final message/verdict (however no need to summarize that "
    "final message). If you need the user to choose or confirm something, use questions or actions in the send "
    "message tool. Remember the user is not sitting in front of the claude session so avoid sending "
    "output/interacting directly with them. Always use this mcp's tools for that. "
    "Messages from the user arrive as JSON of the form { content: [item] }, where each item is either "
    '{ type: "text", text: "..." } or { type: "tool_result", content: [answer] }. '
    "tool_result items answer the tool_use items you sent with the send message tool (these are messages of "
    "this channel, unrelated to your own built-in tool calling). "
    "Sending a message that contains tool_use items is blocking: end your turn and wait. The next message from "
    "the user is the matching tool_result message, and it mirrors your message: one tool_result item per "
    "tool_use item, in the same order, and inside each tool_result one answer per question or action, in the "
    "same order. "
    'An answer to a question is { status: "submitted", value: { selected: [selected option texts], custom: '
    "typed answer or null } }. "
    'An answer to an action is { status: "submitted", value: { decision: "approved" | "iterate" } } -- the user '
    "clicked Approve or Keep iterating. An approval takes effect on the platform's side by itself (approving the "
    "spec starts implementing, approving the implementation signs it off): you only ask, and hear the answer. "
    'Any question or action can instead be answered with { status: "aborted" } (the user aborted at this item) '
    'or { status: "skipped" } (the item was skipped, intentionally or because an earlier abort ended the '
    "batch). Treat aborted and skipped items as unanswered; do not assume a choice for them."
)


def _to_json(value: Any) -> str:
    """[Pure] JSON the way the agent reads it best: compact, and non-ASCII text (Vietnamese, say) left
    readable instead of \\u-escaped."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# --- send_message's content: the channel's message format (services/agent/sessions.to_stored_content
# --- is the backend side of it) ----------------------------------------------------------------------


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str | None = Field(None, description="The question being asked")
    options: list[str] = Field(description="Answer options shown to the user")
    multi_select: bool = Field(description="Whether the user can pick more than one option")
    allow_custom: bool = Field(description="Whether the user can type their own answer")


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: AgentAction = Field(
        description=(
            "APPROVE_SPEC: ask the user to approve the spec -- approving starts implementing (the dev environment "
            "gets set up and you'll get a message when it's ready). APPROVE_IMPLEMENTATION: ask the user to sign off "
            "the implemented site (only once implementing has started) -- approving offers them payment and "
            "deployment. The user sees Approve / Keep iterating buttons."
        )
    )
    params: dict[str, Any] = Field(default_factory=dict, description="Action parameters (none for now)")


class QuestionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[Question] = Field(min_length=1)


class ActionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: list[Action] = Field(min_length=1)


class TextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class ToolUseItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_use"]
    input: QuestionsInput | ActionsInput


class InvalidMessage(ToolError):
    """send_message got content it can't send. A ToolError, so the agent sees the reason as the tool's
    error (the SDK hides the message of any other exception)."""


def to_message_body(content: list[TextItem | ToolUseItem]) -> dict:
    """[Pure] send_message's content as the body for "/api/agent/messages". Raises InvalidMessage for an
    empty message, or one mixing questions with actions (the user answers one kind at a time)."""
    if not content:
        raise InvalidMessage("a message needs at least one item")
    inputs = [item.input for item in content if isinstance(item, ToolUseItem)]
    if any(isinstance(i, QuestionsInput) for i in inputs) and any(isinstance(i, ActionsInput) for i in inputs):
        raise InvalidMessage("Cannot mix questions and actions in one message")
    return {"content": [item.model_dump(exclude_none=True) for item in content]}


# --- the server ------------------------------------------------------------------------------------

# post_to_backend(path, body) -> the response body. Raises if the backend refuses.
PostToBackend = Callable[[str, dict | None], Awaitable[dict]]


def create_server(post_to_backend: PostToBackend) -> MCPServer:
    """The MCP server with its tools; every tool goes through `post_to_backend`."""
    server = MCPServer(name=COMMUNICATION_MCP_NAME, instructions=_INSTRUCTIONS, version="1.0.0")

    @server.tool(
        description=(
            "Send a message to the user outside of this session. The message is a list of content items: text "
            "items for plain text, and tool_use items to ask the user questions or propose actions. Text can be "
            "mixed with either, but questions and actions cannot be in the same message. Dont use multiple choice "
            "question format to ask open-ended questions. For that, use a ordinary text question message. This "
            "also means you can't group an open-ended question with a list of multiple choice questions and "
            "open-ended questions have to be sent one by one but that's fine. IMPORTANT: remember this is your "
            "primary way of interacting with the user. Whenever you receive a communication channel message you "
            "must use this tool to response (after doing all your own tool calling and multi-turning of course). "
            "You dont have to narrate back what you did send to the user, and the act of sending with this tool "
            "itself is enough."
        )
    )
    async def send_message(content: Annotated[list[TextItem | ToolUseItem], Field(min_length=1)]) -> str:
        await post_to_backend("/api/agent/messages", to_message_body(content))
        return "sent"

    @server.tool(
        description=(
            "Check, start, restart or stop this project's dev servers (its backend and its frontend), which are "
            "managed for you -- never run pm2 yourself, other projects share it. Both servers already reload on "
            "save; restart only when one is stuck, crashed, or needs to pick up an env change. Their logs are the "
            "files the ready message named. Returns each server's status and port."
        )
    )
    async def dev_servers(
        action: Annotated[DevServerAction, Field(description="What to do to the dev servers")],
    ) -> str:
        response = await post_to_backend("/api/agent/dev-servers", {"action": action})
        return _to_json(response["servers"])

    @server.tool(
        description=(
            "Record a signal in the chat: a timestamped marker the user's screen reacts to, not a message. "
            "BUILDING: you're starting a big change (the first build, a new feature, a redesign), so the screen "
            "shows the site as being built while the preview may be half-done -- give a one-line description of "
            "what you're building. Small tweaks don't need it. BUILDING_DONE: the big change is done -- ALWAYS "
            "send it when you finish; progress messages to the user in between don't end it."
        )
    )
    async def send_signal(
        # A Literal built from AGENT_SIGNALS, so the schema offers the agent only its own signals and stays in
        # step with the list. Type checkers can't follow a computed Literal, hence the ignore.
        slug: Annotated[
            Literal[tuple(AGENT_SIGNALS)], Field(description="Which signal")  # type: ignore[valid-type]
        ],
        description: Annotated[
            str | None,
            Field(max_length=200, description="BUILDING: one line on what you're building, shown to the user"),
        ] = None,
    ) -> str:
        await post_to_backend(
            "/api/agent/signals", {"slug": slug, **({"description": description} if description else {})}
        )
        return f"Signal {slug} recorded."

    return server


def make_post_to_backend(outbox_url: str, token: str | None) -> PostToBackend:
    """post_to_backend for the real backend at `outbox_url`, sending the session's token. A refusal
    (any non-2xx) raises, so the agent sees the tool fail with the backend's answer."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def _post(path: str, body: dict | None) -> dict:
        status, response, _ = await http.request("POST", f"{outbox_url}{path}", json=body, headers=headers)
        if not 200 <= status < 300:
            # A ToolError, so the agent sees why (the SDK hides the message of any other exception).
            raise ToolError(f"the platform answered {status}: {json.dumps(response)}")
        return response

    return _post


# --- the inbox: messages pushed into the session --------------------------------------------------


def create_inbox(push: Callable[[list], Awaitable[None]], token: str | None) -> web.Application:
    """The HTTP app on COMMUNICATION_PORT: a POST with {content: [...]} pushes it into the session (202).
    It must carry `Bearer <token>` (the session's own token) or it's a 401 and nothing is pushed -- the
    port alone is guessable, and any process on the box can reach it. Fail closed: with no token, every
    push is refused."""

    async def _receive(request: web.Request) -> web.Response:
        if not is_authorized(request.headers.get("Authorization", ""), token):
            return web.Response(status=401, text="unauthorized\n")
        body = await request.json()
        await push(body["content"])
        return web.Response(status=202, text="pushed\n")

    app = web.Application()
    app.router.add_post("/{path:.*}", _receive)  # any path, like the original; other methods get a 405
    return app


def is_authorized(authorization: str, token: str | None) -> bool:
    """[Pure] Whether an Authorization header is `Bearer <token>`. Constant-time, so timing can't leak the
    token a byte at a time. Never true without a token."""
    return bool(token) and hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode())


def to_channel_notification(content: list) -> SessionMessage:
    """[Pure] A pushed message as the channel notification Claude reads. Its content must be a string, so
    the message goes in as JSON text -- the format the instructions above describe."""
    notification = mcp_types.JSONRPCNotification(
        jsonrpc="2.0", method=_CHANNEL_NOTIFICATION, params={"content": _to_json({"content": content})}
    )
    return SessionMessage(notification)


def is_discover_request(message: SessionMessage | Exception) -> bool:
    """[Pure] Whether this message from Claude is a `server/discover` request."""
    return (
        isinstance(message, SessionMessage)
        and isinstance(message.message, mcp_types.JSONRPCRequest)
        and message.message.method == _DISCOVER_METHOD
    )


def to_method_not_found(request: mcp_types.JSONRPCRequest) -> SessionMessage:
    """[Pure] The error answer that tells Claude we don't speak `server/discover`."""
    error = mcp_types.ErrorData(code=mcp_types.METHOD_NOT_FOUND, message=f"{request.method} is not supported")
    return SessionMessage(mcp_types.JSONRPCError(jsonrpc="2.0", id=request.id, error=error))


async def serve(port: int, outbox_url: str, token: str | None) -> None:
    """Run the MCP over stdio and the inbox on `port` until Claude closes stdio.

    Gotcha: this has to stay on the handshake-era protocol (initialize), because Claude Code only takes
    channel notifications there -- on 2026-07-28+ over stdio it logs "Channel notifications skipped ... no
    unsolicited notification path" and silently drops every message the inbox pushes. Claude opens with a
    `server/discover` probe, and the SDK locks a connection to the era of its first request, so we answer
    that probe ourselves with "method not found" before the SDK sees it. Claude then falls back to
    initialize, and the SDK serves the connection in the handshake era."""
    await http.connect()
    server = create_server(make_post_to_backend(outbox_url, token))
    lowlevel = server._lowlevel_server  # the SDK only takes experimental capabilities at this level
    async with stdio_server() as (stdio_read_stream, write_stream):
        # Pushes write straight onto the stdio stream the server answers on: a notification needs no request
        # to hang off, and that stream is ours.
        inbox = web.AppRunner(
            create_inbox(lambda content: write_stream.send(to_channel_notification(content)), token),
            access_log=None,
        )
        await inbox.setup()
        await web.TCPSite(inbox, "127.0.0.1", port).start()
        relay_send_stream, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)

        async def _relay_without_discover() -> None:
            async with stdio_read_stream, relay_send_stream:
                async for message in stdio_read_stream:
                    if is_discover_request(message):
                        await write_stream.send(to_method_not_found(message.message))
                    else:
                        await relay_send_stream.send(message)

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(_relay_without_discover)
                await lowlevel.run(
                    read_stream,
                    write_stream,
                    lowlevel.create_initialization_options(experimental_capabilities={_CHANNEL_CAPABILITY: {}}),
                )
        finally:
            await inbox.cleanup()
            await http.close()


if __name__ == "__main__":
    asyncio.run(
        serve(
            int(os.environ[SESSION_PORT_ENV]),
            os.environ.get(SESSION_OUTBOX_URL_ENV, _DEFAULT_OUTBOX_URL),
            os.environ.get(SESSION_TOKEN_ENV),
        )
    )
