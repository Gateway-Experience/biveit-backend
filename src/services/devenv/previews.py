# Intentions: a screenshot of each project's dev site -- what the project list's cards show instead of a
# made-up sketch, once there's a real site to show. Taken inside the project's own container, with the
# Chromium + Playwright its agent's browser tool already uses, so the host needs no browser of its own.
#
# The picture is a CACHE of what the site looks like: derived, never the source of anything, so losing it
# only means the card goes back to the sketch until the next one is taken. One file per project,
# <site_previews_dir>/<project hash>.jpg, replaced whole each time. When it was taken is the file's mtime --
# nothing's recorded anywhere else, so there's nothing to keep in sync.
# It's kept OUTSIDE the project's folder: that folder is the agent's (mounted into its container, see
# "PROJECT FOLDERS ARE UNTRUSTED INPUT" in CLAUDE.md), and the agent shouldn't get to pick the picture
# the user is shown -- here it only ever comes out of our own capture.
#
# When: a while after the agent last said something while building (capture_later -- its changes
# have usually settled by then), and on an open for a project that doesn't have one yet (an older project,
# or one whose capture failed).

import asyncio
import base64
import os
import re
import time
from datetime import UTC, datetime

from src.config import settings
from src.constants import DEVENV_INTERNAL_FRONTEND_PORT, RUNNER_INSTALL_PATH, SITE_PREVIEW_DELAY_SECONDS
from src.dto.projects import Project
from src.services.devenv.containers import is_container_running, run_in_container
from src.utils.logging import log_error
from src.utils.processes import CommandFailed

# The Playwright that comes with the runner's browser tool (its @playwright/mcp), the one the image's
# Chromium build matches (see data/devenv_image/Dockerfile). playwright-core: no browser download of its own.
_PLAYWRIGHT_PATH = f"{RUNNER_INSTALL_PATH}/extensions/node_modules/playwright-core"

# Opens the site at desktop size (16:10, the card's shape), waits for it to settle, and prints the
# screenshot as base64 JPEG on stdout, between markers -- our command runner reads text (stdout and stderr
# mixed, so a browser warning can land around it), and this way nothing is written in the container. A site that doesn't answer, or answers with a server error, exits non-zero: no picture
# is better than a picture of an error page. --no-sandbox: same reason as the browser tool's (the
# container drops the privileges Chromium's sandbox needs).
_CAPTURE_SCRIPT = """
const { chromium } = require(process.argv[1]);
(async () => {
  const browser = await chromium.launch({ args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const response = await page.goto(process.argv[2], { waitUntil: 'load', timeout: 20000 });
    if (!response || response.status() >= 500) {
      throw new Error(`the site answered ${response && response.status()}`);
    }
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
    await page.waitForTimeout(800);
    const image = await page.screenshot({ type: 'jpeg', quality: 75 });
    process.stdout.write(`<<<preview:${image.toString('base64')}>>>`);
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error.message); process.exit(1); });
"""

_OUTPUT_PATTERN = re.compile(r"<<<preview:([A-Za-z0-9+/=]+)>>>")

# project id -> when its latest capture was asked for (time.monotonic()). Only the latest request of a
# burst gets to capture: each one checks it's still the latest after its wait. In memory: a restart just
# drops what was pending, and the next agent message (or open) asks again.
_latest_requests: dict[int, float] = {}


def get_preview_path(project: Project) -> str:
    """[Pure-ish] Where the project's picture lives (whether or not it's been taken yet)."""
    return os.path.join(os.path.realpath(settings.site_previews_dir), f"{project['hash']}.jpg")


def get_taken_at(project: Project) -> datetime | None:
    """[Pure-ish] When the project's picture was taken, or None if it hasn't been."""
    try:
        return datetime.fromtimestamp(os.stat(get_preview_path(project)).st_mtime, UTC)
    except FileNotFoundError:
        return None


async def capture(project: Project) -> bool:
    """Take the picture of the project's dev site now and store it, replacing the old one. Returns whether
    it got one. Best effort: a container that isn't running, or a site that's down or erroring, just
    leaves the old picture in place (False), logged when it's worth a look."""
    if not await is_container_running(project["id"]):
        return False
    # The browser runs inside the container, so it goes straight to Vite on the container's own port, not
    # the one published on the host.
    site_url = f"http://127.0.0.1:{DEVENV_INTERNAL_FRONTEND_PORT}/"
    try:
        output = await run_in_container(
            project["id"], ["node", "-e", _CAPTURE_SCRIPT, _PLAYWRIGHT_PATH, site_url], timeout_seconds=90
        )
    except CommandFailed as error:
        log_error(error, meta={"project_id": project["id"], "doing": "site preview capture"})
        return False
    match = _OUTPUT_PATTERN.search(output)
    if not match:
        log_error(
            "SitePreviewMissing",
            "the capture printed no picture",
            {"project_id": project["id"], "output": output[-500:]},
        )
        return False
    await asyncio.to_thread(_write_atomically, get_preview_path(project), base64.b64decode(match.group(1)))
    return True


async def capture_later(project: Project) -> None:
    """Capture the project's site once SITE_PREVIEW_DELAY_SECONDS pass without another request for it --
    the agent tends to post several messages in a row, and its changes need a moment to show. Meant to be
    spawned (it waits)."""
    requested_at = time.monotonic()
    _latest_requests[project["id"]] = requested_at
    await asyncio.sleep(SITE_PREVIEW_DELAY_SECONDS)
    if _latest_requests.get(project["id"]) != requested_at:
        return
    _latest_requests.pop(project["id"], None)
    await capture(project)


def _write_atomically(path: str, data: bytes) -> None:
    """Write via a temp file + rename, so a reader never gets half a picture."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.tmp"
    with open(temp_path, "wb") as file:
        file.write(data)
    os.replace(temp_path, path)
