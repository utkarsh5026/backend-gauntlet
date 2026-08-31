"""`python -m workflow_engine.migrate` — apply migrations and exit.

The Python answer to `sqlx migrate run`, wired into `make migrate`. Reads the
same `.env` the engine does, so there is one source of truth for `DATABASE_URL`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import common_telemetry

from .config import Settings
from .db import MIGRATIONS_DIR, create_pool, run_migrations

log = common_telemetry.get_logger(__name__)


async def _run(directory: Path) -> int:
    cfg = Settings()
    pool = await create_pool(cfg.database_url, min_size=1, max_size=2)
    try:
        applied = await run_migrations(pool, directory)
    finally:
        await pool.close()
    log.info("migrate complete", applied=applied, count=len(applied))
    return 0


def main() -> None:
    common_telemetry.init("info")
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else MIGRATIONS_DIR
    if not directory.is_dir():
        log.error("migrations directory not found", directory=str(directory))
        raise SystemExit(1)
    raise SystemExit(asyncio.run(_run(directory)))


if __name__ == "__main__":
    main()
