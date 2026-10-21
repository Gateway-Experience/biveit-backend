# Intentions: the few Mongo admin operations the platform needs -- managing users on a Mongo it doesn't
# otherwise use (the projects' apps own their data; the platform itself is on Postgres). Generic: what
# the users and roles mean is the caller's business. A client per call: these run rarely, at setup.

from pymongo import AsyncMongoClient


async def ensure_user(admin_uri: str, user: str, password: str, roles: list[dict]) -> None:
    """Make sure `user` exists (in the admin database) with `password` and at least `roles`
    ({role, db} dicts). Idempotent: an existing user gets the roles added -- granting one it already has
    is a no-op -- and its password reset to `password`. `admin_uri` must be allowed to manage users."""
    client = AsyncMongoClient(admin_uri)
    try:
        admin = client["admin"]
        existing = await admin.command("usersInfo", user)
        if existing["users"]:
            await admin.command("updateUser", user, pwd=password)
            await admin.command("grantRolesToUser", user, roles=roles)
        else:
            await admin.command("createUser", user, pwd=password, roles=roles)
    finally:
        await client.close()
