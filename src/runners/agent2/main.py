# Intentions: agent2's runner -- one process per conversation, holding its Claude Agent SDK session, and
# carrying the conversation through Claude Code's OWN mechanics:
#   the agent's replies    the final text of each turn goes to the chat as its message (text between tool
#                          calls stays internal). Subagents' text never does.
#   questions              Claude Code's AskUserQuestion reaches us as a permission request (can_use_tool).
#                          The runner posts it to the chat and WAITS -- the tool call stays pending -- until
#                          the user's answer arrives, then hands it back to Claude as the tool's answer.
#   approvals              the one thing Claude Code has no mechanic for: our request_approval tool, waiting
#                          for the user's click the same way.
#   permission prompts     there's no one to ask: refused straight away, with a message the agent can act on.
#   the user's messages    pushed into the runner's inbox, and into the session as user turns (streaming
#                          input -- queued behind whatever turn is running). Answers resolve what's waiting.
# Plus two platform tools (send_signal, dev_servers). Every tool and every chat post goes to an /api/agent/...
# endpoint with the session's token -- the same ones the first runtime uses.
#
# The inbox is HTTP on a unix socket (AGENT_SOCKET): POST /messages {content: [...]}, GET /health, POST
# /shutdown -- all needing the session's token. The runner's working directory is the conversation's
# workspace (the backend starts it there); it continues the workspace's latest Claude session if there is
# one, so a restarted runner picks up where it left off. Ends when told to (/shutdown, SIGTERM), or when
# Claude Code itself goes away -- the backend starts a new one on the next message.
#
# TODO: keep the runner in sync with the backend's log -- the log is the truth (what the user saw and
# answered); the runner's in-memory waits and the Claude transcript can drift from it after a past failure,
# and stay stuck even once everything's healthy:
#   - waiting on a question the log says is answered (the answer's push failed while the runner stayed
#     up): every later message queues behind the blocked turn. Plain text arriving at all means the backend
#     sees nothing pending (it refuses text otherwise), so that's the cue to dismiss whatever's waiting.
#   - a poisoned transcript (something in it the endpoint rejects -- DeepSeek and new block types, images):
#     every turn fails forever. After repeated failed turns, start a fresh session seeded from the log.
# TODO: surface errors to the user -- API errors, overload, rate limits (RateLimitEvent), a failed turn
# (ResultMessage.is_error) only reach the runner's log for now: the user sees silence, or the failed turn's
# last narration posted as if it were the reply (see _follow_session). Also MCP
# elicitation can't be declined from here (the Python SDK has no Elicitation hook); none of our MCPs use it.
#
# Run by services/agent2: python -m src.runners.agent2.main, with this repo on PYTHONPATH. Reads AGENT_SOCKET,
# COMMUNICATION_OUTBOX_URL, COMMUNICATION_TOKEN (and ANTHROPIC_API_KEY, which Claude Code reads itself).
# Never loads the backend's settings (see src/runners/__init__.py).

import asyncio
import contextlib
import fcntl
import hmac
import json
import os
import signal
import socket
import uuid
import warnings
from collections.abc import Awaitable, Callable, Generator
from types import SimpleNamespace
from typing import Any

from aiohttp import web
from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SdkMcpTool,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
    list_sessions,
    tool,
)

from src.constants import (
    AGENT2_CLAIM_WAIT_SECONDS,
    AGENT2_PERMISSION_MODE,
    AGENT2_QUESTION_SHOWN_WAIT_SECONDS,
    AGENT2_SOCKET_ENV,
    AGENT_SIGNALS,
    COMMUNICATION_MCP_NAME,
    SESSION_OUTBOX_URL_ENV,
    SESSION_TOKEN_ENV,
    AgentAction,
    DevServerAction,
)
from src.runners.agent2 import channel
from src.utils import http

# post_to_backend(path, body) -> the response body. Raises if the backend refuses.
PostToBackend = Callable[[str, dict | None], Awaitable[dict]]
# ask_user(tool name, tool_use id if known, interaction id -> tool_use block) -> the user's results for it,
# once they've answered. See make_ask_user.
AskUser = Callable[[str, str | None, Callable[[str], dict]], Awaitable[list[dict]]]

