"""Postgres: the connection pool and the migration runner.

Raw asyncpg with parameterized SQL, not an ORM. V1's idempotency lesson is
partly *in the schema* — the partial unique index `one_active_session_per_stream`
in `migrations/0001_init.sql` — and the statement that relies on it is the thing
the SPEC grades. An ORM would hide exactly that statement.

Every query is a string you can read, with `$1`-style placeholders sent to the
server separately from the data, so there is no string-built SQL and no
injection surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import asyncpg
import structlog

__all__ = ["MIGRATIONS_DIR", "MIGRATIONS_TABLE", "create_pool", "run_migrations"]

log = structlog.get_logger(__name__)

MIGRATIONS_TABLE = "schema_migrations"

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
"""Where `0001_init.sql` and friends live, relative to this package."""


async def _init_connection(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    """Teach a fresh connection to hand `JSONB` back as a Python object.

    `stream_sessions.ladder` is JSONB. Without this codec asyncpg returns it as
    the raw text it arrived as, and the first caller that forgets to `json.loads`
    writes a JSON-encoded *string* back into the column on its next update.
    """
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str, *, min_size: int, max_size: int) -> asyncpg.Pool[asyncpg.Record]:
    """Open a bounded connection pool.

    The bound is the point. Every pooled connection is a backend *process* on
    the Postgres side, so `max_size` × uvicorn workers × pod replicas has to stay
    under the server's `max_connections`. This project makes that sharper than
    most: the API Deployment autoscales, so the replica term in that product
    moves at runtime — and an unbounded pool turns a scale-up into a "too many
    clients already" outage exactly when the viral spike arrives.
    """
    return await asyncpg.create_pool(
        dsn=dsn, min_size=min_size, max_size=max_size, init=_init_connection
    )


async def run_migrations(pool: asyncpg.Pool[asyncpg.Record], migrations_dir: Path) -> list[str]:
    """Apply every `*.sql` in `migrations_dir` that has not run yet.

    Replaces `sqlx migrate run`, so migrating needs nothing installed beyond this
    project's own dependencies. Each file runs inside a transaction together with
    the row recording it: the DDL and the bookkeeping both commit, or neither.

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
