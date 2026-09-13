"""Postgres: the connection type, the pool, and the migration runner.

Raw asyncpg with parameterized SQL, not an ORM: this project's lessons live in the
statements themselves — the isolation level a transfer's transaction runs under
(V2), the reservation insert exactly one racer wins (V3), the `SKIP LOCKED` claim
(V4). Every query is a string you can read, with `$1`-style placeholders sent to the
server separately from the data, so there is no string-built SQL and no injection
surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import asyncpg
import structlog
from asyncpg.pool import PoolConnectionProxy

__all__ = ["MIGRATIONS_DIR", "MIGRATIONS_TABLE", "Conn", "create_pool", "run_migrations"]

log = structlog.get_logger(__name__)

MIGRATIONS_TABLE = "schema_migrations"

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
"""Where `0001_init.sql` and friends live, relative to this package."""

type Conn = asyncpg.Connection[asyncpg.Record] | PoolConnectionProxy[asyncpg.Record]
"""One connection — what a function takes when it must run *inside a transaction its
caller opened*. (`PoolConnectionProxy` is what `pool.acquire()` actually yields.)

This type is what makes the verticals compose. A `Pool` hands each statement to
whichever connection is free, and a transaction lives on exactly one connection, so a
function that takes the pool can never join its caller's transaction. V2's transfer
reads the balance, posts V1's entries and enqueues V4's outbox event on the *same*
`Conn`, inside one `conn.transaction()` — that shared connection is the atomicity.
"""


async def _init_connection(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    """Teach a fresh connection to hand `JSONB` back as a Python object.

    `idempotency_keys.response_body` and `webhook_outbox.payload` are JSONB. Without
    this codec asyncpg returns them as the raw text they arrived as, and a replay
    serves a JSON-encoded *string* where the original response served an object — a
    replay that is no longer indistinguishable from the original, which is the one
    thing V3 promises.
    """
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str, *, min_size: int, max_size: int) -> asyncpg.Pool[asyncpg.Record]:
    """Open a bounded connection pool.

    The bound is the point. Every pooled connection is a backend *process* on the
    Postgres side, so `max_size` × server processes has to stay under the server's
    `max_connections` — and scaling out for the boss fight moves that product. An
    unbounded pool turns "add processes to go faster" into "too many clients already".
    """
    return await asyncpg.create_pool(
        dsn=dsn, min_size=min_size, max_size=max_size, init=_init_connection
    )


async def run_migrations(pool: asyncpg.Pool[asyncpg.Record], migrations_dir: Path) -> list[str]:
    """Apply every `*.sql` in `migrations_dir` that has not run yet.

    Replaces `sqlx migrate run`, so migrating needs nothing installed beyond this
    project's own dependencies. Each file runs inside a transaction together with the
    row recording it: the DDL and the bookkeeping both commit, or neither.

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
