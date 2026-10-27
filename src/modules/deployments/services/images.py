# Intentions: a site's docker images -- one per deployed commit, tagged biveit-<env>-site-<project id>:<commit>,
# so an image is a function of its commit (plus our build files): the same commit is never built twice, and
# a rollback just runs an older tag.
#
# Building one runs the project's own code (npm scripts, the Vite config, Python build hooks), so it happens in
# two halves:
#   1. the apps are built in a throwaway container -- the devenv image, as sandboxed as a dev container
#      (unprivileged, no capabilities, the projects' firewalled network, limits, no host folder) -- running
#      SITE_BUILD_SCRIPT over the commit's archive, then committed as a temporary image
#   2. `docker build` of SITE_IMAGE_DIR copies the built apps out of that image; every RUN in there is ours
# Nothing of the build lands on the host's disk: the archive goes in through docker cp, the results come out
# through docker itself.

import contextlib
import io
import tarfile
from pathlib import Path

from src.config import settings
from src.constants import DEVENV_IMAGE, DEVENV_NETWORK
from src.modules.deployments.constants import (
    DEPLOYMENT_KEPT_IMAGES,
    SITE_BUILD_LIMITS,
    SITE_BUILD_SCRIPT,
    SITE_BUILD_TIMEOUT_SECONDS,
    SITE_BUILD_UID,
    SITE_IMAGE_DIR,
)
from src.services import devenv
from src.utils.processes import CommandFailed, run_command


def get_image_tag(project_id: int, commit: str) -> str:
    """[Pure-ish] The env is in it: every backend on the machine shares one docker, and project ids only mean
    something within one database."""
    return f"biveit-{settings.env}-site-{project_id}:{commit[:12]}"


async def image_exists(tag: str) -> bool:
    try:
        await run_command(["docker", "image", "inspect", tag])
        return True
    except CommandFailed:
        return False


async def build_image(project_id: int, tag: str, app_archive: bytes) -> None:
    """Build `tag` from `app_archive` (the apps, as `git archive` makes them) -- the two halves at the top of
    this file. Labelled with the project, which is how prune_images finds its images. The build container and
    its image are gone afterwards, whatever happened."""
    repository, _, version = tag.partition(":")
    build_container = f"{repository}-build"
    build_tag = f"{build_container}:{version}"
    # One left behind by a backend that died mid-build.
    await run_command(["docker", "rm", "--force", build_container])
    try:
        await devenv.ensure_network()
        await run_command(_to_build_argv(build_container))
        await run_command(["docker", "cp", "-", f"{build_container}:/"], input_text=_to_build_archive(app_archive))
        await run_command(["docker", "start", "--attach", build_container], timeout_seconds=SITE_BUILD_TIMEOUT_SECONDS)
        await run_command(["docker", "commit", build_container, build_tag])
        await run_command(
            [
                "docker",
                "build",
                f"--tag={tag}",
                f"--label=biveit.env={settings.env}",
                f"--label=biveit.site={project_id}",
                f"--build-arg=BUILD_IMAGE={build_tag}",
                SITE_IMAGE_DIR,
            ],
            timeout_seconds=SITE_BUILD_TIMEOUT_SECONDS,
        )
    finally:
        # Also stops a build that timed out: run_command only kills the docker client.
        await run_command(["docker", "rm", "--force", build_container])
        with contextlib.suppress(CommandFailed):  # not there when the build failed before the commit
            await run_command(["docker", "image", "rm", build_tag])


def _to_build_argv(build_container: str) -> list[str]:
    """[Pure-ish] The `docker create` of the build container: a dev container's sandbox (services/devenv/
    containers), minus the mounts and ports, running SITE_BUILD_SCRIPT. HOME (npm's and uv's caches) sits in
    /app, the one place the build's user can write -- the image only takes the apps from there."""
    limits = SITE_BUILD_LIMITS
    return [
        "docker",
        "create",
        f"--name={build_container}",
        "--init",
        f"--user={SITE_BUILD_UID}:{SITE_BUILD_UID}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--network={DEVENV_NETWORK}",
        f"--memory={limits['memory']}",
        f"--memory-swap={limits['memory']}",
        f"--cpus={limits['cpus']}",
        f"--pids-limit={limits['pids']}",
        f"--tmpfs=/tmp:size={limits['tmp_size']},mode=1777",
        "--env=HOME=/app/.home",
        "--env=UV_PYTHON_DOWNLOADS=never",
        "--env=UV_COMPILE_BYTECODE=1",
        "--env=UV_LINK_MODE=copy",
        DEVENV_IMAGE,
        "bash",
        "-c",
        Path(SITE_BUILD_SCRIPT).read_text(),
    ]


def _to_build_archive(app_archive: bytes) -> bytes:
    """[Pure] The apps re-packed under app/ and owned by the build's user, for docker cp to unpack at the build
    container's root -- docker cp (without --archive) keeps the owners a tar records, and the build's user has
    no other way to own its folder (no capabilities to chown with). Done in memory: the agent's files never
    touch the host's disk, and links stay links, only ever followed inside the container."""
    owner = {"uid": SITE_BUILD_UID, "gid": SITE_BUILD_UID, "uname": "", "gname": ""}
    app_dir = tarfile.TarInfo("app")
    app_dir.type = tarfile.DIRTYPE  # TarInfo takes nothing but a name, and replace() can't change the type
    app_dir = app_dir.replace(mode=0o755, **owner)
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(app_archive)) as source, tarfile.open(fileobj=output, mode="w") as target:
        target.addfile(app_dir)
        # Writing a tar is a stream of appends; there's no declarative way around tarfile's API.
        for member in source.getmembers():
            target.addfile(
                member.replace(name=f"app/{member.name}", **owner),
                source.extractfile(member) if member.isfile() else None,
            )
    return output.getvalue()


async def prune_images(project_id: int, live_tag: str) -> None:
    """Remove the project's images beyond the newest DEPLOYMENT_KEPT_IMAGES -- never the live one."""
    output = await run_command(
        [
            "docker",
            "images",
            f"--filter=label=biveit.env={settings.env}",
            f"--filter=label=biveit.site={project_id}",
            "--format={{.Repository}}:{{.Tag}}",
        ]
    )
    # `docker images` lists newest first.
    stale_tags = [tag for tag in output.split()[DEPLOYMENT_KEPT_IMAGES:] if tag != live_tag]
    if stale_tags:
        await run_command(["docker", "image", "rm", *stale_tags])
