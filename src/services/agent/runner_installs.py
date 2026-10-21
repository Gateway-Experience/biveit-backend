# Intentions: the session runner INSTALLED in a project's container -- put in from the host's checkout
# (settings.session_runner_dir) to RUNNER_INSTALL_PATH, root-owned (the agent can read its runner, not
# change it), with its own venv built there by the container's own uv and Python.
#
# Which version a container has is the content of its VERSION file: a hash of the checkout's source (not a
# git commit -- uncommitted edits count too). ensure_runner_installed compares it with the checkout's
# hash every time a runner is about to start, and reinstalls when they differ, so a container always runs
# the current runner without anything being recorded. A runner already running keeps the code it started
# with; the new one is picked up by the next start.

import asyncio
import hashlib
import io
import os
import tarfile

from src.config import settings
from src.constants import RUNNER_INSTALL_PATH
from src.dto.projects import Project
from src.services import devenv
from src.utils.processes import CommandFailed

# Never copied in: the host's own venv (built for the host's Python, useless in the container) and caches.
_NOT_COPIED = (".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache")


class RunnerInstallFailed(Exception):
    """The runner couldn't be installed into the project's container. The message has why."""


def get_source_version(source_dir: str) -> str:
    """[Pure-ish] A hash of the runner checkout's files (paths and contents), skipping _NOT_COPIED."""
    digest = hashlib.sha256()
    for path in _list_source_files(source_dir):  # one update per file: that's the hashing
        digest.update(path.encode() + b"\0")
        with open(os.path.join(source_dir, path), "rb") as source_file:
            digest.update(source_file.read() + b"\0")
    return digest.hexdigest()[:16]


async def ensure_runner_installed(project: Project) -> None:
    """Make sure the project's (running) container has the current runner. Installed already -> nothing;
    missing or another version -> (re)installed. Raises RunnerInstallFailed."""
    source_dir = os.path.realpath(os.path.expanduser(settings.session_runner_dir))
    version = await asyncio.to_thread(get_source_version, source_dir)
    if await _get_installed_version(project["id"]) == version:
        return
    try:
        await _install(project["id"], source_dir, version)
    except CommandFailed as error:
        raise RunnerInstallFailed(f"project {project['id']}'s runner couldn't be installed: {error}") from error


async def _get_installed_version(project_id: int) -> str | None:
    try:
        return (await devenv.run_in_container(project_id, ["cat", f"{RUNNER_INSTALL_PATH}/VERSION"])).strip()
    except CommandFailed:
        return None


async def _install(project_id: int, source_dir: str, version: str) -> None:
    """Put the checkout in (minus _NOT_COPIED) as root's files, and install it there as root, exactly the
    locked versions: its venv (the container's own Python; compiled up front, since the agent's user can't
    write __pycache__ in it) and its bundle's MCP servers (extensions/, npm ci; no install scripts, and no
    browser download -- the image has the browser). Only then VERSION, so an install cut off halfway never
    looks finished."""
    archive = await asyncio.to_thread(_to_root_owned_archive, source_dir, os.path.basename(RUNNER_INSTALL_PATH))
    await devenv.run_in_container(project_id, ["rm", "-rf", RUNNER_INSTALL_PATH], user="root")
    await devenv.extract_into_container(project_id, archive, os.path.dirname(RUNNER_INSTALL_PATH))
    # HOME=/root: the container's own HOME is the agent's (in the project folder), and a root install must
    # read nothing the agent wrote -- an ~/.npmrc or uv config there could point it at another package
    # registry, and what it installs is root-owned and runs for every session.
    # TMPDIR on the container's disk: uv unpacks the SDK there first, and its bundled Claude Code alone is
    # bigger than the container's /tmp (a small tmpfs, see DEVENV_CONTAINER_LIMITS).
    unpack_dir = f"{RUNNER_INSTALL_PATH}.unpack"
    build = (
        f"mkdir -p {unpack_dir} && cd {RUNNER_INSTALL_PATH} && export HOME=/root TMPDIR={unpack_dir} && "
        "UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --compile-bytecode && "
        f"(cd extensions && npm_config_cache={unpack_dir}/npm PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 "
        "npm ci --omit=dev --ignore-scripts --no-audit --no-fund) && "
        f"echo {version} > VERSION && chmod -R a+rX,go-w . ; status=$?; rm -rf {unpack_dir}; exit $status"
    )
    await devenv.run_in_container(project_id, ["sh", "-c", build], user="root", timeout_seconds=600)


def _to_root_owned_archive(source_dir: str, name: str) -> bytes:
    """[Pure-ish] The checkout (minus _NOT_COPIED) as a tar archive of a folder `name`, every entry owned by
    root -- what extract_into_container keeps."""

    def _as_root(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if set(member.name.split("/")) & set(_NOT_COPIED):
            return None
        member.uid, member.gid, member.uname, member.gname = 0, 0, "root", "root"
        return member

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(source_dir, arcname=name, filter=_as_root)
    return buffer.getvalue()


def _list_source_files(source_dir: str) -> list[str]:
    """[Pure-ish] Every file under source_dir, relative and sorted, skipping _NOT_COPIED folders."""
    return sorted(
        os.path.relpath(os.path.join(folder, name), source_dir)
        for folder, subfolders, names in os.walk(source_dir)
        if not set(os.path.relpath(folder, source_dir).split(os.sep)) & set(_NOT_COPIED)
        for name in names
    )
