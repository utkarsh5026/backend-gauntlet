"""Typed settings for the rate limiter.

Every field maps to a variable in `.env.example`, and every one has a working
default, so a bare `make run` against the compose Redis starts a usable service.
The annotation is the parser: `port: int = 50051` gets you the env lookup, the
coercion, the default, and a startup error naming the offending variable.
"""

from __future__ import annotations

from common_config import BaseConfig
from pydantic import Field

from .limiter import Algorithm, LimitConfig

__all__ = ["Settings"]


class Settings(BaseConfig):
    # --- serving ---
    port: int = 50051
    """gRPC port. 50051 is the conventional gRPC dev port."""

    metrics_port: int = 9102
    """HTTP port for `/healthz` + `/metrics` (repo convention: 9100 -> 91NN)."""

    log_level: str = "info"

    # --- the default limit applied to every key (per-tier config comes later) ---
    rate_per_sec: float = Field(default=10.0, gt=0)
    """Sustained refill rate in tokens/second."""

    burst: int = Field(default=20, gt=0)
    """Bucket capacity / window ceiling — the most a caller may spend at once."""

    algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    """Which limiter the distributed path enforces."""

    # --- shared state ---
    redis_url: str = "redis://localhost:6302/0"
    """Matches docker-compose.yml's published port."""

    redis_max_connections: int = Field(default=64, gt=0)
    """Cap on the connection pool.

    A graded knob, not a magic number: this and the server's concurrency limit
    are two halves of one decision. An unbounded pool turns a slow Redis into
    unbounded memory and file descriptors; too small a pool turns it into a
    queue nobody can see. Size them together and record the reasoning in
    `docs/02-design.md`.
    """

    key_ttl_seconds: int = Field(default=3600, gt=0)
    """How long an idle key's state survives in Redis before self-evicting.

    Must comfortably exceed the time it takes a bucket to refill completely,
    or you hand out a fresh full bucket to someone you just throttled.
    """

    # --- failure policy ---
    fail_open: bool = True
    """Redis unreachable: True allows (availability), False denies (protection)."""

    max_concurrent_rpcs: int = Field(default=1000, gt=0)
    """Server-side ceiling on in-flight RPCs — backpressure instead of an OOM."""

    @property
    def limit(self) -> LimitConfig:
        """The configured budget, in the limiters' own vocabulary."""
        return LimitConfig(rate_per_sec=self.rate_per_sec, burst=self.burst)
