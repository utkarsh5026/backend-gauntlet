"""V2 — Sliding window, built from scratch.

The motivating bug: a *fixed*-window counter ("<= limit per minute, reset on the
minute") lets through a **2x burst** straddling a boundary — `limit` requests at
11:00:59 and `limit` more at 11:01:00, which is `2 * limit` inside one real
minute. The window is fixed to the clock, not to the traffic.

Two classic fixes, with a genuine tradeoff between them:

  * **Sliding window log** — keep every request's timestamp, drop the ones older
    than `window`, count what's left. Exact, but memory grows with traffic: a
    key doing 10k req/s over a 60s window holds 600k timestamps. In Python that
    is a `collections.deque` you `popleft()` from the front while the head is
    expired — O(1) per eviction, but the *storage* is the problem, not the time.
  * **Sliding window counter** (what you implement here) — keep only the current
    and previous fixed-window counts, and weight the previous one by how much of
    it still overlaps `now`. Two integers per key regardless of traffic, at the
    cost of assuming the previous window's requests were spread evenly through
    it. That assumption is where the error bound the SPEC asks you to document
    comes from.

Scaffold state: the counter constructs; the first `try_acquire` raises.
"""

from __future__ import annotations

from .limiter import Decision, LimitConfig

__all__ = ["SlidingWindowCounter"]


class SlidingWindowCounter:
    """A sliding-window-counter limiter for a single key.

    O(1) memory: `_current_count` and `_previous_count` are the entire state, no
    matter how much traffic flows through. That is the property to protect —
    if you find yourself appending to a list, you have built the log instead.
    """

    __slots__ = ("_current_count", "_current_start", "_limit", "_previous_count", "_window")

    def __init__(self, config: LimitConfig, now: float) -> None:
        # A window derived from the rate: `burst` events per `window` seconds is
        # the same budget the token bucket expresses as capacity + refill rate.
        self._window = config.burst / config.rate_per_sec if config.rate_per_sec > 0 else 1.0
        self._limit = config.burst
        self._current_start = now
        self._current_count = 0
        self._previous_count = 0

    @property
    def window(self) -> float:
        """Window length in seconds."""
        return self._window

    def try_acquire(self, cost: int, now: float) -> Decision:
        """Account for a request costing `cost`, as of monotonic `now`."""
        # TODO(V2): the sliding-window-counter decision.
        #   1. Roll the windows forward. How many whole windows have elapsed
        #      since `_current_start`? Zero means stay put; one means current
        #      becomes previous and current resets; two or more means the
        #      previous window is entirely in the past, so it contributes
        #      nothing — zero it, don't carry a stale count forward. That
        #      "skipped a whole window" case is the one tests catch late.
        #   2. Estimate the sliding count:
        #        weight   = how much of the previous window still overlaps `now`
        #                   (a fraction in [0, 1) — derive it from how far into
        #                   the current window you are)
        #        estimate = _previous_count * weight + _current_count
        #   3. If `estimate + cost <= _limit`, add `cost` and allow. Otherwise
        #      deny, with a `retry_after` derived from how long until enough of
        #      the previous window has rolled off to make room.
        #
        # `estimate` is a float and `_limit` is an int — be deliberate about the
        # comparison. Rounding the estimate down is *more* permissive than the
        # exact log; rounding up is stricter. Which one you choose is the error
        # bound you have to document.
        raise NotImplementedError("V2: sliding-window-counter decision")

    def peek(self, now: float) -> Decision:
        """Report the current estimate without consuming budget."""
        # TODO(V2): roll the windows forward, compute the estimate, and report
        # it — but do not add `cost` to any counter.
        raise NotImplementedError("V2: non-consuming peek at the window estimate")
