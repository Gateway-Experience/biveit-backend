# Intentions: a site's docker images -- one per deployed commit, tagged biveit-<env>-site-<project id>:<commit>,
# so an image is a function of its commit (plus our build files): the same commit is never built twice, and
# a rollback just runs an older tag. Built from SITE_IMAGE_DIR plus the commit's archive (see its Dockerfile
# for the layer order that keeps rebuilds quick).
#
# TODO: the build's RUN steps (npm ci, the frontend build, uv sync) run the project's code -- postinstall
# scripts, Vite plugins -- on docker's default build network, which the firewall rules for the projects'
# network don't cover. Move them to a buildx builder on that network.

import asyncio
import io
import os
import shutil
import tarfile
import tempfile

from src.config import settings
from src.modules.deployments.constants import DEPLOYMENT_KEPT_IMAGES, SITE_BUILD_TIMEOUT_SECONDS, SITE_IMAGE_DIR
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
    """Build `tag` from our build files plus `app_archive` (the apps, as `git archive` makes them), in a
    temporary build folder of ours. Labelled with the project, which is how prune_images finds its images."""
    with tempfile.TemporaryDirectory(prefix="biveit-site-build-") as context_dir:
        await asyncio.to_thread(_prepare_build_context, context_dir, app_archive)
        await run_command(
            [
                "docker",
                "build",
                f"--tag={tag}",
                f"--label=biveit.env={settings.env}",
                f"--label=biveit.site={project_id}",
                context_dir,
            ],
            timeout_seconds=SITE_BUILD_TIMEOUT_SECONDS,
        )


def _prepare_build_context(context_dir: str, app_archive: bytes) -> None:
    """Our build files at the top, the apps under app/. The archive comes from the agent's repo, so it's
    unpacked with tarfile's "data" filter: no absolute paths, nothing outside app/, no links pointing out."""
    shutil.copytree(SITE_IMAGE_DIR, context_dir, dirs_exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(app_archive)) as archive:
        archive.extractall(os.path.join(context_dir, "app"), filter="data")


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
