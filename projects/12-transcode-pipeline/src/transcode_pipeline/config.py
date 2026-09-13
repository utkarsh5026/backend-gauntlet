"""Typed settings for the pipeline.

Every field maps to a variable in `.env.example`, and every one has a working
default, so `make run` boots against this project's docker-compose Postgres with
no edits. The type annotation *is* the parser: `lease_secs: float` gets the env
lookup, the coercion, the default, and a startup error that names the variable.

The intervals keep their `.env.example` names and units (`SCHEDULER_INTERVAL_MS`,
`POLL_INTERVAL_MS`), so one `.env` reads the same everywhere; the `*_secs`
properties convert once, here, because every asyncio API takes seconds.

The default ladder is not an env var. It is the server's answer to a job that
doesn't pin its own: three rungs from 1080p down.
"""

from __future__ import annotations

from pathlib import Path

from common_config import BaseConfig
from pydantic import Field

from .models import Rendition

__all__ = ["DEFAULT_LADDER", "Settings"]

DEFAULT_LADDER: tuple[Rendition, ...] = (
    Rendition(name="1080p", height=1080, v_bitrate_kbps=5000, a_bitrate_kbps=128),
    Rendition(name="720p", height=720, v_bitrate_kbps=2800, a_bitrate_kbps=128),
    Rendition(name="480p", height=480, v_bitrate_kbps=1400, a_bitrate_kbps=96),
)
"""The ladder used when a job doesn't specify one. A tuple of frozen models, so no
request can edit the rungs every other job shares."""


class Settings(BaseConfig):
    # --- HTTP ---
    port: int = Field(default=8080, gt=0, lt=65536)
    """The control-plane API: submit + inspect jobs."""
    log_level: str = "info"

    # --- Postgres: the durable DAG store ---
    database_url: str = "postgres://transcode:transcode@localhost:5412/transcode"
    db_max_connections: int = Field(default=20, gt=0)
    """Per process. Each worker holds a connection for the length of every store
    call, the scheduler holds one per pass, and HTTP handlers share what's left —
    see the Python checklist on sizing this *together* with `worker_concurrency`
    and the number of worker processes."""

    # --- Artifacts + the codec toolbox ---
    work_dir: Path = Path("./work")
    """Root of the artifact store. Sources, chunk outputs and finished renditions
    all live under here; nothing may escape it."""
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # --- Chunking (V1) ---
    target_chunk_secs: float = Field(default=6.0, gt=0)
    """Desired chunk length. A goal, not a law: the keyframe boundary wins (V1), so
    real chunks cluster around it."""

    # --- Coordinator: scheduler + worker pool ---
    run_workers: bool = False
    """Off by default so the bare scaffold serves the control-plane API without the
    scheduler's first tick reaching an unbuilt V2 store method."""
    worker_concurrency: int = Field(default=4, gt=0)
    """Workers in this process — and therefore the cap on concurrent ffmpeg encodes.
    The pool size is the backpressure (V3)."""
    scheduler_interval_ms: int = Field(default=500, gt=0)
    """How often PENDING→READY promotion and lease reaping run."""
    poll_interval_ms: int = Field(default=500, gt=0)
    """How often an idle worker re-checks for a READY task."""

    # --- Leases / retries (V3) ---
    lease_secs: float = Field(default=120.0, gt=0)
    """How long a claimed task stays leased before the reaper assumes its worker
    died. Too short and a slow transcode gets double-run; too long and a crashed
    worker's chunk stalls its rendition's stitch for the whole window."""
    max_attempts: int = Field(default=3, gt=0)
    """Transcode attempts before a task is dead-lettered."""

    @property
    def scheduler_interval_secs(self) -> float:
        return self.scheduler_interval_ms / 1000

    @property
    def poll_interval_secs(self) -> float:
        return self.poll_interval_ms / 1000

    @property
    def default_ladder(self) -> tuple[Rendition, ...]:
        return DEFAULT_LADDER
