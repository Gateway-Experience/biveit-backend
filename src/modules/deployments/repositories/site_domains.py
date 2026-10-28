# Intentions: data access for `site_domains` -- the domains a project's production site is served on, one row
# each, a domain belonging to at most one project. Schema: migrations/0010_site_domains.sql.

from typing import Any

import asyncpg
import pydash

from src.utils import postgres

_COLUMNS = "id, project_id, domain, created_at"


class DomainTaken(Exception):
    """A domain is already another project's (uq_site_domains_domain). Raised instead of leaking asyncpg's
    UniqueViolationError past this layer."""


async def find(criteria: dict[str, Any]) -> list[dict]:
    """Rows matching `criteria` (column = value, ANDed), oldest first. A project's ({"project_id": ..}) rides
    idx_site_domains_project; one domain's ({"domain": ..}) the unique index."""
    where = pydash.pick(criteria, "id", "project_id", "domain")
    sql = f"SELECT {_COLUMNS} FROM site_domains WHERE {postgres.to_where_clause(where)} ORDER BY created_at, id"
    return await postgres.fetch(sql, where)


async def replace(project_id: int, domains: list[str]) -> None:
    """Make `domains` exactly the project's: drop the ones it no longer lists, add the new ones (the ones it
    keeps keep their rows). One statement, so all or nothing -- a domain that's another project's raises
    DomainTaken and changes nothing."""
    sql = """
        WITH dropped AS (
            DELETE FROM site_domains WHERE project_id = $project_id AND NOT (domain = ANY($domains::text[]))
        )
        INSERT INTO site_domains (project_id, domain)
        SELECT $project_id, listed.domain FROM unnest($domains::text[]) AS listed (domain)
        WHERE NOT EXISTS (
            SELECT 1 FROM site_domains WHERE project_id = $project_id AND domain = listed.domain
        )
    """
    try:
        await postgres.execute(sql, {"project_id": project_id, "domains": domains})
    except asyncpg.UniqueViolationError as error:
        raise DomainTaken(domains) from error
