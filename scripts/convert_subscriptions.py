# Intentions: a ONE-OFF conversion of a database from the old payments tables to the current ones, which the normal
# migrations can't do (they only apply files they haven't seen, by name):
#   before  subscriptions (recurring only, the provider's own statuses) + orders (one-off purchases), from the old
#           0011_subscriptions.sql and 0012_orders.sql
#   after   one subscriptions table (migrations/0011_subscriptions.sql as it is now): recurring rows (provider
#           subscriptions) and lifetime ones (the one-time purchase, a provider order), our own statuses
# In one transaction: the old rows are kept aside, both tables dropped, subscriptions created from today's 0011, the
# old subscriptions put back (ids kept) and the one-time orders added as lifetime rows, and 0012_orders.sql's
# record removed from schema_migrations (its file is gone; 0012 is long_running_op_events now -- run the normal
# migrations after this). A database already converted is left alone.
#
# Statuses are mapped by the same code the webhooks use (lemonsqueezy.to_status). An unknown one stops the whole
# conversion (nothing is changed) -- the dry run lists them first.
#
# Run as a MODULE, from the backend folder (so `src` and `scripts` are importable):
#   uv run python -m scripts.convert_subscriptions                              # local, dry run: what it would do
#   uv run python -m scripts.convert_subscriptions --apply                      # local, for real
#   uv run python -m scripts.convert_subscriptions --target prod --confirm --apply
# Targets and DSNs work like scripts/migrate.py (.env.migration; --dsn overrides; non-local needs --confirm).

import asyncio
import json
import sys
from collections import Counter

import asyncpg

from scripts.migrate import MIGRATIONS_DIR, _resolve_dsn
from src.modules.payments.constants import Billing, Plan, Product, ProviderObject
from src.modules.payments.services.lemonsqueezy import to_status

_NEW_SCHEMA = MIGRATIONS_DIR / "0011_subscriptions.sql"
_OLD_ORDERS_MIGRATION = "0012_orders.sql"
_COLUMNS = [
    "id", "provider", "provider_object", "provider_ref", "billing", "user_id", "target_type", "target_id", "product",
    "plan", "product_data", "status", "renews_at", "ends_at", "trial_ends_at", "custom_data", "snapshot",
    "created_at", "updated_at",
]  # fmt: skip


def to_new_subscription(old: dict) -> dict:
    """[Pure] An old subscriptions row -> today's: a recurring provider subscription, our status. Id kept."""
    return {
        **{column: old[column] for column in _COLUMNS if column in old},
        "provider_object": ProviderObject.SUBSCRIPTION,
        "billing": Billing.RECURRING,
        "status": to_status(ProviderObject.SUBSCRIPTION, old["status"]),
    }


def to_new_one_time(order: dict) -> dict:
    """[Pure] An old orders row (the one-time purchase) -> today's: a lifetime provider order, our status. A new id
    (the table's next); created_at stays when it was paid."""
    return {
        "provider": order["provider"],
        "provider_object": ProviderObject.ORDER,
        "provider_ref": order["provider_ref"],
        "billing": Billing.LIFETIME,
        "user_id": order["user_id"],
        "target_type": order["target_type"],
        "target_id": order["target_id"],
        "product": Product.ONE_TIME,
        "plan": Plan.ONE_TIME,
        "product_data": {},
        "status": to_status(ProviderObject.ORDER, order["status"]),
        "renews_at": None,
        "ends_at": None,
        "trial_ends_at": None,
        "custom_data": order["custom_data"],
        "snapshot": order["snapshot"],
        "created_at": order["created_at"],
        "updated_at": order["updated_at"],
    }


def find_unknown_statuses(old_subscriptions: list[dict], old_orders: list[dict]) -> list[str]:
    """[Pure] The provider statuses to_status doesn't know, as "<object>:<status>" -- each would stop the conversion."""

    def is_known(provider_object: ProviderObject, status: str) -> bool:
        try:
            to_status(provider_object, status)
            return True
        except ValueError:
            return False

    unknown = [
        *[f"subscription:{row['status']}" for row in old_subscriptions if not is_known(ProviderObject.SUBSCRIPTION, row["status"])],
        *[f"order:{row['status']}" for row in old_orders if not is_known(ProviderObject.ORDER, row["status"])],
    ]  # fmt: skip
    return sorted(set(unknown))


async def _has_column(conn: asyncpg.Connection, table: str, column: str) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = $1 AND column_name = $2)",
            table,
            column,
        )
    )


