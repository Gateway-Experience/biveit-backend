# Intentions: managing nginx site files from the backend -- one file per site in a sites folder nginx
# includes, written with sudo, checked with `nginx -t`, then a reload. Generic: what a site serves is the
# caller's business.
#
# Needs passwordless sudo for exactly: tee into the sites folder, rm there, nginx -t, and reloading nginx.

import asyncio
import os

from src.utils.processes import CommandFailed, run_command


async def ensure_site(site_path: str, site_text: str) -> None:
    """Write the site file and reload nginx -- unless it's already there with the same content. A site nginx
    rejects is removed again before the error goes up: left in place, it would make every later reload
    fail, for every site on the box."""
    if await asyncio.to_thread(_read_text_or_none, site_path) == site_text:
        return
    await run_command(["sudo", "-n", "tee", site_path], input_text=site_text)
    try:
        await run_command(["sudo", "-n", "nginx", "-t"])
    except CommandFailed:
        await run_command(["sudo", "-n", "rm", "-f", site_path])
        raise
    await run_command(["sudo", "-n", "systemctl", "reload", "nginx"])


async def remove_site(site_path: str) -> None:
    """Remove the site file and reload nginx. Idempotent: no file, nothing to do (no sudo either)."""
    if not await asyncio.to_thread(os.path.exists, site_path):
        return
    await run_command(["sudo", "-n", "rm", "-f", site_path])
    await run_command(["sudo", "-n", "systemctl", "reload", "nginx"])


def _read_text_or_none(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as site_file:
            return site_file.read()
    except FileNotFoundError:
        return None
