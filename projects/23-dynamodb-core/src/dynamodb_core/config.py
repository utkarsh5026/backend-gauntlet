"""Typed settings for one node.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` starts an empty node.
"""

from __future__ import annotations

from pathlib import Path

from common_config import BaseConfig
from pydantic import Field

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- server ---
    port: int = 8000
    log_level: str = "info"

    # --- storage (V1) ---
    data_dir: Path = Path("./data")
    max_item_bytes: int = Field(default=409_600, gt=0)

    # --- capacity (V4) ---
    default_read_capacity: int = Field(default=1000, gt=0)
    default_write_capacity: int = Field(default=1000, gt=0)
    # The per-partition ceiling. This is the number that makes a hot key throttle
    # while the table as a whole looks idle — the boss fight lives here.
    partition_read_capacity: int = Field(default=3000, gt=0)
    partition_write_capacity: int = Field(default=1000, gt=0)
    burst_seconds: float = Field(default=300.0, gt=0)

    # --- streams (V5) ---
    stream_retention_hours: float = Field(default=24.0, gt=0)
    stream_buffer_size: int = Field(default=10_000, gt=0)

    @property
    def stream_retention_seconds(self) -> float:
        return self.stream_retention_hours * 3600
