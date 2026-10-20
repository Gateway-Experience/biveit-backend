# agent2 tests: the runner (src/runners/agent2) and the backend's side of it (services/agent2). Only the
# Claude Agent SDK client is faked (FakeClaudeClient: it records what's pushed into the session, and streams
# whatever messages a test feeds it) -- the runner's inbox runs for real on a unix socket, and the backend
# really talks to it over that socket.

import asyncio
import json
import os
import sys
from typing import ClassVar

import jsonschema
import pytest
from aiohttp.test_utils import TestClient, TestServer
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from src.config import settings
from src.runners.agent2 import channel
from src.runners.agent2 import main as runner
from src.services import agent2
from src.services.agent2 import sessions as agent2_sessions
from src.utils import systemd
from src.utils.processes import CommandFailed

_TOKEN = "session-token"


class FakeClaudeClient:
    """Stands in for ClaudeSDKClient: records the options and every user turn pushed in, and streams what's
    fed to it (`feed`). Its session "ends" (Claude Code going away) on `end()` or when it's closed."""

    instances: ClassVar[list["FakeClaudeClient"]] = []

    def __init__(self, options):
        self.options = options
        self.turns: list[str] = []
        self.stream: asyncio.Queue = asyncio.Queue()
        FakeClaudeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.end()

    async def query(self, prompt):
        self.turns.append(prompt)

    async def receive_messages(self):
        message = await self.stream.get()
        while message is not None:
            yield message
            message = await self.stream.get()

    def feed(self, *messages):
        for message in messages:
            self.stream.put_nowait(message)

    def end(self):
        self.stream.put_nowait(None)


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch):
    FakeClaudeClient.instances = []
    monkeypatch.setattr(runner, "ClaudeSDKClient", FakeClaudeClient)
    monkeypatch.setattr(settings, "session_token_secret", "session-token-secret-for-tests")


def _text(text):
    return {"type": "text", "text": text}


def _said(text, subagent=None):
    """What the main agent (or a subagent, given its parent call's id) says, as the SDK streams it."""
    return AssistantMessage(content=[TextBlock(text)], model="m", parent_tool_use_id=subagent)


def _calls(name, *texts_before):
    """The main agent saying `texts_before`, then calling the tool `name`, in one streamed message."""
    content = [*[TextBlock(text) for text in texts_before], ToolUseBlock(id="toolu_x", name=name, input={})]
    return AssistantMessage(content=content, model="m", parent_tool_use_id=None)


def _turn_done():
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s"
    )


def _submitted(selected=(), custom=None):
    return {"status": "submitted", "value": {"selected": list(selected), "custom": custom}}


_ASK = {
    "questions": [
        {
            "question": "Which layout?",
            "header": "Layout",
            "options": [{"label": "Grid", "description": "Cards in rows"}, {"label": "List", "description": ""}],
            "multiSelect": False,
        },
        {
            "question": "Which pages?",
            "header": "Pages",
            "options": [{"label": "Menu", "description": "Prices"}, {"label": "Map", "description": "Where we are"}],
            "multiSelect": True,
        },
    ]
}


# --- channel ---


def test_claude_questions_become_chat_questions_with_subtitles():
    block = channel.to_ask_user_block("toolu_x", _ASK)
    assert block["id"] == "toolu_x" and block["type"] == "tool_use"
    first, second = block["input"]["questions"]
    assert first == {
        "question": "Which layout?",
        "options": [{"label": "Grid", "description": "Cards in rows"}, {"label": "List"}],  # empty subtitle left out
        "multi_select": False,
        "allow_custom": True,  # Claude's questions have no "Other": typing your own is always on
    }
    assert second["multi_select"]


def test_answers_go_back_keyed_by_question_text():
    answers = channel.to_answers(_ASK, [_submitted(custom="Masonry"), _submitted(["Menu"], "Blog")])
    assert answers == {"Which layout?": "Masonry", "Which pages?": ["Menu", "Blog"]}


