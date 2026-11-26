# Intentions: where a project's dev environment can be reached -- derived from the project (its id for
# the ports, its hash for the dev site) and DEV_DOMAIN, nothing stored. Ports come from the port plan
# (settings.project_port_slots: on a server every project has its own, locally a few are shared). A local platform
# (ENV=local) has no dev site either.

from src.config import settings
from src.dto.projects import Project


def get_dev_backend_port(project_id: int) -> int:
    return settings.get_project_port(settings.dev_backend_port_base, project_id)


def get_dev_frontend_port(project_id: int) -> int:
    return settings.get_project_port(settings.dev_frontend_port_base, project_id)


def shares_dev_ports(project_id: int, other_project_id: int) -> bool:
    """[Pure-ish] Whether two different projects' dev servers would take the same host ports (the same slot), so
    only one of them can run at a time. Only ever locally: on a server each project has a slot of its own."""
    return project_id != other_project_id and _get_port_slot(project_id) == _get_port_slot(other_project_id)


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


def _get_port_slot(project_id: int) -> int:
    return project_id % settings.project_port_slots


def _is_local_platform() -> bool:
    return settings.env == "local"