_ASK_USER_QUESTION = "AskUserQuestion"
# Built-in tools that only make sense with someone at the terminal.
_DISALLOWED_TOOLS = ["ExitPlanMode"]
_OUR_TOOL_NAMES = ("request_approval", "send_signal", "dev_servers")
_REQUEST_APPROVAL = f"mcp__{COMMUNICATION_MCP_NAME}__request_approval"
# The tools that ask the user something, as Claude Code names them in the stream: the text before them is
# their lead-up, not narration (read_main_message), and their askers wait for the stream to show them
# (claim_question). Add every tool that calls ask_user -- forgetting one fails quietly: its lead-up gets
# dropped, and its question waits out AGENT2_QUESTION_SHOWN_WAIT_SECONDS first.
_QUESTION_TOOL_NAMES = (_ASK_USER_QUESTION, _REQUEST_APPROVAL)
# The SDK warns that our own tools skip the permission callback -- which is the point: they're pre-approved.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)


def make_post_to_backend(outbox_url: str, token: str) -> PostToBackend:
    """post_to_backend for the real backend at `outbox_url`, sending the session's token. A refusal (any
    non-2xx) raises -- inside a tool, the SDK hands the agent the message as the tool's error."""
    headers = {"Authorization": f"Bearer {token}"}

    async def _post(path: str, body: dict | None) -> dict:
        status, response, _ = await http.request("POST", f"{outbox_url}{path}", json=body, headers=headers)
        if not 200 <= status < 300:
            raise RuntimeError(f"the platform answered {status}: {json.dumps(response)}")
        return response

    return _post


# --- waiting on the user ------------------------------------------------------------------------------


# This is generally safe from race condition because runner is single-threaded. There are a few logical races though:
# - A subagent's approval can take the main agent's text.
#   Say the main agent writes "I'll have a helper check the menu", then a subagent calls request_approval.
#   That narration becomes the approval's lead-up. It's harmless, just a slightly odd lead-up. (Not via
#   AskUserQuestion: subagents don't get it -- only our MCP tools can reach them.)
# - Two questions at once (parallel tool calls, or parallel subagents' approvals):
#   the first one takes the lead text and the second gets none. Each waits on its own id, so answers can't mix.
# - A duplicate answer (say, a double submit):
#   the second copy finds the Future already done, so it's left over and reaches the agent as a "late answers" user turn.
#   That's mildly confusing for the agent, not broken.
# Conv state is only meant to be current and never accumulative.
def make_conversation_state() -> SimpleNamespace:
    """The runner's only mutable state, kept in here: everything else gets these functions, never the state.
    Two things need a shared spot: a tool call waiting for the user's answer, which arrives via the inbox;
    and the agent's latest text, noted while following the session and taken by a question or the reply."""
    waiting: dict[str, asyncio.Future] = {}
    latest: dict[str, str | None] = {"text": None}
    # Question tool_use id -> its tool name: shown by the stream, not claimed yet. Within the turn only (end_turn).
    shown: dict[str, str] = {}
    shown_changed = asyncio.Condition()

    def note_text(text: str) -> None:
        """The main agent said something; the latest wins."""
        latest["text"] = text

    def take_text() -> str | None:
        """Hand out the latest text and clear it -- whoever takes it (a question's lead-up, or the turn's
        reply) is the only one to post it, so the same words never reach the chat twice."""
        text, latest["text"] = latest["text"], None
        return text

    @contextlib.contextmanager
    def waiting_for(interaction_id: str) -> Generator[asyncio.Future]:
        """With-block giving the Future that the answer to `interaction_id` resolves. Enter it BEFORE posting
        the question, so the answer can't arrive first; it's dropped on exit, answered or not."""
        waiting[interaction_id] = asyncio.get_running_loop().create_future()
        try:
            yield waiting[interaction_id]
        finally:
            waiting.pop(interaction_id, None)

    def resolve_answers(items: list[dict]) -> list[dict]:
        """Take one inbox message's items: each answer to a question we're waiting on wakes that question
        up, and is used up. Returns the rest, for the agent as a normal user turn -- the user's words, and
        answers nobody waits for any more (asked before a restart, or already answered)."""
        awaited = [item for item in items if item["type"] == "tool_result" and _is_open(item.get("tool_use_id"))]
        for answer in awaited:  # resolving is the whole point: one side effect per answer
            waiting[answer["tool_use_id"]].set_result(answer["content"])
        return [item for item in items if not any(item is answer for answer in awaited)]

    def _is_open(interaction_id: str | None) -> bool:
        future = waiting.get(interaction_id) if interaction_id else None
        return future is not None and not future.done()

    async def note_question(tool_use_id: str, tool_name: str) -> None:
        """The stream showed a question call -- so the text before it has been noted by now."""
        async with shown_changed:
            shown[tool_use_id] = tool_name
            shown_changed.notify_all()

    async def claim_question(tool_name: str, tool_use_id: str | None) -> str | None:
        """Wait until the stream has shown this question, so take_text then gets the text right before it --
        the asker can run before the stream is read that far. By id when the asker has it (AskUserQuestion);
        else the oldest unclaimed one by name (an MCP tool's handler isn't told its id; calls come in the
        order Claude made them). Returns Claude's id for it -- or None after AGENT2_QUESTION_SHOWN_WAIT_SECONDS,
        which shouldn't happen: the question then goes out without its lead-up rather than not at all."""

        def _find() -> str | None:
            if tool_use_id is not None:
                return tool_use_id if tool_use_id in shown else None
            return next((shown_id for shown_id, name in shown.items() if name == tool_name), None)

        async with shown_changed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shown_changed.wait_for(_find), AGENT2_QUESTION_SHOWN_WAIT_SECONDS)
            found = _find()
            if found:
                shown.pop(found)
            return found

    def end_turn() -> str | None:
        """The turn is over: returns its reply (take_text), and forgets questions shown but never claimed --
        only the timeout path leaves any, and a question can't outlive its turn (an asker waiting on the user
        holds the turn open). So between turns nothing's left here but what's waiting on the user, and a
        long-running runner behaves just like a fresh one."""
        shown.clear()
        return take_text()

    return SimpleNamespace(
        note_text=note_text,
        take_text=take_text,
        end_turn=end_turn,
        note_question=note_question,
        claim_question=claim_question,
        waiting_for=waiting_for,
        resolve_answers=resolve_answers,
        get_waiting_ids=lambda: list(waiting),
    )