def test_a_partial_answer_is_a_refusal_that_says_what_was_answered():
    results = [_submitted(["Grid"]), {"status": "aborted"}]
    assert channel.to_answers(_ASK, results) is None
    refusal = channel.to_refusal(_ASK, results)
    assert "Which layout? -> Grid" in refusal and "don't assume" in refusal


def test_decisions_read_plainly():
    assert channel.to_decision([{"status": "submitted", "value": {"decision": "approved"}}]) == "approved"
    assert channel.to_decision([{"status": "submitted", "value": {"decision": "iterate"}}]).startswith("iterate")
    assert "dismissed" in channel.to_decision([{"status": "aborted"}])


def test_user_turns_are_their_words_and_late_answers_are_spelled_out():
    assert channel.to_user_turn([_text("Xin chào")]) == "Xin chào"
    late = channel.to_user_turn([{"type": "tool_result", "tool_use_id": "t", "content": [{"status": "aborted"}]}])
    assert late.startswith("[Answers to the questions you asked before") and '"aborted"' in late


# --- waiting on the user ---


@pytest.fixture
def chat():
    """What the runner posts to the chat, plus an ask_user over it, with the agent's lead-up text noted."""
    state = {"posted": [], "conversation": runner.make_conversation_state()}

    async def post(path, body):
        state["posted"].append((path, body))
        return {"servers": [{"app": "backend", "status": "online"}]} if path.endswith("/dev-servers") else {}

    state["conversation"].note_text("Two quick questions:")
    state["post"] = post
    state["ask_user"] = runner.make_ask_user(post, state["conversation"])
    return state


async def _answer_when_asked(chat, interaction_id, results):
    """Play the user: once the interaction is waiting, answer it the way the inbox would."""
    while interaction_id not in chat["conversation"].get_waiting_ids():
        await asyncio.sleep(0)
    left_over = chat["conversation"].resolve_answers(
        [{"type": "tool_result", "tool_use_id": interaction_id, "content": results}]
    )
    assert left_over == []


async def test_a_claude_question_waits_for_the_user_and_answers_claude(chat):
    await chat["conversation"].note_question("toolu_q", "AskUserQuestion")  # the stream showed it already
    can_use_tool = runner.make_can_use_tool(chat["ask_user"])
    answering = asyncio.create_task(_answer_when_asked(chat, "toolu_q", [_submitted(["Grid"]), _submitted(["Map"])]))
    decision = await can_use_tool("AskUserQuestion", _ASK, ToolPermissionContext(tool_use_id="toolu_q"))
    await answering

    assert isinstance(decision, PermissionResultAllow)
    assert decision.updated_input == {**_ASK, "answers": {"Which layout?": "Grid", "Which pages?": ["Map"]}}
    # Posted with the agent's lead-up text, under Claude's own call id, and nothing's left waiting.
    path, body = chat["posted"][0]
    assert path == "/api/agent/messages" and body["content"][0] == _text("Two quick questions:")
    assert body["content"][1]["id"] == "toolu_q" and chat["conversation"].get_waiting_ids() == []


async def test_a_skipped_question_is_refused_with_the_reason(chat):
    await chat["conversation"].note_question("toolu_q", "AskUserQuestion")
    can_use_tool = runner.make_can_use_tool(chat["ask_user"])
    answering = asyncio.create_task(
        _answer_when_asked(chat, "toolu_q", [{"status": "aborted"}, {"status": "untouched"}])
    )
    decision = await can_use_tool("AskUserQuestion", _ASK, ToolPermissionContext(tool_use_id="toolu_q"))
    await answering
    assert isinstance(decision, PermissionResultDeny) and "skipped" in decision.message and not decision.interrupt


