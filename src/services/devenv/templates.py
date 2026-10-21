# Intentions: rendering a project's files from the templates in DEVENV_TEMPLATES_DIR (adapted from
# gateway-exp-deployment's render_env.py).
#
# A template is any text file where `{{{<python expression>}}}` is replaced by its value. In scope:
#   project_id, conversation_id
#   dev_backend_port, dev_frontend_port, dev_host (None without DEV_DOMAIN), dev_url, workspace_dir
#   dev_redis_db            the project's own Redis database for dev
#   dev_mongo_uri()         the project's dev database, logged in as its owner (devenv/databases)
#   settings                the backend's settings, e.g. {{{settings.dev_domain}}} -- never its credentials:
#                           whatever a template renders, the project's agent can read
#   secret(name, length=48) a secret DERIVED from PROJECT_SECRET_SEED, the project id and the name
#                           (devenv/secrets), so it's the same every time it's rendered (dev and prod env
#                           files share it) and different in every project. Nothing to store.
# Credentials come in through `settings`, so the templates themselves hold none and can be committed.
# Templates are ours and trusted: expressions run with eval, without builtins.

import os
import re

from src.config import settings
from src.dto.projects import Project
from src.services.devenv import addresses, databases, secrets, workspaces

_PLACEHOLDER = re.compile(r"\{\{\{(.+?)\}\}\}")


class TemplateFailed(Exception):
    """A template is missing, or one of its expressions failed. The message names the template."""


def render_template(template_name: str, project: Project, conversation_id: int) -> str:
    """[Pure-ish] The template DEVENV_TEMPLATES_DIR/template_name rendered for this project's workspace.
    Raises TemplateFailed."""
    template_path = os.path.join(os.path.expanduser(settings.devenv_templates_dir), template_name)
    scope = _get_scope(project, conversation_id)
    try:
        with open(template_path, encoding="utf-8") as template_file:
            template_text = template_file.read()
        return _PLACEHOLDER.sub(lambda match: str(eval(match.group(1), {"__builtins__": {}}, scope)), template_text)
    except Exception as error:
        raise TemplateFailed(f"{template_name}: {type(error).__name__}: {error}") from error


def _get_scope(project: Project, conversation_id: int) -> dict:
    """[Pure-ish] What a template's expressions can use (listed at the top of this file)."""
    project_id = project["id"]
    return {
        "project_id": project_id,
        "conversation_id": conversation_id,
        "dev_backend_port": addresses.get_dev_backend_port(project_id),
        "dev_frontend_port": addresses.get_dev_frontend_port(project_id),
        "dev_host": addresses.get_dev_host(project),
        "dev_url": addresses.get_dev_url(project),
        "dev_redis_db": addresses.get_dev_redis_db(project_id),
        "workspace_dir": workspaces.get_conversation_dir(project, conversation_id),
        "settings": settings,
        "dev_mongo_uri": lambda: databases.get_dev_mongo_uri(project),
        "secret": lambda name, length=48: secrets.derive_secret(f"project:{project_id}:{name}", length),
    }