def make_ask_user(post_to_backend: PostToBackend, state: SimpleNamespace) -> AskUser:
    """ask_user: post an interaction to the chat -- the agent's text right before the question first, as its
    lead-up -- and wait for the user's results, which come in through the inbox (state.resolve_answers).
    Takes the question's tool name (one of _QUESTION_TOOL_NAMES), its tool_use id when the caller knows it,
    and `to_block`, building the chat's tool_use block for the interaction's id -- which is Claude's own id
    for the call once the stream has shown it (state.claim_question)."""

    async def _ask(tool_name: str, tool_use_id: str | None, to_block: Callable[[str], dict]) -> list[dict]:
        claimed_id = await state.claim_question(tool_name, tool_use_id)
        interaction_id = claimed_id or tool_use_id or f"toolu_{uuid.uuid4().hex[:24]}"
        with state.waiting_for(interaction_id) as answer:
            lead_text = state.take_text()
            content = [*([{"type": "text", "text": lead_text}] if lead_text else []), to_block(interaction_id)]
            await post_to_backend("/api/agent/messages", {"content": content})
            return await answer

    return _ask


def make_can_use_tool(ask_user: AskUser) -> Callable:
    """The permission callback: what Claude Code would otherwise ask a person at the terminal. Questions go
    to the user; every other prompt is refused right away (the agent hears why and carries on)."""

    async def _can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name != _ASK_USER_QUESTION:
            print(f"refused a permission prompt: {tool_name}", flush=True)
            return PermissionResultDeny(
                message="This needs a person's permission, and nobody's at the terminal here. Find another "
                + "way, or tell the user what you'd need.",
                interrupt=False,
            )
        results = await ask_user(
            _ASK_USER_QUESTION,
            context.tool_use_id,
            lambda interaction_id: channel.to_ask_user_block(interaction_id, tool_input),
        )
        answers = channel.to_answers(tool_input, results)
        if answers is None:
            return PermissionResultDeny(message=channel.to_refusal(tool_input, results), interrupt=False)
        # This sends the result back to the agent so we dont have to send to anywhere else.
        # And the BE already stored the answers before it got to the runner.
        return PermissionResultAllow(updated_input={**tool_input, "answers": answers})

    return _can_use_tool


# --- the agent's tools (an in-process MCP server) -----------------------------------------------------


