"""Typed settings for the job queue.

Every field maps to a variable in `.env.example`, and the type annotation is the
parser: declaring `port: int` gets the env lookup, the string->int coercion, the
default, and a startup error naming the offending variable.

The durations are stored as the units the environment expresses them in
(`POLL_INTERVAL_MS`, `VISIBILITY_TIMEOUT_SECS`) and exposed as `float` seconds
through properties, so nothing downstream has to remember which variable was in
which unit.
"""

from __future__ import annotations

from pathlib import Path

from common_config import BaseConfig
from pydantic import Field

__all__ = ["Settings"]


class Settings(BaseConfig):
    port: int = 8080
    """HTTP port. The compose file publishes Postgres on 5404 (project-scoped)."""

    log_level: str = "info"

    enqueue_token: str = ""
    """Shared bearer token required on the mutating routes (`POST /jobs`, requeue).

    Empty = auth disabled (dev only). `main` warns loudly, because with the
    `exec`/`shell` job kinds an open `POST /jobs` is remote code execution on every
    worker, not merely a way to make them busy."""

    database_url: str = "postgres://jobs:jobs@localhost:5404/jobs"
    """Postgres — the durable store AND the queue broker."""

    db_pool_min: int = Field(default=2, ge=1)
    db_pool_max: int = Field(default=20, ge=1)
    """Bounded on purpose (the "bounded pool sized on purpose" checklist item).

    The ceiling is a property of Postgres, not of this process: every pooled
    connection is a backend process over there, so the sum across all replicas has
    to stay under `max_connections`. Size it *with* `worker_concurrency` — a worker
    blocked waiting for a connection is a stalled worker, so the pool needs enough
    room for every worker's claim plus the API's own traffic."""

    # --- Worker pool ---

    run_workers: bool = False
    """Off by default, so the API can be served without a worker pool attached."""

    worker_concurrency: int = Field(default=4, ge=1)
    queue: str = "default"
    """Which named queue this process's workers drain."""

    poll_interval_ms: int = Field(default=1000, ge=1)
    """How often an idle worker re-checks. V4's LISTEN/NOTIFY makes this the
    *fallback* cadence rather than the pickup latency."""

    claim_batch: int = Field(default=10, ge=1)
    """Jobs claimed per round-trip. One-per-round-trip spends the worker's time on
    network latency instead of on work."""

    # --- Visibility timeout / lease (V2) ---

    visibility_timeout_secs: int = Field(default=30, ge=1)
    """How long a claimed job stays invisible before the reaper assumes the worker
    died and makes it claimable again. Too short = spurious double-runs of slow
    jobs; too long = slow recovery from a crashed worker."""

    reaper_interval_secs: int = Field(default=10, ge=1)
    gauge_interval_secs: int = Field(default=10, ge=1)

    # --- Retries (V3) ---

    default_max_attempts: int = Field(default=5, ge=1)

    # --- Job execution ---

    job_log_dir: Path = Path("logs")
    """Base directory for per-attempt `exec`/`shell` output files."""

    @property
    def poll_interval(self) -> float:
        """Idle poll fallback, in seconds."""
        return self.poll_interval_ms / 1000.0

    @property
    def visibility_timeout(self) -> float:
        """Lease length, in seconds."""
        return float(self.visibility_timeout_secs)

    @property
    def token(self) -> str | None:
        """The configured enqueue token, or `None` when auth is disabled."""
        return self.enqueue_token or None
