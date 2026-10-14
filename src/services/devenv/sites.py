# Intentions: a project's dev site in nginx -- https://<project hash>.<DEV_DOMAIN>, /api/ to its dev
# backend and everything else to Vite. One file per project in NGINX_SITES_DIR, served with the wildcard
# certificate set up once on the VPS (so no certbot run per project). Without a dev host (a local
# platform, or no DEV_DOMAIN -- see addresses.get_dev_host) there's no site at all.
#
# Needs passwordless sudo for exactly: tee into NGINX_SITES_DIR, rm there, nginx -t, and reloading nginx.

import asyncio
import os

from src.config import settings
from src.constants import DEVENV_NGINX_TEMPLATE
from src.dto.projects import Project
from src.services.devenv import addresses, templates
from src.utils.processes import CommandFailed, run_command


async def ensure_dev_site(project: Project, conversation_id: int) -> None:
    """Write the project's site file and reload nginx -- unless there's no dev host, or the file is
    already there with the same content. A site nginx rejects is removed again before the error goes up:
    left in place, it would make every later reload fail, for every project on the box."""
    if addresses.get_dev_host(project) is None:
        return
    site_path = os.path.join(settings.nginx_sites_dir, f"vib3-project-{project['id']}.conf")
    site_text = templates.render_template(DEVENV_NGINX_TEMPLATE, project, conversation_id)
    if await asyncio.to_thread(_read_text_or_none, site_path) == site_text:
        return
    await run_command(["sudo", "-n", "tee", site_path], input_text=site_text)
    try:
        await run_command(["sudo", "-n", "nginx", "-t"])
    except CommandFailed:
        await run_command(["sudo", "-n", "rm", "-f", site_path])
        raise
    await run_command(["sudo", "-n", "systemctl", "reload", "nginx"])


def _read_text_or_none(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as site_file:
            return site_file.read()
    except FileNotFoundError:
        return None
