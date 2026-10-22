# Intentions: where a project's dev environment can be reached -- derived from the project (its id for
# the ports, its hash for the dev site) and DEV_DOMAIN, nothing stored. A local platform (ENV=local) is
# the exception: fixed ports for every project, and no dev site (see LOCAL_DEV_BACKEND_PORT).

from src.config import settings
from src.constants import (
    LOCAL_DEV_BACKEND_PORT,
    LOCAL_DEV_FRONTEND_PORT,
    PROJECT_DEV_BACKEND_PORT_BASE,
    PROJECT_DEV_FRONTEND_PORT_BASE,
)
from src.dto.projects import Project


def get_dev_backend_port(project_id: int) -> int:
    return LOCAL_DEV_BACKEND_PORT if _is_local_platform() else PROJECT_DEV_BACKEND_PORT_BASE + project_id


def get_dev_frontend_port(project_id: int) -> int:
    return LOCAL_DEV_FRONTEND_PORT if _is_local_platform() else PROJECT_DEV_FRONTEND_PORT_BASE + project_id


def get_dev_host(project: Project) -> str | None:
    """[Pure-ish] The dev site's host name, <project hash>.<DEV_DOMAIN> (each project its own origin, so
    their cookies and storage stay apart), or None when there's no dev site: on a local platform, or
    without a DEV_DOMAIN. No host means no nginx site either (services/devenv/sites)."""
    return f"{project['hash']}.{settings.dev_domain}" if settings.dev_domain and not _is_local_platform() else None


def get_dev_url(project: Project) -> str:
    """[Pure-ish] Where the user (and the agent) open the dev site: through nginx if there is one, else
    straight at the Vite port."""
    dev_host = get_dev_host(project)
    return f"https://{dev_host}" if dev_host else f"http://localhost:{get_dev_frontend_port(project['id'])}"


def _is_local_platform() -> bool:
    return settings.env == "local"
