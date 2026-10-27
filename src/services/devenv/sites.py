# Intentions: a project's dev site in nginx -- https://<project hash>.<DEV_DOMAIN>, /api/ to its dev
# backend and everything else to Vite. One file per project in NGINX_SITES_DIR (utils/nginx), served with
# the wildcard certificate set up once on the VPS (so no certbot run per project). Without a dev host (a
# local platform, or no DEV_DOMAIN -- see addresses.get_dev_host) there's no site at all.

import os

from src.config import settings
from src.constants import DEVENV_NGINX_TEMPLATE
from src.dto.projects import Project
from src.services.devenv import addresses, templates
from src.utils import nginx


async def ensure_dev_site(project: Project, conversation_id: int) -> None:
    """Write the project's site file and reload nginx (nginx.ensure_site) -- unless there's no dev host."""
    if addresses.get_dev_host(project) is None:
        return
    site_path = os.path.join(settings.nginx_sites_dir, f"biveit-project-{project['id']}.conf")
    await nginx.ensure_site(site_path, templates.render_template(DEVENV_NGINX_TEMPLATE, project, conversation_id))