async def _has_table(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table))


async def _fetch_rows(conn: asyncpg.Connection, sql: str) -> list[dict]:
    """Rows as plain dicts, jsonb decoded (asyncpg hands it over as text by default)."""
    await conn.set_type_codec("jsonb", encoder=_to_json, decoder=_from_json, schema="pg_catalog")
    return [dict(row) for row in await conn.fetch(sql)]


def _to_json(value: object) -> str:
    return json.dumps(value, default=str)


def _from_json(value: str) -> object:
    return json.loads(value)


async def convert(dsn: str, apply: bool) -> None:
    """Convert the database at `dsn` (see the top of this file) -- or, without `apply`, only say what it'd do."""
    conn = await asyncpg.connect(dsn)
    try:
        if await _has_column(conn, "subscriptions", "provider_object"):
            print("already converted: subscriptions has provider_object -- nothing to do")
            return
        has_orders = await _has_table(conn, "orders")
        old_subscriptions = await _fetch_rows(conn, "SELECT * FROM subscriptions ORDER BY id")
        old_orders = await _fetch_rows(conn, "SELECT * FROM orders ORDER BY id") if has_orders else []
        old_one_times = [order for order in old_orders if order["product"] == Product.ONE_TIME]
        unknown = find_unknown_statuses(old_subscriptions, old_one_times)
        if unknown:
            raise SystemExit(f"unknown provider statuses, nothing changed: {unknown}")
        new_subscriptions = [to_new_subscription(row) for row in old_subscriptions]
        new_one_times = [to_new_one_time(row) for row in old_one_times]
        print(f"subscriptions: {len(old_subscriptions)} -> recurring rows, ids kept")
        print(f"  statuses (old, new): {_count_status_changes(old_subscriptions, new_subscriptions)}")
        print(f"orders: {len(old_orders)}, of which {len(old_one_times)} one-time -> lifetime rows (others dropped)")
        print(f"  statuses (old, new): {_count_status_changes(old_one_times, new_one_times)}")
        if not apply:
            print("dry run -- nothing changed (add --apply to convert)")
            return
        async with conn.transaction():
            await conn.execute("DROP TABLE subscriptions")
            await conn.execute("DROP TABLE IF EXISTS orders")
            await conn.execute(_NEW_SCHEMA.read_text())
            # The old subscriptions keep their ids; the sequence then moves past them, so the one-time rows (new ids)
            # can't collide.
            await _insert_rows(conn, new_subscriptions, _COLUMNS)
            await conn.execute(
                "SELECT setval(pg_get_serial_sequence('subscriptions', 'id'), COALESCE(MAX(id), 0) + 1, false) "
                "FROM subscriptions"
            )
            await _insert_rows(conn, new_one_times, [column for column in _COLUMNS if column != "id"])
            await conn.execute("DELETE FROM schema_migrations WHERE name = $1", _OLD_ORDERS_MIGRATION)
        print(f"converted: {len(new_subscriptions) + len(new_one_times)} rows in subscriptions; orders dropped;")
        print(f"  {_OLD_ORDERS_MIGRATION} unrecorded")
        print("next: run the normal migrations (uv run python -m scripts.migrate ...)")
    finally:
        await conn.close()


def _count_status_changes(old_rows: list[dict], new_rows: list[dict]) -> dict:
    """[Pure] How many rows go from each old status to each new one: {(old, new): count}."""
    return dict(Counter((old["status"], str(new["status"])) for old, new in zip(old_rows, new_rows, strict=True)))


async def _insert_rows(conn: asyncpg.Connection, rows: list[dict], columns: list[str]) -> None:
    if not rows:
        return
    placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 1))
    sql = f"INSERT INTO subscriptions ({', '.join(columns)}) VALUES ({placeholders})"
    await conn.executemany(sql, [[row.get(column) for column in columns] for row in rows])


async def main() -> None:
    # argv: [--target NAME] [--dsn DSN] [--confirm] [--apply]
    args = sys.argv[1:]
    target = args[args.index("--target") + 1] if "--target" in args else "local"
    dsn_override = args[args.index("--dsn") + 1] if "--dsn" in args else None
    if (target != "local" or dsn_override) and "--confirm" not in args:
        raise SystemExit(f"--target {target} (or --dsn) needs --confirm; refusing to run.")
    await convert(dsn_override or _resolve_dsn(target), apply="--apply" in args)


if __name__ == "__main__":
    asyncio.run(main())