async def test_every_other_permission_prompt_is_refused_at_once(chat):
    decision = await runner.make_can_use_tool(chat["ask_user"])(
        "Bash", {"command": "rm -rf /"}, ToolPermissionContext()
    )
    assert isinstance(decision, PermissionResultDeny) and "nobody's at the terminal" in decision.message
    assert chat["posted"] == []  # never shown to the user


def test_answers_nobody_waits_for_are_left_over_with_the_users_words(chat):
    items = [_text("also, make it blue"), {"type": "tool_result", "tool_use_id": "toolu_gone", "content": []}]
    assert chat["conversation"].resolve_answers(items) == items


async def test_a_question_asked_before_the_stream_shows_it_waits_for_its_lead_up(chat):
    chat["conversation"].take_text()  # nothing held yet: the stream hasn't been read that far
    can_use_tool = runner.make_can_use_tool(chat["ask_user"])
    answering = asyncio.create_task(_answer_when_asked(chat, "toolu_q", [_submitted(["Grid"]), _submitted(["Map"])]))
    asking = asyncio.create_task(can_use_tool("AskUserQuestion", _ASK, ToolPermissionContext(tool_use_id="toolu_q")))
    await asyncio.sleep(0.05)
    assert chat["posted"] == []  # waiting for the stream
    chat["conversation"].note_text("Here are the layouts:")  # ...which now catches up
    await chat["conversation"].note_question("toolu_q", "AskUserQuestion")
    await asking
    await answering
    assert chat["posted"][0][1]["content"][0] == _text("Here are the layouts:")


async def test_a_question_the_stream_never_shows_still_goes_out(chat, monkeypatch):
    monkeypatch.setattr(runner, "AGENT2_QUESTION_SHOWN_WAIT_SECONDS", 0.05)
    can_use_tool = runner.make_can_use_tool(chat["ask_user"])
    answering = asyncio.create_task(_answer_when_asked(chat, "toolu_q", [_submitted(["Grid"]), _submitted(["Map"])]))
    await can_use_tool("AskUserQuestion", _ASK, ToolPermissionContext(tool_use_id="toolu_q"))
    await answering
    assert chat["posted"][0][1]["content"][-1]["id"] == "toolu_q"


async def test_nothing_unclaimed_outlives_its_turn(chat, monkeypatch):
    monkeypatch.setattr(runner, "AGENT2_QUESTION_SHOWN_WAIT_SECONDS", 0.05)
    state = runner.make_conversation_state()
    await state.note_question("toolu_stale", runner._REQUEST_APPROVAL)  # its claim timed out earlier
    state.end_turn()
    assert await state.claim_question(runner._REQUEST_APPROVAL, None) is None  # not picked up by a later one


async def test_the_stream_notes_questions_after_their_text(chat):
    client = FakeClaudeClient(None)
    client.feed(_calls("AskUserQuestion", "Which colour do you want?"))
    client.end()
    state = runner.make_conversation_state()
    await runner._follow_session(client, chat["post"], state)
    assert await state.claim_question("AskUserQuestion", "toolu_x") == "toolu_x"
    assert state.take_text() == "Which colour do you want?"


# --- the runner's tools ---


@pytest.fixture
def tools(chat):
    return {tool.name: tool for tool in runner.create_tools(chat["post"], chat["ask_user"])}


async def test_an_approval_waits_for_the_click(chat, tools):
    await chat["conversation"].note_question("toolu_claude", runner._REQUEST_APPROVAL)
    requesting = asyncio.create_task(tools["request_approval"].handler({"action": "APPROVE_SPEC"}))
    while not chat["conversation"].get_waiting_ids():
        await asyncio.sleep(0)
    [interaction_id] = chat["conversation"].get_waiting_ids()
    assert interaction_id == "toolu_claude"  # claimed by name: Claude's own id for the call
    chat["conversation"].resolve_answers(
        [
            {
                "type": "tool_result",
                "tool_use_id": interaction_id,
                "content": [{"status": "submitted", "value": {"decision": "approved"}}],
            }
        ],
    )
    result = await requesting
    assert result["content"][0]["text"] == "approved"
    [_, body] = chat["posted"][0]
    assert body["content"][1] == {
        "type": "tool_use",
        "id": interaction_id,
        "input": {"actions": [{"name": "APPROVE_SPEC", "params": {}}]},
    }


