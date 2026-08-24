"""V1 — Token bucket, built from scratch (in-process, single node).

The algorithm in its purest form: a small class with no I/O, which you can
unit-test exhaustively before Redis is anywhere near it. Get this right and V3
becomes "the same arithmetic, but atomic and written in Lua".

The defining trick is **lazy refill — compute on read.** There is no background
task ticking tokens into a million buckets; a task per key would cost more than
the service it protects. Instead every `try_acquire` asks "how many tokens
*would* have accrued since I last looked?", caps that at capacity, and only then
tries to spend.

**The Python trap.** `tokens` is a `float`, and floats are why "we allow 10/sec"
quietly becomes 9.9997/sec after a few million requests. Two specific hazards:

  * Adding `elapsed * rate` a few million times accumulates representation
    error. Whether that matters depends on whether you also *clamp* at capacity
    — think about which operations lose budget and which manufacture it.
  * `int(x)` truncates toward zero and `round()` uses banker's rounding. Neither
    is obviously right here. Decide what you are rounding, and where — the SPEC
    property-tests that the long-run rate does not drift.

Scaffold state: the bucket constructs; the first `try_acquire` raises. That is
your worklist.
"""

from __future__ import annotations

from .limiter import Decision, LimitConfig

__all__ = ["TokenBucket"]


class TokenBucket:
    """One key's token bucket. Cheap — expect one per active key.

    A fresh bucket starts **full**, so a caller that has been quiet may burst up
    to `capacity` immediately. That is the point of having a burst knob at all.
    """

    __slots__ = ("_capacity", "_last_refill", "_refill_per_sec", "_tokens")

    def __init__(self, config: LimitConfig, now: float) -> None:
        self._capacity = float(config.burst)
        self._refill_per_sec = config.rate_per_sec
        self._tokens = float(config.burst)
        self._last_refill = now

    @property
    def tokens(self) -> float:
        """Tokens as of the last refill — for tests and `/metrics`, not decisions.

        Deliberately *not* refreshed on read: a property that silently advances
        time would make the drift property test meaningless.
        """
        return self._tokens

    def try_acquire(self, cost: int, now: float) -> Decision:
        """Account for a request costing `cost` tokens, as of monotonic `now`.

        `now` is a parameter rather than a `time.monotonic()` call inside, so a
        test can drive a thousand refills across a simulated hour instantly.
        """
        # TODO(V1): lazy refill, then acquire.
        #   1. Refill: add `(now - self._last_refill) * self._refill_per_sec`
        #      tokens, clamp to `self._capacity` (`min` is the whole clamp), and
        #      advance `self._last_refill` to `now`.
        #   2. If there are at least `cost` tokens: deduct and
        #      `return Decision.allow(...)`.
        #   3. Otherwise work out how long until `cost` tokens exist — a
        #      division, and think about what it means when the rate is 0 — and
        #      `return Decision.deny(retry_after, ...)`.
        #
        # Keep `self._tokens` a float the whole way through. `remaining` on the
        # Decision is an int because it goes on the wire; converting there (not
        # in the state) is what stops rounding from eating the budget.
        raise NotImplementedError("V1: lazy token-bucket refill + acquire")

    def peek(self, now: float) -> Decision:
        """Report state as of `now` **without** consuming budget.

        Backs the `Peek` RPC. Note the subtlety: refilling is not "consuming",
        so a peek may legitimately advance `_last_refill` — but it must never
        change the answer a concurrent `try_acquire` would have given.
        """
        # TODO(V1): refill (or compute the refilled value without storing it —
        # decide which, and say why) and report `remaining` and a `retry_after`
        # for the caller's "am I about to be limited?" probe.
        raise NotImplementedError("V1: non-consuming peek at bucket state")
