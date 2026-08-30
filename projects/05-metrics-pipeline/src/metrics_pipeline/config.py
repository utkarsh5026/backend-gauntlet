"""Typed settings for the pipeline.

Every field maps to a variable in `.env.example`, and every one has a working
default so a bare `make run` starts the ingest API against the compose
dependencies. Types here are the parser: declaring `port: int` gets the env
lookup, the coercion, the default, and a startup error naming the offending
variable.

The knobs are grouped by the vertical they belong to, because most of them are
*the* tuning surface of that vertical — the window/grace pair is V2's flush
contract, and the batch size/delay pair is V3's whole size-or-time trigger.
"""

from __future__ import annotations

from datetime import timedelta

from common_config import BaseConfig
from pydantic import Field, SecretStr

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- HTTP API (ingest + live feed + query) ---
    port: int = 8080
    log_level: str = "info"

    # --- Broker: NATS JetStream (the durable log between ingest and consumer) ---
    nats_url: str = "nats://localhost:4205"
    stream_name: str = "METRICS"
    """JetStream stream backing the raw-metrics subject."""
    broker_connect_timeout: float = Field(default=5.0, gt=0)
    """How long to wait for the broker at startup before booting degraded.
    Bounded on purpose — see the note in `broker.connect`."""

    # --- Store: ClickHouse (the queryable home for rolled-up metrics) ---
    clickhouse_url: str = "http://localhost:8105"
    clickhouse_db: str = "default"
    clickhouse_user: str = "default"
    clickhouse_password: SecretStr = SecretStr("")
    """`SecretStr` so a stray `log.info(settings=cfg)` prints `**********`
    instead of the password. The repo rule is "never log secrets"; this makes
    the rule structural rather than a thing you have to remember."""
    rollup_table: str = "metrics_rollup"

    # --- SSE live feed (V4) ---
    sse_capacity: int = Field(default=1024, gt=0)
    """How far one dashboard may fall behind before it is shed. Bounded on
    purpose: a slow live client is DROPPED, never allowed to back-pressure the
    pipeline (SPEC V4)."""

    # --- Consumer pipeline (rollup -> sink) ---
    run_consumer: bool = False
    """Off by default so the bare scaffold serves the ingest API without hitting
    a vertical's NotImplementedError. Flip it on once V1/V2 work."""
    durable_name: str = "rollup-consumer"
    """Durable consumer name — remembers its offset across restarts."""
    fetch_batch: int = Field(default=256, gt=0)
    """Messages pulled from the broker per fetch. This is the broker->rollup
    prefetch bound the SPEC asks you to tune *together* with the batch below."""

    # --- Rollup windows (V2) ---
    window_secs: int = Field(default=60, gt=0)
    """Tumbling-window width."""
    grace_secs: int = Field(default=10, ge=0)
    """Watermark grace: wait this long past a window's end for late points."""
    flush_interval_ms: int = Field(default=1000, gt=0)
    """How often to close windows + time-flush the sink."""

    # --- Batched sink (V3) ---
    batch_max_rows: int = Field(default=10_000, gt=0)
    """Size trigger: flush once this many rows are buffered."""
    batch_max_delay_ms: int = Field(default=1000, gt=0)
    """Time trigger: latency ceiling for an idle pipeline."""

    @property
    def window(self) -> timedelta:
        return timedelta(seconds=self.window_secs)

    @property
    def grace(self) -> timedelta:
        return timedelta(seconds=self.grace_secs)

    @property
    def flush_interval(self) -> float:
        """Seconds, as asyncio wants them."""
        return self.flush_interval_ms / 1000

    @property
    def batch_max_delay(self) -> float:
        return self.batch_max_delay_ms / 1000