def create_tools(post_to_backend: PostToBackend, ask_user: AskUser) -> list[SdkMcpTool]:
    """The platform's tools for the agent. The SDK checks the arguments against each schema before a handler
    runs, and turns a raised error into the tool's error result."""

    @tool(
        "request_approval",
        "Ask the user to approve something, and wait for their answer. APPROVE_SPEC: the spec is ready -- "
        "approving starts implementing (the dev environment gets set up, and you'll get a message when it's "
        "ready). APPROVE_IMPLEMENTATION: the site is done -- approving signs it off and offers them payment and "
        "deployment (only once implementing has started). They see Approve / Keep iterating buttons, with your "
        "text right before this call shown above them. Returns their decision.",
        {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": [str(action) for action in AgentAction]}},
            "required": ["action"],
            "additionalProperties": False,
        },
    )
    async def request_approval(args: dict[str, Any]) -> dict[str, Any]:
        results = await ask_user(
            _REQUEST_APPROVAL,
            None,
            lambda interaction_id: {
                "type": "tool_use",
                "id": interaction_id,
                "input": {"actions": [{"name": args["action"], "params": {}}]},
            },
        )
        return _to_tool_result(channel.to_decision(results))

    @tool(
        "send_signal",
        "Record a signal in the chat: a timestamped marker the user's screen reacts to, not a message. "
        "BUILDING: you're starting a big change (the first build, a new feature, a redesign), so the screen shows "
        "the site as being built while the preview may be half-done -- give a one-line description of what you're "
        "building. Small tweaks don't need it. BUILDING_DONE: the big change is done -- ALWAYS send it when you "
        "finish. A build shows for an hour after the latest BUILDING, so for a longer one send BUILDING again "
        "to keep it on.",
        {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "enum": [str(slug) for slug in AGENT_SIGNALS]},
                "description": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "BUILDING: one line on what you're building, shown to the user",
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    )
    async def send_signal(args: dict[str, Any]) -> dict[str, Any]:
        await post_to_backend("/api/agent/signals", args)
        return _to_tool_result(f"Signal {args['slug']} recorded.")

    @tool(
        "dev_servers",
        "Check, start, restart or stop this project's dev servers (its backend and its frontend), which are "
        "managed for you -- never run pm2 yourself, other projects share it. Both servers already reload on save; "
        "restart only when one is stuck, crashed, or needs to pick up an env change. Their logs are the files the "
        "ready message named. Returns each server's status and port.",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [str(action) for action in DevServerAction],
                    "description": "What to do to the dev servers",
                }
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    )
    async def dev_servers(args: dict[str, Any]) -> dict[str, Any]:
        response = await post_to_backend("/api/agent/dev-servers", {"action": args["action"]})
        return _to_tool_result(json.dumps(response["servers"], ensure_ascii=False, separators=(",", ":")))

    return [request_approval, send_signal, dev_servers]


def _to_tool_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


# --- the inbox: messages pushed into the session -------------------------------------------------------


def is_authorized(authorization: str, token: str | None) -> bool:
    """[Pure] Whether an Authorization header is `Bearer <token>`. Constant-time, so timing can't leak the
    token a byte at a time. Never true without a token."""
    return bool(token) and hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode())


def create_inbox(push: Callable[[list], Awaitable[None]], token: str | None, stop: asyncio.Event) -> web.Application:
    """The HTTP app on the runner's socket. Every route needs `Bearer <token>` (401 otherwise, nothing done):
    the socket's file permissions already keep others out, and this keeps it that way once it's shared
    (a container mount, say). Fail closed: with no token, everything is refused.
      POST /messages  {content: [...]} -> `push` (answers to what's waiting, the rest as a user turn) (202)
      GET  /health    200 while the runner is up -- how the backend finds out whether to start one
      POST /shutdown  ends the runner (202)"""

    @web.middleware
    async def _require_token(request: web.Request, handler: Callable) -> web.StreamResponse:
        if not is_authorized(request.headers.get("Authorization", ""), token):
            return web.Response(status=401, text="unauthorized\n")
        return await handler(request)

    async def _receive(request: web.Request) -> web.Response:
        body = await request.json()
        await push(body["content"])
        return web.Response(status=202, text="pushed\n")

    async def _health(_: web.Request) -> web.Response:
        return web.json_response({"status": "up"})

    async def _shutdown(_: web.Request) -> web.Response:
        stop.set()
        return web.Response(status=202, text="stopping\n")

    app = web.Application(middlewares=[_require_token])
    app.router.add_post("/messages", _receive)
    app.router.add_get("/health", _health)
    app.router.add_post("/shutdown", _shutdown)
    return app


