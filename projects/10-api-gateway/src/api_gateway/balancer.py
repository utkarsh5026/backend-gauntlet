"""V3 — Load balancing across a backend pool.

Module: `src/api_gateway/balancer.py`. A route points at a *pool*; something has
to pick one backend per request. Round-robin is the floor: it is fair in the
number of requests handed out and completely blind to what happens to them, so
the moment backend #3 starts taking 400 ms instead of 4 ms, round-robin keeps
feeding it exactly one third of your traffic. Naive random is no better — it just
produces unlucky hot spots instead of predictable ones.

The scaffold gives you the pool structure, the per-backend bookkeeping the
policies read (in-flight count, EWMA latency, circuit breaker), and the policy
enum. `Balancer.pick` is yours. Build round-robin, then least-connections, then
P2C + EWMA, and prove with a bench that they diverge under skewed load — the
"prove" half is the actual assignment; all three are ten lines each.

## What "cheap on the hot path" means in Python

The SPEC asks for selection that doesn't serialize every request behind one lock.
In Rust that meant atomics. Here it means something simpler and easy to get wrong
in the other direction: **do not put an `asyncio.Lock` in `pick()`**. This gateway
runs one event loop, so a run of plain attribute reads and writes with no `await`
between them is already indivisible — nothing else can observe a half-updated
cursor. Acquiring a lock would add a scheduler round-trip to every single request
to protect against a race that cannot happen. Keep `pick()` a plain synchronous
function and the problem stays solved.

The corollary is the trap: the *accounting* around a request does span an
`await`. `in_flight` is incremented before the upstream call and decremented
after, and in between the coroutine can be cancelled (client disconnects, deadline
fires) or raise. A decrement that only runs on the success path leaks the counter
upward forever, and since least-connections and P2C read that counter, a leak
quietly and permanently steers traffic *away* from a perfectly healthy backend.
Wrap it in `try` / `finally` — `finally` runs on cancellation too, which is
exactly why it is the right tool here.
"""

from __future__ import annotations

from enum import StrEnum

from .health import CircuitBreaker

__all__ = ["Backend", "Balancer", "LbPolicy"]


class LbPolicy(StrEnum):
    """Load-balancing policy over a pool. Parsed from the route config's `lb`."""

    ROUND_ROBIN = "round_robin"
    """Hand out backends in rotation. Simple, ignores load — the floor."""

    LEAST_CONN = "least_conn"
    """Route to the backend with the fewest in-flight requests."""

    P2C = "p2c"
    """Power of two choices: sample two at random, pick the less-loaded.

    The cheap approximation that beats both. Pure random has no load signal;
    least-connections over a large pool has to look at everything and, worse,
    *every* balancer picks the same "least loaded" backend at the same instant and
    stampedes it. Two random samples cost O(1), and the odds that both are the
    unlucky one are small enough that the herd never forms."""


class Backend:
    """One upstream backend and its live state.

    The counters here are the *signals* the balancer (V3) and the health layer
    (V4) read. None of them update themselves: wiring the accounting into the
    request path is part of V1/V3, and until you do, least-connections and P2C are
    reading zeros and behaving exactly like round-robin — which is a confusing way
    to discover you skipped a step, so do the accounting first.
    """

    __slots__ = ("addr", "circuit", "ewma_seconds", "in_flight")

    def __init__(self, addr: str, circuit: CircuitBreaker | None = None) -> None:
        self.addr = addr
        """`host:port`, used to build the upstream URL."""

        self.in_flight = 0
        """Requests currently in flight to this backend (least-conn / P2C signal).

        Maintained by the proxy path: increment before dispatch, decrement in a
        `finally`. See the module docstring on why `finally` and not "after"."""

        self.ewma_seconds = 0.0
        """Exponentially-weighted moving average of recent upstream latency, in
        seconds (the P2C tie-break signal).

        Seconds, as a float, because that is the unit every asyncio and httpx
        timing API already speaks — the Rust version stored microseconds as an
        integer only because it had to keep it in an atomic."""

        self.circuit = circuit if circuit is not None else CircuitBreaker()
        """Per-backend circuit breaker (V4): when open, this backend is skipped."""

    def is_available(self) -> bool:
        """Is this backend eligible to receive a request (circuit not open)? (V4)"""
        return self.circuit.allow()

    def __repr__(self) -> str:
        return f"Backend({self.addr!r}, in_flight={self.in_flight})"


class Balancer:
    """The pool + policy for one route's upstream."""

    def __init__(self, policy: LbPolicy, backends: list[Backend]) -> None:
        self.policy = policy
        self.backends = backends
        self._cursor = 0
        """Rotation cursor for round-robin. A plain int: see the module docstring."""

    def pick(self) -> Backend | None:
        """Pick a backend for the next request, or `None` if the whole pool is
        unavailable (-> 503, distinct from V2's 404 for *no route at all*).

        TODO(V3): implement per `self.policy` —
          * `ROUND_ROBIN`: advance `self._cursor` modulo the pool size. The subtle
            part is skipping unavailable backends *without* letting the skip
            distort the rotation or spin forever when the pool is entirely down.
          * `LEAST_CONN`: the eligible backend with the smallest `in_flight`.
            `min(eligible, key=...)` is the whole implementation; the interesting
            question is what you break ties on, because a fresh pool is all zeros
            and an arbitrary tie-break silently becomes "always the first one".
          * `P2C`: two distinct random samples from the eligible set
            (`random.sample` gives you distinctness for free), then take the better
            on `in_flight`, breaking ties on `ewma_seconds`. With a pool of one or
            two, P2C degenerates — decide what it does then rather than letting
            `random.sample` raise.

        In every policy: skip any backend whose `is_available()` is `False`, and
        keep the function synchronous and lock-free.
        """
        raise NotImplementedError("V3: choose a healthy backend per the LB policy")
