# Intentions: apply migrations/*.sql in filename order, idempotently. This creates NEW tables; a change
# to an existing table is made by editing its file in place + running the DDL by hand (the files double
# as table docs), so a re-run won't replay it. Applied files are recorded in a
# schema_migrations table, so a re-run only applies the new ones. Each file runs in its own transaction
# together with its marker, so a failing file leaves nothing half-applied.
#
# Run as a MODULE (so `src` is importable):
#   uv run python -m scripts.migrate                               # local, every pending file
#   uv run python -m scripts.migrate --target prod --confirm       # prod, every pending file
#   uv run python -m scripts.migrate --only 0001 0002              # only files starting with these prefixes
#   uv run python -m scripts.migrate --force --only 0003           # RE-APPLY an already-recorded file
#
# TARGET: DSNs live in ONE dedicated file, .env.migration (gitignored; template: .env.migration.example),
# kept apart from the app's .env.<env> files. `--target <name>` (default local) reads POSTGRES_DSN_<NAME>
# from it. A missing file or variable is a hard error -- never a silent fallback, so a mistyped target
# can't hit the wrong DB. `--dsn <dsn>` overrides the file entirely. Any non-local target (or an explicit
# --dsn) needs `--confirm`, so a stray `--target prod` can't apply without an ack.
# References: evolix-backend scripts/migrate.py (same mechanism, minus the clickhouse store).

import asyncio
import sys
from pathlib import Path

import asyncpg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"
ENV_MIGRATION = ROOT / ".env.migration"


def _resolve_dsn(target: str) -> str:
    """POSTGRES_DSN_<TARGET> from .env.migration. The file is parsed, NOT loaded into os.environ, so
    migration config never leaks into the process env. Missing file/var -> hard error."""
    if not ENV_MIGRATION.exists():
        raise SystemExit(f"{ENV_MIGRATION.name} not found (holds the migration DSNs); refusing to run.")
    var = f"POSTGRES_DSN_{target.upper()}"
    dsn = dotenv_values(ENV_MIGRATION).get(var)
    if not dsn:
        raise SystemExit(f"--target {target} needs {var} set in {ENV_MIGRATION.name}; refusing to run.")
    return dsn


def _select_files(only: list[str] | None) -> list[Path]:
    """[Pure-ish] Every .sql in filename order, optionally narrowed to names starting with an `only` prefix."""
    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return paths if only is None else [p for p in paths if any(p.name.startswith(prefix) for prefix in only)]


async def migrate(dsn: str, only: list[str] | None = None, force: bool = False) -> None:
    """Apply pending migrations to `dsn`. `force` re-applies already-recorded files (for an edited
    migration) and refreshes their marker instead of skipping them."""
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
        )
        applied = {row["name"] for row in await conn.fetch("SELECT name FROM schema_migrations")}
        pending = [p for p in _select_files(only) if force or p.name not in applied]
        for path in pending:
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES ($1) ON CONFLICT (name) DO UPDATE SET applied_at = now()",
                    path.name,
                )
            print(f"{'force-applied' if path.name in applied else 'applied'} {path.name}")
        if not pending:
            print("nothing to apply")
    finally:
        await conn.close()


async def main() -> None:
    # argv: [--target NAME] [--dsn DSN] [--confirm] [--force] [--only PREFIX ...]. --only swallows the
    # rest of argv, so it goes last.
    args = sys.argv[1:]
    only = args[args.index("--only") + 1 :] if "--only" in args else None
    target = args[args.index("--target") + 1] if "--target" in args else "local"
    dsn_override = args[args.index("--dsn") + 1] if "--dsn" in args else None

    if (target != "local" or dsn_override) and "--confirm" not in args:
        raise SystemExit(f"refusing to migrate target '{target}' / an explicit --dsn without --confirm.")

    print(f"migrating target={target}" + (" (explicit --dsn)" if dsn_override else ""))
    await migrate(dsn_override or _resolve_dsn(target), only, "--force" in args)


if __name__ == "__main__":
    asyncio.run(main())
