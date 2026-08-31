"""Postgres: the connection pool and the migration runner.

Raw asyncpg with parameterized SQL, not an ORM — and here that is not a
performance preference, it is the subject. V3's lesson *is* the scan statement
(`… WHERE fire_at <= now() FOR UPDATE SKIP LOCKED`), V4's *is* the claim, and
V1's *is* that an append of N events is one transaction. An ORM would hide the
exact lines the SPEC grades.

Every query in this project is a string you can read, with `$1`-style
placeholders sent to the server separately from the data — so there is no
string-concatenated SQL anywhere and no injection surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import asyncpg
import structlog

__all__ = ["MIGRATIONS_DIR", "MIGRATIONS_TABLE", "Executor", "create_pool", "run_migrations"]

log = structlog.get_logger(__name__)

MIGRATIONS_TABLE = "schema_migrations"

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
"""Where `0001_init.sql` and friends live, relative to this package."""

type Executor = asyncpg.Pool[asyncpg.Record] | asyncpg.Connection[asyncpg.Record]
"""Anything that can run a query: the pool, or one connection inside a transaction.

Worth naming, because it is how this engine composes atomic work without a
transaction object threaded through every signature. A method that takes an
`Executor` can be called two ways: hand it the pool and it runs standalone; hand
it the connection you already have a transaction open on and it joins that
transaction instead. `TIMER_STARTED` and its `timers` row committing together
(V3), and a whole workflow-task completion landing at once (V4), are both just
"pass the same connection to each step".

It is the Python answer to Rust's `&mut Transaction` parameter — with the same
rule attached: a method that quietly acquires its *own* connection while its
caller holds a transaction has silently opted out of that transaction, and the
atomicity the SPEC grades is gone with no error to show for it.
"""


async def _init_connection(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    """Teach a fresh connection to hand `JSONB` back as a Python object.

    Without this asyncpg returns `attributes` as the raw text it arrived as, so
    every caller would have to remember to `json.loads` it — and the one that
    forgets writes a JSON-encoded *string* back into the column on the next
    append, corrupting an event that is by definition never going to be edited
    back. Doing it once, per connection, makes `event.attributes` simply be the
    attributes.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def create_pool(dsn: str, *, min_size: int, max_size: int) -> asyncpg.Pool[asyncpg.Record]:
    """Open a bounded connection pool.

    The bound is the point. Every pooled connection is a *backend process* on the
    Postgres side, so `max_size` × replicas has to stay under the server's
    `max_connections`. An unbounded pool does not make the database faster; it
    makes a traffic spike into a "too many clients already" outage.

    This engine makes the sizing question sharper than most, because a parked
    long-poll and a running transaction are both "in flight" but only one of them
    should be holding a connection. A poller that keeps a connection checked out
    while it waits for work has turned the pool into the queue's real concurrency
    limit — which is a fine design as long as you chose it on purpose and wrote
    down why.
    """
    return await asyncpg.create_pool(
        dsn=dsn, min_size=min_size, max_size=max_size, init=_init_connection
    )


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
        rows: list[Any] = await conn.fetch(f"SELECT version FROM {MIGRATIONS_TABLE}")
        done: set[str] = {row["version"] for row in rows}

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
