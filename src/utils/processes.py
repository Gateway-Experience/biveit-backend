# Intentions: running external commands (uv, npm, pm2, nginx...) from async code. Generic -- the caller
# decides what a command means and what its output says. Commands run with get_base_env(), never our
# full environment (see SUBPROCESS_ENV_PASSTHROUGH).

import asyncio
import os
import shlex

import pydash

from src.constants import SUBPROCESS_ENV_PASSTHROUGH

# How much of a failed command's output goes into the error -- the end, where the actual error usually is.
_OUTPUT_TAIL_CHARS = 2000


class CommandFailed(Exception):
    """A command exited non-zero, or ran past its timeout. The message has the command and the tail of
    its output, so it reads on its own in a log or in front of a human."""


async def run_command(
    argv: list[str],
    cwd: str | None = None,
    input_text: str | bytes | None = None,
    timeout_seconds: float = 600,
    env: dict[str, str] | None = None,
) -> str:
    """Run argv (no shell) and return its output, stdout and stderr together. Raises CommandFailed on a
    non-zero exit or a timeout (the process is killed then). `input_text` (text, or raw bytes) is fed to its
    stdin; `env` replaces get_base_env() as the command's whole environment."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env if env is not None else get_base_env(),
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(input_text.encode() if isinstance(input_text, str) else input_text), timeout_seconds
        )
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise CommandFailed(f"`{shlex.join(argv)}` timed out after {timeout_seconds}s", {"cwd": cwd}) from error
    output = stdout.decode(errors="replace")
    if process.returncode != 0:
        raise CommandFailed(
            f"`{shlex.join(argv)}` exited {process.returncode}:\n{output[-_OUTPUT_TAIL_CHARS:]}", {"cwd": cwd}
        )
    return output


def get_base_env() -> dict[str, str]:
    """[Pure-ish] The part of our environment that's safe and useful to hand to a command we launch:
    SUBPROCESS_ENV_PASSTHROUGH, with our own virtualenv taken off PATH (`uv run` puts it first, and a
    project's `python` must not resolve to ours)."""
    own_venv_bin = os.path.join(os.environ["VIRTUAL_ENV"], "bin") if os.environ.get("VIRTUAL_ENV") else None
    base_env = pydash.pick(dict(os.environ), *SUBPROCESS_ENV_PASSTHROUGH)
    path_entries = [entry for entry in base_env.get("PATH", "").split(os.pathsep) if entry != own_venv_bin]
    return {**base_env, "PATH": os.pathsep.join(path_entries)}
