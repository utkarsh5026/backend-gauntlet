"""Security - per-API-key rate limiting for the write path (`POST /api/links`).

Project 01 only needs a *taste* of abuse protection: building a token bucket, a
sliding window, and an atomic Redis+Lua decision from scratch is the entire
vertical of project 02. So this is a compact in-process **GCRA** limiter - the
same algorithm the Rust version got from `tower_governor`.

**Why GCRA and not a token bucket.** A bucket stores a level and a last-refill
time, and every check has to compute the refill. GCRA stores *one* number per
key: the theoretical arrival time (TAT) of the next conforming request. A check
is a comparison and an addition, with no refill loop and nothing to drift. The
two knobs map onto it directly - `PERIOD` is the emission interval (one request
per period, sustained) and `BURST` sets how far ahead of the TAT a caller may
run before being told to wait.

Bucketed on the **API key**, not the client IP, so one noisy client cannot
exhaust the budget for everyone else behind the same NAT or proxy. Scoped to the
write path: the stats endpoint and the public redirect are never throttled here.

In-process state, which means the budget is per replica: with N instances behind
a load balancer the effective limit is N times this one. Making the decision
shared and atomic across replicas is exactly what project 02 is for.
"""

from __future__ import annotations

import math
import time
from typing import Final

from .errors import RateLimited

__all__ = ["BURST", "PERIOD_SECS", "Decision", "RateLimiter", "enforce_rate_limit"]

PERIOD_SECS: Final[float] = 0.2
"""Emission interval: one request per 200ms sustained, i.e. ~5 rps per key."""

BURST: Final[int] = 10
"""Largest burst a single key may spend before it has to wait for refills."""

_PRUNE_THRESHOLD: Final[int] = 10_000


class Decision:
    """The outcome of one check, plus the headers a client needs to back off."""

    __slots__ = ("allowed", "limit", "remaining", "retry_after")

    def __init__(self, *, allowed: bool, remaining: int, limit: int, retry_after: float) -> None:
        self.allowed = allowed
        self.remaining = remaining
        self.limit = limit
        self.retry_after = retry_after

    def headers(self) -> dict[str, str]:
        """`retry-after` is whole seconds - that is what the HTTP header means -
        so a sub-second wait rounds *up* to 1 rather than down to "retry now"."""
        headers = {
            "x-ratelimit-limit": str(self.limit),
            "x-ratelimit-remaining": str(self.remaining),
        }
        if not self.allowed:
            headers["retry-after"] = str(max(1, math.ceil(self.retry_after)))
        return headers


class RateLimiter:
    """GCRA over an in-process dict of `key -> theoretical arrival time`.

    State is kept in **integer nanoseconds**, not float seconds. That is not
    fussiness: the TAT is built by repeatedly adding the period to itself, and in
    binary floating point `0.2` added five times is not `1.0`. The error lands
    exactly on the `>` that decides allow-vs-deny, so a caller whose budget has
    genuinely refilled gets a 429 anyway. Integers make the boundary exact and
    the arithmetic free.
    """

    __slots__ = ("_burst", "_period_ns", "_prune_at", "_tat_ns", "_tolerance_ns")

    def __init__(self, *, period: float = PERIOD_SECS, burst: int = BURST) -> None:
        if period <= 0 or burst < 1:
            raise ValueError("rate limiter needs a positive period and a burst of at least 1")
        self._period_ns = round(period * 1e9)
        self._burst = burst
        # How far ahead of "now" the TAT may sit while still admitting a request.
        # `burst - 1` periods, not `burst`, so a cold key admits exactly `burst`
        # requests back-to-back and denies the next one.
        self._tolerance_ns = (burst - 1) * self._period_ns
        self._tat_ns: dict[str, int] = {}
        self._prune_at = _PRUNE_THRESHOLD

    def check(self, key: str, *, now: float | None = None) -> Decision:
        """Charge one request against `key`.

        `now` is seconds, injectable so tests can drive elapsed time without
        sleeping; production uses the monotonic clock, which - unlike the wall
        clock the id generator needs - cannot be stepped backwards by an NTP
        correction. A rate limiter on a clock that can jump backwards would hand
        out free budget on every correction.
        """
        now_ns = time.monotonic_ns() if now is None else round(now * 1e9)
        tat_ns = max(self._tat_ns.get(key, now_ns), now_ns)

        if tat_ns - now_ns > self._tolerance_ns:
            return Decision(
                allowed=False,
                remaining=0,
                limit=self._burst,
                retry_after=(tat_ns - self._tolerance_ns - now_ns) / 1e9,
            )

        new_tat_ns = tat_ns + self._period_ns
        self._tat_ns[key] = new_tat_ns
        if len(self._tat_ns) > self._prune_at:
            self._prune(now_ns)

        remaining = max(0, (now_ns + self._tolerance_ns - new_tat_ns) // self._period_ns + 1)
        return Decision(allowed=True, remaining=remaining, limit=self._burst, retry_after=0.0)

    def _prune(self, now_ns: int) -> None:
        """Forget keys whose budget is fully refilled.

        Without this the dict is a slow memory leak keyed by attacker-supplied
        input: every API key ever seen would be remembered forever. A key at or
        behind `now` has no state left worth storing - it is indistinguishable
        from one that has never been seen.

        The next threshold is set to twice what survived, which is what keeps
        this amortized O(1). Pruning purely on `len > THRESHOLD` looks right and
        is quadratic: once a workload's *live* key count sits above the
        threshold, every subsequent request rebuilds the whole dict and finds
        almost nothing to drop.
        """
        self._tat_ns = {key: tat for key, tat in self._tat_ns.items() if tat > now_ns}
        self._prune_at = max(_PRUNE_THRESHOLD, 2 * len(self._tat_ns))


async def enforce_rate_limit(key: str, limiter: RateLimiter) -> dict[str, str]:
    """Charge `key` and raise if it is over budget. Returns headers for the
    success path so a conforming caller still learns its remaining budget."""
    decision = limiter.check(key)
    if not decision.allowed:
        raise RateLimited(headers=decision.headers())
    return decision.headers()
