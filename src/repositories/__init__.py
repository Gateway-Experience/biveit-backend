# The repository layer -- the ONLY place that knows about the concrete store (PostgreSQL). Services talk
# to repos in storage-neutral terms and never import asyncpg directly.
#
# Conventions in this layer:
#   - One module per table, named after the table. Structure stays FLAT -- it should mirror the table
#     list in the db, not the app's feature tree.
#   - Functions are thin and REUSABLE: `get` for one row by id, `get_by_<field>` for one by some other
#     unique key, `find` for a filtered list, plus `insert`/`update`/`delete`. A repo function
#     over-fitted to one caller's exact field selection is a smell -- one generic `find` serving
#     several callers is the goal.
#   - The schema lives in migrations/*.sql (applied by scripts/migrate.py); each module points at its
#     file. Rows come back as plain dicts with an int `id`.

from src.repositories import ai_conversations, ai_messages, events, kv_store, projects, system_configurations, users
from src.utils import postgres


async def check_store_reachable() -> bool:
    """Whether the store answers -- the health probe's actual check, expressed WITHOUT naming the
    engine. Lives here rather than in the controller so "which database is it" stays a fact only this
    layer knows. Raises if the store is unreachable."""
    return await postgres.ping()


__all__ = [
    "ai_conversations",
    "ai_messages",
    "check_store_reachable",
    "events",
    "kv_store",
    "projects",
    "system_configurations",
    "users",
]
