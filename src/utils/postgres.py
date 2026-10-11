# Intentions: low-level async PostgreSQL access (shared pool + query helpers). Infra plumbing in utils/;
# repositories call fetch()/fetchrow()/execute(), never manage the pool.
#
# Connection-module convention (shared by postgres / redis / http):
#   - connect(): create the singleton ONCE at service startup, before serving. No lock needed --
#     startup is sequential, so there's no concurrent-first-caller race to guard.
#   - pool(): return the live handle (asserts connect() ran).
#   - close(): graceful shutdown.
#
# Schema lives in migrations/*.sql, applied by scripts/migrate.py -- NOT at app startup.
# References: evolix-backend src/utils/postgres.py (same shape, minus transactions until a flow needs one).

import json
import re
from datetime import date, datetime
from decimal import Decimal

import asyncpg

from src.config import settings

_pool: asyncpg.Pool | None = None


def _json_default(value: object) -> str:
    """[Pure] jsonb encoder fallback for the types plain json.dumps rejects: Decimal -> str (keeps exact
    precision), datetime/date -> ISO string. Anything else is a genuine bug, so raise."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


async def _init_conn(conn: asyncpg.Connection) -> None:
    """Let asyncpg take/return plain Python objects for jsonb columns (it speaks raw strings otherwise)."""
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda value: json.dumps(value, default=_json_default),
        decoder=json.loads,
        schema="pg_catalog",
    )


# ci:trivial
async def connect() -> None:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.postgres_dsn, init=_init_conn, max_size=settings.postgres_pool_max_size
        )


# ci:trivial
def pool() -> asyncpg.Pool:
    assert _pool is not None, "postgres not connected; call connect() at startup"
    return _pool


# ci:trivial
async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def ping() -> bool:
    """Whether the server answers. The health endpoint's actual check -- a real round trip, not a
    pool-side flag. Raises if the server is unreachable."""
    await pool().fetchval("SELECT 1")
    return True


# --- Named params ---------------------------------------------------------------------------------
# asyncpg is positional-only ($1, $2, ...), which is brittle: SQL and the args list drift apart and a
# reorder silently mis-binds columns. These helpers let a query name its params ($username) and take a
# dict instead. They also dict()-cast rows, so repos can return results directly.

# `$` + an identifier. Can't start with a digit, so positional `$1` and dollar-quoting `$$` are left
# alone; the lookbehind keeps `a$b` / `$$tag$$` untouched too.
_NAMED_PLACEHOLDER = re.compile(r"(?<![$\w])\$([a-zA-Z_]\w*)")

# What a dynamically-built column name may look like. Callers whitelist columns already; this is the
# second fence so a stray key can never be spliced into SQL.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def to_positional(sql: str, params: dict) -> tuple[str, list]:
    """[Pure] Translate `$name` placeholders to `$1, $2, ...`, returning the rewritten SQL and the args in
    matching order. A name used twice binds once. Raises KeyError if `params` lacks a name -- fail loud
    rather than mis-bind. Exposed for callers holding their own connection (e.g. scripts/migrate.py)."""
    order: dict[str, int] = {}  # name -> 1-based position, assigned on first sighting

    def _index(match: re.Match) -> str:
        name = match.group(1)
        if name not in params:
            raise KeyError(f"named param ${name} has no value in params")
        order.setdefault(name, len(order) + 1)
        return f"${order[name]}"

    positional_sql = _NAMED_PLACEHOLDER.sub(_index, sql)
    return positional_sql, [params[name] for name in order]


# --- Dynamic clauses ------------------------------------------------------------------------------
# For statements whose columns are only known at runtime (a partial update, an equality filter). Each
# takes the params dict itself and renders `$name` placeholders for it, so pass that same dict as the
# query params. The CALLER whitelists the keys; these just refuse anything that isn't a plain identifier.


def _assert_identifiers(columns) -> None:
    bad_columns = [column for column in columns if not _IDENTIFIER.match(column)]
    if bad_columns:
        raise ValueError(f"not a column name: {bad_columns}")


def to_set_clause(updates: dict) -> str:
    """[Pure] `{"a": .., "b": ..}` -> `"a = $a, b = $b"` (empty dict -> "")."""
    _assert_identifiers(updates)
    return ", ".join(f"{column} = ${column}" for column in updates)


def to_where_clause(criteria: dict) -> str:
    """[Pure] `{"a": .., "b": ..}` -> `"a = $a AND b = $b"`, or `"TRUE"` for no criteria."""
    _assert_identifiers(criteria)
    return " AND ".join(f"{column} = ${column}" for column in criteria) or "TRUE"


def to_insert_clause(row: dict) -> str:
    """[Pure] `{"a": .., "b": ..}` -> `"(a, b) VALUES ($a, $b)"`."""
    _assert_identifiers(row)
    return f"({', '.join(row)}) VALUES ({', '.join(f'${column}' for column in row)})"


# ci:trivial
async def execute(sql: str, params: dict | None = None) -> str:
    """Run a statement with `$name` placeholders. Returns asyncpg's status tag (e.g. "DELETE 1")."""
    positional_sql, args = to_positional(sql, params or {})
    return await pool().execute(positional_sql, *args)


# ci:trivial
async def fetch(sql: str, params: dict | None = None) -> list[dict]:
    positional_sql, args = to_positional(sql, params or {})
    return [dict(row) for row in await pool().fetch(positional_sql, *args)]


# ci:trivial
async def fetchrow(sql: str, params: dict | None = None) -> dict | None:
    positional_sql, args = to_positional(sql, params or {})
    row = await pool().fetchrow(positional_sql, *args)
    return dict(row) if row else None


async def execute_many(sql: str, params_list: list[dict]) -> None:
    """Run one statement for each params dict, in a single batched round trip. Every dict must carry
    every name the SQL uses (the placeholder order is worked out from the first one)."""
    if not params_list:
        return
    positional_sql, _ = to_positional(sql, params_list[0])
    names = _NAMED_PLACEHOLDER.findall(sql)
    ordered_names = list(dict.fromkeys(names))  # first-appearance order, matching to_positional's $1..$n
    await pool().executemany(positional_sql, [[params[name] for name in ordered_names] for params in params_list])
