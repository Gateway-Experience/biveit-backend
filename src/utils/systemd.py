# Intentions: long-lived processes as transient units of the user's systemd manager (`systemd-run --user`),
# so they belong to systemd rather than to whoever started them. Generic -- the caller decides what runs.
#
# Why bother: a child of a systemd service stays in the service's cgroup however detached it is, and the
# service's restart (default KillMode=control-group) kills it. A transient unit has its own cgroup.
# Needs the user's manager to be running: on a server, `loginctl enable-linger <user>` keeps it up without
# a login. A service running as that user usually has no XDG_RUNTIME_DIR, so it's filled in here.

import asyncio
import os
import shutil

from src.utils.processes import CommandFailed, get_base_env, run_command


def is_available() -> bool:
    """[Pure-ish] Whether systemd-run is on this machine at all (not on macOS, say)."""
    return shutil.which("systemd-run") is not None


def to_run_command(unit: str, argv: list[str], cwd: str, env_file: str, log_path: str) -> list[str]:
    """[Pure] The systemd-run command starting `argv` as the transient service `unit`, its env read from
    `env_file` (to_env_file). A file and not --setenv=NAME=VALUE, so secrets never show up on a command
    line (`ps` shows those to every user); name-only --setenv would do too, but older systemds (249, Ubuntu
    22.04) don't have it. --collect: the unit is cleaned up even when it fails, so its name is free for the
    next start."""
    return [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        f"--working-directory={cwd}",
        f"--property=EnvironmentFile={env_file}",
        f"--property=StandardOutput=append:{log_path}",
        "--property=StandardError=inherit",
        "--",
        *argv,
    ]


def to_env_file(env: dict[str, str]) -> str:
    """[Pure] `env` as an EnvironmentFile: single-quoted values, which systemd takes literally (no escapes,
    no $ expansion). A value with a quote or a newline can't be written that way, so it's refused."""
    unwritable = [name for name, value in env.items() if "'" in value or "\n" in value]
    if unwritable:
        raise ValueError(f"env values with quotes or newlines can't go in an EnvironmentFile: {unwritable}")
    return "".join(f"{name}='{value}'\n" for name, value in env.items())


async def start_service(
    unit: str, argv: list[str], cwd: str, env: dict[str, str], env_file: str, log_path: str
) -> None:
    """Start `argv` as the transient service `unit` with exactly `env` (plus whatever the user's manager sets,
    like its own PATH -- so pass PATH), output appended to `log_path`. `env` is written to `env_file` first,
    owner-only, and left there: systemd reads it when the process starts, which may be after we return.
    Returns once systemd has it, not once it's ready. Raises CommandFailed, e.g. when a unit by that name is
    already running."""
    await asyncio.to_thread(_write_private_file, env_file, to_env_file(env))
    await run_command(to_run_command(unit, argv, cwd, env_file, log_path), env=_to_manager_env(), timeout_seconds=30)


async def is_active(unit: str) -> bool:
    """Whether `unit` is running (or starting up). An unknown unit is just inactive."""
    state = await run_command(
        ["systemctl", "--user", "show", "--property=ActiveState", "--value", unit], env=_to_manager_env()
    )
    return state.strip() in ("active", "activating", "reloading")


async def stop(unit: str) -> None:
    """Stop `unit` (SIGTERM to all its processes, SIGKILL after systemd's timeout). Stopping a unit that
    isn't there is fine."""
    try:
        await run_command(["systemctl", "--user", "stop", unit], env=_to_manager_env())
    except CommandFailed as error:
        if "not loaded" not in str(error):
            raise


def _to_manager_env() -> dict[str, str]:
    """[Pure-ish] The env systemctl/systemd-run need to find the user's manager: XDG_RUNTIME_DIR (its bus
    lives there), filled in when we were started without one."""
    return {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}", **get_base_env()}


def _write_private_file(path: str, content: str) -> None:
    """Write `content` to `path`, readable by us only -- also when it already existed with looser rights."""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(content)
