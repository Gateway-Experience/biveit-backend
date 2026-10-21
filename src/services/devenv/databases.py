# Intentions: a project's access to the platform's Mongo in DEV. The USER is the security boundary, so
# credentials are per user: each project owner gets one Mongo user, user_<id>, its password derived from
# PROJECT_SECRET_SEED (nothing stored), and it's granted readWrite on each of their projects' dev
# databases as those get set up. A user's projects can reach each other's data -- it's all theirs -- but
# never another user's.
#
# The backend manages those users through PROJECT_MONGO_URI (which needs the userAdmin role); the projects'
# apps connect to PROJECT_MONGO_HOST with their owner's credentials. Prod isn't covered yet: deployed
# projects are meant to get per-project credentials.

from urllib.parse import quote

from src.config import settings
from src.dto.projects import Project
from src.services.devenv import secrets
from src.utils import mongo


def get_dev_database(project_id: int) -> str:
    return f"db_project_dev_{project_id}"


def get_mongo_user(user_id: int) -> str:
    return f"user_{user_id}"


def get_dev_mongo_uri(project: Project) -> str:
    """[Pure-ish] The connection string the project's dev backend uses: its owner's user, its own database."""
    user = get_mongo_user(project["user_id"])
    password = quote(_derive_password(project["user_id"]), safe="")
    return (
        f"mongodb://{user}:{password}@{settings.project_mongo_host}/{get_dev_database(project['id'])}?authSource=admin"
    )


async def ensure_dev_access(project: Project) -> None:
    """Make sure the project's owner has their Mongo user, and that it can readWrite the project's dev
    database. Idempotent -- safe on every setup."""
    await mongo.ensure_user(
        settings.project_mongo_uri,
        get_mongo_user(project["user_id"]),
        _derive_password(project["user_id"]),
        [{"role": "readWrite", "db": get_dev_database(project["id"])}],
    )


def _derive_password(user_id: int) -> str:
    return secrets.derive_secret(f"user:{user_id}:mongo", 32)