async def test_signals_and_dev_servers_post_to_the_backend(chat, tools):
    await tools["send_signal"].handler({"slug": "BUILDING", "description": "First version"})
    servers = await tools["dev_servers"].handler({"action": "status"})
    assert json.loads(servers["content"][0]["text"]) == [{"app": "backend", "status": "online"}]
    assert chat["posted"] == [
        ("/api/agent/signals", {"slug": "BUILDING", "description": "First version"}),
        ("/api/agent/dev-servers", {"action": "status"}),
    ]


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("send_signal", {"slug": "IMPLEMENTATION_DONE"}),  # ours to write, not the agent's
        ("dev_servers", {"action": "delete-all"}),
        ("request_approval", {"action": "PAY"}),
    ],
)
def test_the_schemas_keep_the_agent_in_line(name, args, tools):
    # The SDK validates arguments against each tool's schema before its handler runs.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(args, tools[name].input_schema)


# --- following the session ---


async def test_only_each_turns_final_main_text_reaches_the_chat(chat):
    client = FakeClaudeClient(None)
    client.feed(
        _said("Let me look at the menu page..."),  # narration between tool calls: internal
        _said("subagent chatter", subagent="toolu_task"),  # a subagent's: never shown
        _said("Done! Your menu now has prices."),
        _turn_done(),
        _turn_done(),  # a turn with no text posts nothing
    )
    client.end()
    await runner._follow_session(client, chat["post"], runner.make_conversation_state())
    assert chat["posted"] == [("/api/agent/messages", {"content": [_text("Done! Your menu now has prices.")]})]


async def test_text_followed_by_a_tool_call_is_narration_never_the_reply(chat):
    client = FakeClaudeClient(None)
    client.feed(
        _said("Let me fix the header..."),
        _calls("Edit"),
        _turn_done(),  # strict: ended on a tool call, so no reply at all
        _calls("Read", "Checking the menu page..."),
        _said("Which colour do you want?"),
        _calls("AskUserQuestion"),  # a question isn't a tool call here: the text before it stays its lead-up
    )
    client.end()
    state = runner.make_conversation_state()
    await runner._follow_session(client, chat["post"], state)
    assert chat["posted"] == [] and state.take_text() == "Which colour do you want?"


# --- the runner's inbox ---


@pytest.fixture
async def inbox():
    pushed, stop = [], asyncio.Event()

    async def push(content):
        pushed.append(content)

    client = TestClient(TestServer(runner.create_inbox(push, _TOKEN, stop)))
    await client.start_server()
    yield client, pushed, stop
    await client.close()


async def test_the_inbox_needs_the_token(inbox):
    client, pushed, stop = inbox
    for headers in ({}, {"Authorization": "Bearer guess"}):
        assert (await client.post("/messages", json={"content": [_text("x")]}, headers=headers)).status == 401
        assert (await client.get("/health", headers=headers)).status == 401
        assert (await client.post("/shutdown", headers=headers)).status == 401
    assert pushed == [] and not stop.is_set()


async def test_the_inbox_pushes_and_stops(inbox):
    client, pushed, stop = inbox
    auth = {"Authorization": f"Bearer {_TOKEN}"}
    assert (await client.get("/health", headers=auth)).status == 200
    assert (await client.post("/messages", json={"content": [_text("hi")]}, headers=auth)).status == 202
    assert (await client.post("/shutdown", headers=auth)).status == 202
    assert pushed == [[_text("hi")]] and stop.is_set()


def test_no_token_means_nothing_gets_in():
    assert not runner.is_authorized("Bearer ", None) and not runner.is_authorized("Bearer x", "")


