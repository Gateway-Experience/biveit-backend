# The devenv service package -- a project's development environment: the folders its code lives in on
# disk, and once its agent starts building, the app copied in from the boilerplate, its dev servers
# (pm2) on the project's fixed ports and its dev site (nginx). See PROJECT_DEV_BACKEND_PORT_BASE in
# constants for the port plan.
#   workspaces.py    the on-disk layout; seeding a workspace from the template; copying in the boilerplate
#   environments.py  ensure_devenv: every setup step in order, each skipped when already done
#   servers.py       the dev servers in pm2, and checking they answer
#   sites.py         the dev site in nginx
#   templates.py     rendering a project's files from DEVENV_TEMPLATES_DIR
#   addresses.py     a project's dev ports and URL, derived from its id
#   containers.py    the project's container (where its dev servers, Redis and agent will run)
#   secrets.py       secrets derived from PROJECT_SECRET_SEED (nothing stored)
#
# It doesn't know about the agent or the chat -- services/agent builds on it, never the other way round.
# Callers import THIS package (`from src.services import devenv`), not the files.

from src.services.devenv.containers import (
    ContainerUnavailable,
    ensure_container,
    ensure_network,
    extract_into_container,
    get_container_name,
    get_gateway_ip,
    get_host_socket_dir,
    is_container_running,
    list_running_project_ids,
    remove_container,
    run_in_container,
    run_in_container_binary,
    stop_container,
    to_container_path,
)
from src.services.devenv.environments import (
    control_dev_servers,
    describe_devenv,
    ensure_devenv,
    get_devenv_status,
    stop_dev_servers,
)
from src.services.devenv.secrets import derive_secret
from src.services.devenv.workspaces import (
    ensure_workspace,
    get_conversation_dir,
    get_project_dir,
    has_app,
)

__all__ = [
    "ContainerUnavailable",
    "control_dev_servers",
    "derive_secret",
    "describe_devenv",
    "ensure_container",
    "ensure_devenv",
    "ensure_network",
    "ensure_workspace",
    "extract_into_container",
    "get_container_name",
    "get_conversation_dir",
    "get_devenv_status",
    "get_gateway_ip",
    "get_host_socket_dir",
    "get_project_dir",
    "has_app",
    "is_container_running",
    "list_running_project_ids",
    "remove_container",
    "run_in_container",
    "run_in_container_binary",
    "stop_container",
    "stop_dev_servers",
    "to_container_path",
]