# --- the session -------------------------------------------------------------------------------------


def to_options(workspace_dir: str, communication_server: Any, can_use_tool: Callable) -> ClaudeAgentOptions:
    """[Pure-ish: reads the workspace's Claude sessions] The SDK options for the conversation's session.
    - the workspace's CLAUDE.md and .claude/ settings (its deny rules included) come in as PROJECT settings;
      the machine's user settings stay out
    - INSTRUCTIONS on top of Claude Code's own system prompt
    - our tools pre-approved; whatever would still prompt goes to `can_use_tool` (AGENT2_PERMISSION_MODE
      decides the rest without asking)
    - continue the workspace's latest session if it has one: a workspace holds exactly one conversation,
      so "the latest" is ours"""
    return ClaudeAgentOptions(
        cwd=workspace_dir,
        setting_sources=["project"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": channel.INSTRUCTIONS},
        mcp_servers={COMMUNICATION_MCP_NAME: communication_server},
        allowed_tools=[f"mcp__{COMMUNICATION_MCP_NAME}__{name}" for name in _OUR_TOOL_NAMES],
        disallowed_tools=_DISALLOWED_TOOLS,
        permission_mode=AGENT2_PERMISSION_MODE,
        can_use_tool=can_use_tool,
        continue_conversation=bool(list_sessions(workspace_dir, limit=1)),
    )


def claim_socket(socket_path: str) -> int | None:
    """Take the conversation's socket for this runner: lock `<socket>.lock` and clear any leftover socket
    file, so it's free to listen on. Returns the lock's file descriptor -- keep it open for as long as the
    runner lives -- or None when another runner holds it (this one should just exit).
    The lock and not the socket itself, because binding isn't one: asyncio quietly replaces an existing
    socket file, so a second runner would steal the path. A flock is released by the OS when its process
    dies however it dies, so a dead runner never leaves a stale lock behind."""
    lock_fd = os.open(f"{socket_path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return None
    with contextlib.suppress(FileNotFoundError):
        os.unlink(socket_path)  # a dead runner's leftover
    return lock_fd


async def _wait_for_socket(socket_path: str, deadline: float) -> int | None:
    """Recursive: claim_socket, retried until `deadline` while the holder isn't listening -- a runner that's
    shutting down (stopped right before this one was started) holds the socket a few seconds more, and so
    does one still starting up. None as soon as the holder listens: that one's live, and it's the one."""
    lock_fd = claim_socket(socket_path)
    if lock_fd is not None or _is_listening(socket_path) or asyncio.get_running_loop().time() > deadline:
        return lock_fd
    await asyncio.sleep(0.2)
    return await _wait_for_socket(socket_path, deadline)


def _is_listening(socket_path: str) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.connect(socket_path)
        except OSError:
            return False
        return True


def read_main_message(message: Any) -> tuple[bool, str | None]:
    """[Pure] What a streamed message means for the main agent's held text: (whether it calls a tool that
    isn't a question -- so any text held from before was narration -- and the text it ends with, if any).
    Subagents' messages (parent_tool_use_id set) and non-assistant ones mean nothing: (False, None).
    A question (AskUserQuestion, request_approval) doesn't count as a tool call here: the text right before
    it is its lead-up."""
    if not isinstance(message, AssistantMessage) or message.parent_tool_use_id is not None:
        return False, None
    tool_call_indexes = [
        index
        for index, block in enumerate(message.content)
        if isinstance(block, ToolUseBlock) and block.name not in _QUESTION_TOOL_NAMES
    ]
    after_tools = message.content[tool_call_indexes[-1] + 1 :] if tool_call_indexes else message.content
    text = "\n\n".join(block.text for block in after_tools if isinstance(block, TextBlock)) or None
    return bool(tool_call_indexes), text


def get_question_calls(message: Any) -> list[ToolUseBlock]:
    """[Pure] The question calls in a streamed message -- the main agent's or a subagent's (subagents can't
    use AskUserQuestion, but they can call request_approval) -- whose askers wait for the stream to show
    them (claim_question)."""
    if not isinstance(message, AssistantMessage):
        return []
    return [
        block for block in message.content if isinstance(block, ToolUseBlock) and block.name in _QUESTION_TOOL_NAMES
    ]


async def _follow_session(client: ClaudeSDKClient, post_to_backend: PostToBackend, state: SimpleNamespace) -> None:
    """Follow everything the session says until Claude Code goes away, posting each turn's reply.

    Only text the agent ends its turn with is its reply; text followed by a tool call is narration ("Let me
    check the menu page...") and never shown. A text can't tell which it is when it arrives -- what comes
    next decides -- so the latest one is held (read_main_message), until:
      more text         it replaces the held one
      a tool call       the held one was narration: dropped
      a question        takes it as its lead-up (make_ask_user) -- shown above the question
      ResultMessage     the turn is over (Claude Code sends it once the agent stops): what's held is the reply.
                        Strict: a turn ending on a tool call has nothing held, and posts nothing -- the
                        instructions tell the agent to always end on a message (channel.INSTRUCTIONS)

    A question's asker (a permission callback or a tool handler) runs in its own task, possibly before this
    loop has read the question's message -- say, while it's posting a reply. So each question call is noted
    once its text is (state.note_question), and the asker waits for that before taking the lead-up.

    Gotcha: a turn that FAILED (API error, rate limit, max turns -- message.is_error) ends with a
    ResultMessage too, and its held text may be cut short. Not handled yet: see the TODO at the top."""
    async for message in client.receive_messages():
        calls_tool, text = read_main_message(message)
        if calls_tool:
            state.take_text()  # dropped: it was narration
        if text:
            state.note_text(text)
        for question in get_question_calls(message):  # after the text: its lead-up is held by now
            await state.note_question(question.id, question.name)
        if isinstance(message, ResultMessage):
            print(f"turn done: {message.subtype}, ${message.total_cost_usd or 0:.4f}", flush=True)
            reply = state.end_turn()
            if reply:
                await _post_reply(post_to_backend, reply)


async def _post_reply(post_to_backend: PostToBackend, reply: str) -> None:
    """Post the agent's reply to the chat. A failure is only logged: the turn is over either way, and the
    user can ask again."""
    try:
        await post_to_backend("/api/agent/messages", {"content": [{"type": "text", "text": reply}]})
    except Exception as error:
        print(f"couldn't post the reply: {error}", flush=True)


async def serve(socket_path: str, outbox_url: str, token: str | None) -> None:
    """Run the conversation's session and its inbox until told to stop or until Claude Code goes away."""
    lock_fd = await _wait_for_socket(socket_path, asyncio.get_running_loop().time() + AGENT2_CLAIM_WAIT_SECONDS)
    if lock_fd is None:
        print(f"another runner is live on {socket_path}; exiting", flush=True)
        return
    await http.connect()
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    post_to_backend = make_post_to_backend(outbox_url, token or "")
    state = make_conversation_state()
    ask_user = make_ask_user(post_to_backend, state)
    communication_server = create_sdk_mcp_server(COMMUNICATION_MCP_NAME, tools=create_tools(post_to_backend, ask_user))
    options = to_options(os.getcwd(), communication_server, make_can_use_tool(ask_user))
    try:
        async with ClaudeSDKClient(options) as client:

            async def _push(items: list[dict]) -> None:
                left_over = state.resolve_answers(items)
                if left_over:
                    await client.query(channel.to_user_turn(left_over))

            inbox = web.AppRunner(create_inbox(_push, token, stop), access_log=None)
            await inbox.setup()
            await web.UnixSite(inbox, socket_path).start()
            os.chmod(socket_path, 0o600)
            print(f"listening on {socket_path}", flush=True)
            session_ended = asyncio.create_task(_follow_session(client, post_to_backend, state))
            stopped = asyncio.create_task(stop.wait())
            await asyncio.wait({session_ended, stopped}, return_when=asyncio.FIRST_COMPLETED)
            session_ended.cancel()
            stopped.cancel()
            await inbox.cleanup()
    finally:
        await http.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(socket_path)
        os.close(lock_fd)  # last: the socket's gone by the time a next runner can take over


if __name__ == "__main__":
    asyncio.run(
        serve(
            os.environ[AGENT2_SOCKET_ENV],
            os.environ[SESSION_OUTBOX_URL_ENV],
            os.environ.get(SESSION_TOKEN_ENV),
        )
    )
