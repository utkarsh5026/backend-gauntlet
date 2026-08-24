"""The vocabulary every limiter speaks.

All three verticals — `token_bucket` (V1), `sliding_window` (V2) and
`redis_limiter` (V3) — take a `LimitConfig` and hand back a `Decision`. Pinning
that contract here is what lets you swap the algorithm from an env var without
the gRPC layer noticing.

**On time.** Every limiter in this project takes `now` as a `float` of
**monotonic** seconds (`time.monotonic()`), injected by the caller rather than
read inside the algorithm. Two reasons, and both are graded:

  * `time.time()` is a wall clock. NTP can step it backwards, and a backwards
    step in a refill calculation either manufactures budget or freezes a bucket
    until the clock catches up. `time.monotonic()` cannot go backwards.
  * Injecting `now` means a test can advance time by a millisecond or an hour
    without sleeping — which is the only way to test refill honestly.

V3 is the exception on the *source*, not the shape: the timestamp there has to
come from Redis (`redis.call("TIME")`), because N instances with N slightly
different clocks must agree on when "now" is.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = ["Algorithm", "Decision", "LimitConfig", "LocalLimiter"]


class Algorithm(StrEnum):
    """Which algorithm the distributed limiter enforces.

    A `StrEnum` so `Algorithm("token_bucket")` parses straight out of the
    environment — pydantic does the coercion and rejects a typo at startup with
    a message naming the variable — and so the value round-trips into log fields
    and `/metrics` labels as plain text.
    """

    TOKEN_BUCKET = "token_bucket"
    SLIDING_WINDOW = "sliding_window"


@dataclass(frozen=True, slots=True)
class LimitConfig:
    """The budget configured for a key."""

    rate_per_sec: float
    """Sustained refill rate in tokens/second — the long-run average."""

    burst: int
    """Maximum burst: the bucket capacity, or the sliding window's ceiling."""


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of one rate-limit decision.

    Frozen because a decision is a fact about a moment, not a mutable record: an
    accidental `decision.allowed = True` three frames later would be a security
    bug that type-checks. Construct a new one instead.
    """

    allowed: bool
    remaining: int
    """Budget left after this decision."""
    limit: int
    """The ceiling that applied."""
    retry_after: float = 0.0
    """Seconds to wait before a retry could succeed. Zero when allowed."""

    @classmethod
    def allow(cls, remaining: int, limit: int) -> Decision:
        return cls(allowed=True, remaining=remaining, limit=limit)

    @classmethod
    def deny(cls, retry_after: float, limit: int) -> Decision:
        return cls(allowed=False, remaining=0, limit=limit, retry_after=retry_after)


class LocalLimiter(Protocol):
    """What V1 and V2 have in common: an in-process decision, no I/O.

    A `Protocol` rather than a base class — this is Python's structural typing.
    `TokenBucket` and `SlidingWindowCounter` satisfy it by *having* the method;
    neither has to inherit from anything or know this file exists. That is what
    makes the algorithm swappable without a shared hierarchy, and it is the
    idiom worth reaching for whenever you would have written an interface.
    """

    def try_acquire(self, cost: int, now: float) -> Decision:
        """Account for a request costing `cost` tokens, as of monotonic `now`."""
        ...
