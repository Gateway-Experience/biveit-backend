# Intentions: what a project's site runs with in production -- its own database access and its env vars.
#
# Prod is kept apart from dev on purpose: the site logs into Mongo as its own per-PROJECT user, site_<id>,
# allowed only its prod database (dev logs in as the owner, user_<id>, see devenv/databases), and its secrets
# are derived under their own subject ("project:<id>:prod:<name>"), so nothing in the dev container -- which
# the agent can read -- opens the live site. The env vars come from the boilerplate's SITE_ENV_TEMPLATE,
# rendered here and handed to the container directly: they're never written into the workspace.

import io
import os
from urllib.parse import quote

from dotenv import dotenv_values

from src.config import settings
from src.dto.projects import Project
from src.modules.deployments.constants import SITE_ENV_TEMPLATE, SITE_INTERNAL_BACKEND_PORT
from src.services import devenv
from src.utils import mongo, templates


def get_site_database(project_id: int) -> str:
    return f"db_project_{project_id}"


async def ensure_database_access(project: Project) -> None:
    """Make sure the site's Mongo user exists and can readWrite its prod database. Idempotent."""
    await mongo.ensure_user(
        settings.project_mongo_uri,
        _get_mongo_user(project["id"]),
        _derive_secret(project["id"], "mongo", 32),
        [{"role": "readWrite", "db": get_site_database(project["id"])}],
    )


def render_site_env(project: Project, site_urls: list[str]) -> dict[str, str]:
    """[Pure-ish] The site's env vars: SITE_ENV_TEMPLATE (from our boilerplate checkout, never a workspace's
    copy -- its expressions run) rendered and parsed. `site_urls` are where the site is served (its CORS
    origins). In the template's scope:
      project_id, backend_port (the backend's own port in the container), mongo_uri, mongo_db,
      site_origins (site_urls, comma-separated), settings,
      secret(name, length=48)   derived per project, under the prod subject (see the top of this file)
    Raises templates.TemplateFailed."""
    project_id = project["id"]
    password = quote(_derive_secret(project_id, "mongo", 32), safe="")
    scope = {
        "project_id": project_id,
        "backend_port": SITE_INTERNAL_BACKEND_PORT,
        "mongo_uri": (
            f"mongodb://{_get_mongo_user(project_id)}:{password}@{settings.project_mongo_host}"
            f"/{get_site_database(project_id)}?authSource=admin"
        ),
        "mongo_db": get_site_database(project_id),
        "site_origins": ",".join(site_urls),
        "settings": settings,
        "secret": lambda name, length=48: _derive_secret(project_id, name, length),
    }
    template_path = os.path.join(os.path.realpath(os.path.expanduser(settings.boilerplate_dir)), SITE_ENV_TEMPLATE)
    env_text = templates.render_template_file(template_path, scope)
    return {name: value or "" for name, value in dotenv_values(stream=io.StringIO(env_text)).items()}


def _get_mongo_user(project_id: int) -> str:
    return f"site_{project_id}"


def _derive_secret(project_id: int, name: str, length: int) -> str:
    return devenv.derive_secret(f"project:{project_id}:prod:{name}", length)
