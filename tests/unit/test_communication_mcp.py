# The communication MCP (src/mcps/communication), run for real the way Claude runs it: as a subprocess
# speaking MCP over stdio, against a fake backend. Checks the handshake (it's a channel), the tools reach
# the right endpoints with the session's token, a push into the inbox comes out as a channel notification,
# and it exits when stdio closes.

import asyncio
import json
import os
import socket
import sys

import mcp.types as mcp_types
import pytest
from aiohttp import web

from src.utils import http


def _get_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
async def fake_backend():
    """A backend that records every call as (path, Authorization header, JSON body) and answers like ours."""
    calls = []

    async def _answer(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        calls.append((request.path, request.headers.get("Authorization"), body))
        if request.path.endswith("/dev-servers"):
            return web.json_response({"servers": [{"app": "backend", "status": "online"}]})
        if request.path.endswith("/devenv"):
            return web.json_response({"detail": "CONVERSATION_NOT_FOUND"}, status=404)
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/{tail:.*}", _answer)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = _get_free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    yield {"url": f"http://127.0.0.1:{port}", "calls": calls}
    await runner.cleanup()


@pytest.fixture
async def session_mcp(fake_backend):
    """The MCP as a subprocess; yields {call(method, params) -> result, notify, read, inbox_port, process}."""
    inbox_port = _get_free_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.mcps.communication.main",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={
            **os.environ,
            "COMMUNICATION_PORT": str(inbox_port),
            "COMMUNICATION_OUTBOX_URL": fake_backend["url"],
            "COMMUNICATION_OUTBOX_TOKEN": "session-token",
        },
    )
    ids = iter(range(1, 1000))

    async def _write(message: dict) -> None:
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    async def read() -> dict:
        return json.loads(await asyncio.wait_for(process.stdout.readline(), 10))

    async def call(method: str, params: dict | None = None) -> dict:
        await _write({"jsonrpc": "2.0", "id": next(ids), "method": method, "params": params or {}})
        return (await read())["result"]

    async def notify(method: str) -> None:
        await _write({"jsonrpc": "2.0", "method": method})

    yield {"call": call, "notify": notify, "read": read, "inbox_port": inbox_port, "process": process}
    if process.returncode is None:
        process.kill()
        await process.wait()


async def _initialize(session_mcp) -> dict:
    result = await session_mcp["call"](
        "initialize",
        {
            "protocolVersion": mcp_types.LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    await session_mcp["notify"]("notifications/initialized")
    return result


async def _call_tool(session_mcp, name: str, arguments: dict) -> dict:
    return await session_mcp["call"]("tools/call", {"name": name, "arguments": arguments})


async def test_it_is_a_channel_with_the_platforms_tools(session_mcp):
    initialized = await _initialize(session_mcp)
    assert initialized["capabilities"]["experimental"] == {"claude/channel": {}}
    assert initialized["serverInfo"]["name"] == "communication"
    tools = await session_mcp["call"]("tools/list")
    assert [tool["name"] for tool in tools["tools"]] == [
        "send_message",
        "start_implementation",
        "dev_servers",
        "building",
        "implementing_done",
    ]


async def test_tools_post_to_the_backend_with_the_session_token(session_mcp, fake_backend):
    await _initialize(session_mcp)
    question = {"options": ["A", "B"], "multi_select": False, "allow_custom": True}
    sent = await _call_tool(
        session_mcp,
        "send_message",
        {"content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "input": {"questions": [question]}}]},
    )
    servers = await _call_tool(session_mcp, "dev_servers", {"action": "status"})
    await _call_tool(session_mcp, "building", {"active": True, "summary": "First version"})

    assert sent["content"][0]["text"] == "sent"
    assert json.loads(servers["content"][0]["text"]) == [{"app": "backend", "status": "online"}]
    assert fake_backend["calls"] == [
        (
            "/api/agent/messages",
            "Bearer session-token",
            {"content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "input": {"questions": [question]}}]},
        ),
        ("/api/agent/dev-servers", "Bearer session-token", {"action": "status"}),
        ("/api/agent/building", "Bearer session-token", {"active": True, "summary": "First version"}),
    ]


async def test_the_agent_sees_why_a_tool_failed(session_mcp, fake_backend):
    await _initialize(session_mcp)
    one_question = {"questions": [{"options": ["A"], "multi_select": False, "allow_custom": False}]}
    mixed = await _call_tool(
        session_mcp,
        "send_message",
        {
            "content": [
                {"type": "tool_use", "input": one_question},
                {"type": "tool_use", "input": {"actions": [{"name": "pay", "params": {}}]}},
            ]
        },
    )
    refused = await _call_tool(session_mcp, "start_implementation", {})

    assert mixed["isError"] and "Cannot mix questions and actions" in mixed["content"][0]["text"]
    assert refused["isError"] and "404" in refused["content"][0]["text"]
    assert [path for path, _, _ in fake_backend["calls"]] == ["/api/agent/devenv"]  # the mixed one never left


async def test_a_push_into_the_inbox_comes_out_as_a_channel_notification(session_mcp):
    await _initialize(session_mcp)
    await http.connect()
    try:
        status = await _post_when_listening(
            session_mcp["inbox_port"], {"content": [{"type": "text", "text": "Xin chào, Tiếng Việt"}]}
        )
    finally:
        await http.close()
    notification = await session_mcp["read"]()
    assert status == 202
    assert notification["method"] == "notifications/claude/channel"
    # Compact JSON with the text left readable -- not \\u-escaped, which the agent reads worse.
    assert notification["params"]["content"] == '{"content":[{"type":"text","text":"Xin chào, Tiếng Việt"}]}'


async def _post_when_listening(port: int, body: dict, attempts: int = 50) -> int:
    """POST to the inbox, retrying while it isn't listening yet (it starts right after the handshake)."""
    try:
        status, _, _ = await http.request("POST", f"http://127.0.0.1:{port}/", json=body)
        return status
    except OSError:
        if attempts <= 1:
            raise
        await asyncio.sleep(0.1)
        return await _post_when_listening(port, body, attempts - 1)


async def test_it_exits_when_claude_closes_stdio(session_mcp):
    await _initialize(session_mcp)
    session_mcp["process"].stdin.close()
    assert await asyncio.wait_for(session_mcp["process"].wait(), 10) == 0


async def test_it_never_loads_the_backends_settings():
    # It runs inside the agent's session: our settings (and the .env secrets they load) must stay out. A
    # fresh interpreter, since this test process has long imported them.
    probe = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys, src.mcps.communication.main; print('src.config' in sys.modules)",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await probe.communicate()
    assert stdout.decode().strip() == "False"
