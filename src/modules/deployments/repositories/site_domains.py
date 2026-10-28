# Intentions: data access for `site_domains` -- the domains attached to a project's production site, one row each,
# a domain belonging to at most one project; at most one of a project's is ACTIVE (activated_at set), the one the
# site is served on. Schema: migrations/0010_site_domains.sql.

from typing import Any

import asyncpg
import pydash

from src.utils import postgres

_COLUMNS = "id, project_id, domain, kind, activated_at, created_at"


class DomainTaken(Exception):
    """A domain is already another project's (uq_site_domains_domain). Raised instead of leaking asyncpg's
    UniqueViolationError past this layer."""


async def find(criteria: dict[str, Any]) -> list[dict]:
    """Rows matching `criteria` (column = value, ANDed), oldest first. A project's ({"project_id": ..}) rides
    idx_site_domains_project; one domain's ({"domain": ..}) the unique index."""
    where = pydash.pick(criteria, "id", "project_id", "domain", "kind")
    sql = f"SELECT {_COLUMNS} FROM site_domains WHERE {postgres.to_where_clause(where)} ORDER BY created_at, id"
    return await postgres.fetch(sql, where)


async def attach(project_id: int, domain: str, kind: str) -> dict:
    """Attach a domain to the project (not active), or return its row if it's already the project's (kind kept).
    Raises DomainTaken when it's another project's."""
    sql = f"""
        INSERT INTO site_domains (project_id, domain, kind) VALUES ($project_id, $domain, $kind)
        ON CONFLICT (domain) DO UPDATE SET domain = EXCLUDED.domain WHERE site_domains.project_id = $project_id
        RETURNING {_COLUMNS}
    """
    row = await postgres.fetchrow(sql, {"project_id": project_id, "domain": domain, "kind": kind})
    if row is None:  # the conflict's WHERE didn't match: someone else's
        raise DomainTaken(domain)
    return row


async def activate(project_id: int, domain: str) -> bool:
    """Make one of the project's attached domains its active one (any other stops being active). Two updates in a
    transaction: clearing first, so uq_site_domains_active never sees two at once. Returns False if the domain
    isn't the project's."""
    params = {"project_id": project_id, "domain": domain}
    statements = [
        (
            "UPDATE site_domains SET activated_at = NULL WHERE project_id = $project_id AND domain <> $domain "
            "AND activated_at IS NOT NULL"
        ),
        # Already active: left as is (activated_at keeps saying when it became active).
        (
            "UPDATE site_domains SET activated_at = now() WHERE project_id = $project_id AND domain = $domain "
            "AND activated_at IS NULL"
        ),
    ]
    async with postgres.pool().acquire() as conn, conn.transaction():
        for statement in statements:  # in order: clear, then set
            sql, args = postgres.to_positional(statement, params)
            await conn.execute(sql, *args)
        sql, args = postgres.to_positional(
            "SELECT EXISTS (SELECT 1 FROM site_domains WHERE project_id = $project_id AND domain = $domain)", params
        )
        exists = await conn.fetchval(sql, *args)
    return bool(exists)


async def detach(project_id: int, domain: str) -> None:
    """Take a domain off the project (the name is free for anyone after)."""
    await postgres.execute(
        "DELETE FROM site_domains WHERE project_id = $project_id AND domain = $domain",
        {"project_id": project_id, "domain": domain},
    )


async def replace(project_id: int, domains: list[str]) -> None:
    """Make `domains` exactly the project's, as admin-set ones: drop the ones it no longer lists, add the new ones
    (the ones it keeps keep their rows). One statement, so all or nothing -- a domain that's another project's
    raises DomainTaken and changes nothing. Which one is active is the caller's to set (activate)."""
    sql = """
        WITH dropped AS (
            DELETE FROM site_domains WHERE project_id = $project_id AND NOT (domain = ANY($domains::text[]))
        )
        INSERT INTO site_domains (project_id, domain, kind)
        SELECT $project_id, listed.domain, 'admin' FROM unnest($domains::text[]) AS listed (domain)
        WHERE NOT EXISTS (
            SELECT 1 FROM site_domains WHERE project_id = $project_id AND domain = listed.domain
        )
    """
    try:
        await postgres.execute(sql, {"project_id": project_id, "domains": domains})
    except asyncpg.UniqueViolationError as error:
        raise DomainTaken(domains) from error
