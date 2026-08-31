"""Postgres: the connection pool and the migration runner.

Raw asyncpg with parameterized SQL, not an ORM. Two of this SPEC's proofs are
*statement counts* - V2 wants "1,000 concurrent requests reach Postgres as <= 1
query" and V3 wants batching "verifiable by counting statements" - and you can
only argue about a number you can see. Every query in this project is a string
you can read, with `$1`-style placeholders that are sent to the server
separately from the data, so there is no string-concatenated SQL anywhere and
no injection surface (the "No SQL injection" checklist item).
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import structlog

__all__ = ["MIGRATIONS_TABLE", "create_pool", "run_migrations"]

log = structlog.get_logger(__name__)

MIGRATIONS_TABLE = "schema_migrations"


async def create_pool(dsn: str, *, min_size: int, max_size: int) -> asyncpg.Pool[asyncpg.Record]:
    """Open a bounded connection pool.

    The bound is the point. Every pooled connection is a *backend process* on the
    Postgres side, so `max_size` multiplied by the number of replicas has to stay
    under the server's `max_connections`. An unbounded pool does not make the
    database faster; it makes a traffic spike into a "too many clients already"
    outage.
    """
    return await asyncpg.create_pool(dsn=dsn, min_size=min_size, max_size=max_size)


async def run_migrations(pool: asyncpg.Pool[asyncpg.Record], migrations_dir: Path) -> list[str]:
    """Apply every `*.sql` in `migrations_dir` that has not run yet.

    Replaces `sqlx migrate run`, so migrating needs nothing installed beyond this
    project's own dependencies. Each file runs inside a transaction together with
    the row that records it, which is what makes a half-applied migration
    impossible: either the DDL and the bookkeeping both commit, or neither does.

    Returns the versions applied by this call, in order.
    """
    files = sorted(p for p in migrations_dir.glob("*.sql") if p.is_file())
    if not files:
        log.warning("no migrations found", directory=str(migrations_dir))
        return []

    async with pool.acquire() as conn:
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} ("
            "  version     TEXT PRIMARY KEY,"
            "  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")"
        )
        done: set[str] = {
            row["version"] for row in await conn.fetch(f"SELECT version FROM {MIGRATIONS_TABLE}")
        }

        applied: list[str] = []
        for path in files:
            version = path.stem
            if version in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute(f"INSERT INTO {MIGRATIONS_TABLE} (version) VALUES ($1)", version)
            log.info("migration applied", version=version)
            applied.append(version)

    if not applied:
        log.info("migrations up to date", count=len(files))
    return applied