# --- the runner's socket and options ---


def test_only_one_runner_gets_a_conversations_socket(tmp_path):
    path = str(tmp_path / "1.agent.sock")
    open(path, "w").close()  # a dead runner's leftover socket file
    lock_fd = runner.claim_socket(path)
    assert lock_fd is not None and not os.path.exists(path)  # cleared for us to listen on
    assert runner.claim_socket(path) is None  # held: a second runner backs off
    os.close(lock_fd)  # what dying does too
    assert runner.claim_socket(path) is not None


def test_options_load_the_workspace_and_only_continue_what_exists(tmp_path):
    async def can_use_tool(*_):
        pass

    options = runner.to_options(str(tmp_path), object(), can_use_tool)
    assert options.cwd == str(tmp_path) and options.setting_sources == ["project"]
    assert options.permission_mode == "auto" and not options.continue_conversation  # nothing to continue yet
    assert options.system_prompt["append"] == channel.INSTRUCTIONS
    assert options.can_use_tool is can_use_tool and options.disallowed_tools == ["ExitPlanMode"]
    assert "mcp__communication__request_approval" in options.allowed_tools


# --- the backend's side, talking to a real runner over its socket ---


_PROJECT = {"id": 3, "hash": "p3", "user_hash": "u7"}


@pytest.fixture
def runners(tmp_path, monkeypatch):
    """services/agent2 against runners served right here (runner.serve as a task) instead of spawned
    processes, on a socket in tmp_path. Yields the runner tasks started."""
    started = []
    socket_path = str(tmp_path / "5.agent.sock")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    async def start_in_process(project, conversation_id):
        cwd = os.getcwd()
        os.chdir(workspace)  # serve() takes its workspace from its cwd, like the real process
        try:
            started.append(
                asyncio.create_task(
                    runner.serve(socket_path, "http://127.0.0.1:9", agent2_sessions.tokens.to_session_token(5))
                )
            )
            await asyncio.sleep(0)
        finally:
            os.chdir(cwd)

    async def no_workspace(project, conversation_id):
        pass

    monkeypatch.setattr(agent2_sessions, "get_socket_path", lambda project, conversation_id: socket_path)
    monkeypatch.setattr(agent2_sessions, "_start_runner", start_in_process)
    monkeypatch.setattr(agent2_sessions.devenv, "ensure_workspace", no_workspace)
    return started


async def test_a_runner_is_started_once_then_reused(runners):
    await agent2.ensure_session(_PROJECT, 5)
    await agent2.ensure_session(_PROJECT, 5)
    assert len(runners) == 1 and len(FakeClaudeClient.instances) == 1
    await agent2.stop_session(_PROJECT, 5)
    await asyncio.wait_for(runners[0], 5)


async def test_messages_reach_the_session_as_user_turns(runners):
    await agent2.ensure_session(_PROJECT, 5)
    await agent2.send(_PROJECT, 5, [_text("make it blue")])
    assert FakeClaudeClient.instances[0].turns == ["make it blue"]
    await agent2.stop_session(_PROJECT, 5)
    await asyncio.wait_for(runners[0], 5)


async def test_a_stopped_or_dead_runner_is_replaced_on_the_next_ensure(runners):
    await agent2.ensure_session(_PROJECT, 5)
    assert await agent2.stop_session(_PROJECT, 5)
    await asyncio.wait_for(runners[0], 5)
    assert not await agent2.stop_session(_PROJECT, 5)  # nothing left to stop

    await agent2.ensure_session(_PROJECT, 5)  # a fresh one
    FakeClaudeClient.instances[1].end()  # Claude Code goes away: the runner ends with it
    await asyncio.wait_for(runners[1], 5)
    await agent2.ensure_session(_PROJECT, 5)
    assert len(runners) == 3
    await agent2.stop_session(_PROJECT, 5)
    await asyncio.wait_for(runners[2], 5)


