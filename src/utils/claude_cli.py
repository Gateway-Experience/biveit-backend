# Intentions: asking Claude Code (the `claude` CLI on this machine) a one-off question and getting its answer as
# text -- for small generation jobs the backend does itself, outside any agent session. Generic: what to ask, and
# what the answer means, is the caller's business.
#
# It bills to the same model key the agents use (settings.anthropic_*), and talks to the same endpoint: with
# anthropic_base_url set (another provider speaking Anthropic's API), AGENT_ENDPOINT_ENV maps Claude's model names
# onto that provider's. Locally with no key at all, it's whatever login the machine's Claude Code has.
#
# SAFE FOR UNTRUSTED TEXT IN THE PROMPT: it runs with every tool disabled, no settings / MCP servers / hooks loaded,
# in an empty folder -- so a prompt that talks the model into "running something" has nothing to run it with. Keep
# it that way: this runs on the host, as the backend's user.

import tempfile

import pydash

from src.config import settings
from src.constants import AGENT_ENDPOINT_ENV, CLAUDE_CLI_TIMEOUT_SECONDS
from src.utils.processes import get_base_env, run_command_binary


async def ask_claude(prompt: str, model: str = "haiku") -> str:
    """Claude Code's answer to `prompt`, stripped. `model` is one of Claude Code's names (haiku, sonnet, opus).
    Raises processes.CommandFailed when it can't answer (no key, endpoint down, timeout). [I/O]"""
    argv = [
        "claude", "-p",
        "--model", model,
        "--tools", "",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--output-format", "text",
    ]  # fmt: skip
    with tempfile.TemporaryDirectory() as empty_dir:
        # stdout only: Claude Code prints its notices (an unknown model name, connectors off...) on stderr.
        output = await run_command_binary(
            argv,
            cwd=empty_dir,
            input_text=prompt,
            timeout_seconds=CLAUDE_CLI_TIMEOUT_SECONDS,
            env=_to_claude_env(),
        )
    return output.decode(errors="replace").strip()


def _to_claude_env() -> dict[str, str]:
    """[Pure-ish] The command's whole environment: the safe base, plus the model key and endpoint."""
    key_env = pydash.omit_by(
        {"ANTHROPIC_API_KEY": settings.anthropic_api_key, "ANTHROPIC_AUTH_TOKEN": settings.anthropic_auth_token},
        lambda value: not value,
    )
    # A one-off question needs no deep thinking, whatever the agents' endpoint settings say.
    endpoint_env = (
        {"ANTHROPIC_BASE_URL": settings.anthropic_base_url, **AGENT_ENDPOINT_ENV, "CLAUDE_CODE_EFFORT_LEVEL": "low"}
        if settings.anthropic_base_url
        else {}
    )
    return {**get_base_env(), **key_env, **endpoint_env, "DISABLE_AUTOUPDATER": "1"}
