"""Typed settings for the broker.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` starts a broker with no setup at all — the
filesystem is the only dependency. Types here are the parser: declaring
`port: int` gets you the env lookup, the coercion, the default, and a startup
error naming the offending variable.
"""

from __future__ import annotations

from pathlib import Path

from common_config import BaseConfig
from pydantic import Field

from .log import LogConfig

__all__ = ["Settings"]

# The sparse index (V2) stores byte positions as unsigned 32-bit values relative
# to the segment start, so a segment can never be allowed past 4 GiB.
MAX_SEGMENT_BYTES = 4 * 1024**3 - 1


class Settings(BaseConfig):
    # --- HTTP ---
    port: int = 9092
    """HTTP API port. 9092 is the Kafka broker-port convention."""

    log_level: str = "info"

    # --- storage ---
    data_dir: Path = Path("./data")
    """Where the broker lives. It creates `topics/<topic>/<partition>/` trees of
    segment (`.log`) + index (`.index`) files under here, and `groups/` for
    committed offsets."""

    segment_bytes: int = Field(default=64 * 1024 * 1024, gt=0, le=MAX_SEGMENT_BYTES)
    """Roll to a new segment once the active one exceeds this many bytes (V1).
    Kept small here so a modest test produces multiple segments; Kafka's default
    is 1 GiB. The upper bound is a V2 constraint, not a taste — see
    `MAX_SEGMENT_BYTES`."""

    index_interval_bytes: int = Field(default=4096, gt=0)
    """Write a sparse index entry about every this-many bytes of log (V2).
    Smaller = faster seeks, more memory; larger = the reverse. Kafka's default is
    4 KiB."""

    default_partitions: int = Field(default=3, ge=1)
    """Partition count for a topic created without an explicit one (V3)."""

    max_record_bytes: int = Field(default=1024 * 1024, gt=0)
    """Hard cap on a single record's value, enforced on produce so one client
    cannot stream the broker out of disk. Kafka's default is ~1 MiB."""

    # --- auth (horizontal TODO) ---
    write_api_key: str | None = None
    """Credential gating produce + topic creation. `None` leaves the broker open,
    which is fine on localhost and a graded failure anywhere else."""

    @property
    def log_config(self) -> LogConfig:
        """The per-log tunables, bundled for `Log`/`Segment`."""
        return LogConfig(
            segment_bytes=self.segment_bytes,
            index_interval_bytes=self.index_interval_bytes,
        )