async def test_sending_to_a_runner_that_isnt_there_is_unavailable(runners, monkeypatch):
    monkeypatch.setattr(agent2_sessions, "SESSION_SEND_ATTEMPTS", 2)
    monkeypatch.setattr(agent2_sessions, "exponential_backoff", lambda *a, **k: lambda attempt: 0)
    with pytest.raises(agent2.RunnerUnavailable):
        await agent2.send(_PROJECT, 5, [_text("hello?")])


def test_billing_env_brings_an_endpoints_models_only_with_the_endpoint():
    assert agent2_sessions.to_billing_env("", "", "") == {}  # locally: Claude Code's own login and defaults
    assert agent2_sessions.to_billing_env("sk-ant", "", "") == {"ANTHROPIC_API_KEY": "sk-ant"}
    deepseek = agent2_sessions.to_billing_env("", "https://api.deepseek.com/anthropic", "sk-ds")
    assert deepseek["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert deepseek["ANTHROPIC_AUTH_TOKEN"] == "sk-ds" and deepseek["ANTHROPIC_MODEL"] == "deepseek-flash[1m]"


async def test_outside_local_a_runner_needs_an_api_key(monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "anthropic_auth_token", "")
    with pytest.raises(agent2.RunnerUnavailable, match="ANTHROPIC_API_KEY"):
        await agent2_sessions._start_runner(_PROJECT, 5)


async def test_the_runner_never_loads_the_backends_settings():
    # It's handed what it needs through its env; our settings (and the .env secrets they load) stay out.
    probe = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys, src.runners.agent2.main; print('src.config' in sys.modules)",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await probe.communicate()
    assert stdout.decode().strip() == "False"


def test_a_runner_units_env_stays_off_its_command_line():
    command = systemd.to_run_command(
        "agent2-local-conversation-5", ["python", "-m", "runner"], "/ws", "/s/5.sock.env", "/ws/5.agent.log"
    )
    assert command[:2] == ["systemd-run", "--user"] and "--property=EnvironmentFile=/s/5.sock.env" in command
    assert command[-3:] == ["python", "-m", "runner"] and "--property=StandardOutput=append:/ws/5.agent.log" in command
    assert systemd.to_env_file({"TOKEN": "a$b c"}) == "TOKEN='a$b c'\n"  # taken literally by systemd
    with pytest.raises(ValueError):
        systemd.to_env_file({"TOKEN": "it's"})


async def test_a_runner_that_stopped_answering_gets_its_unit_stopped(tmp_path, monkeypatch):
    stopped = []

    async def unit_active(unit):
        return True

    async def stop_unit(unit):
        stopped.append(unit)

    monkeypatch.setattr(agent2_sessions, "get_socket_path", lambda project, conversation_id: str(tmp_path / "x.sock"))
    monkeypatch.setattr(agent2_sessions.systemd, "is_available", lambda: True)
    monkeypatch.setattr(agent2_sessions.systemd, "is_active", unit_active)
    monkeypatch.setattr(agent2_sessions.systemd, "stop", stop_unit)
    assert await agent2.stop_session(_PROJECT, 5)
    assert stopped == [agent2.get_unit_name(5)]


async def test_losing_the_start_race_to_another_runner_is_fine(tmp_path, monkeypatch):
    async def unit_taken(*args):
        raise CommandFailed("Unit agent2-local-conversation-5.service already exists.")

    async def unit_active(unit):
        return True

    monkeypatch.setattr(agent2_sessions.systemd, "is_available", lambda: True)
    monkeypatch.setattr(agent2_sessions.systemd, "start_service", unit_taken)
    monkeypatch.setattr(agent2_sessions.systemd, "is_active", unit_active)
    monkeypatch.setattr(agent2_sessions, "get_socket_dir", lambda: str(tmp_path))
    await agent2_sessions._start_runner(_PROJECT, 5)  # no RunnerUnavailable: ensure_session waits for the winner
