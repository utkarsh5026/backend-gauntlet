"""V4 — Concurrency & scaling: concurrency is the unit of capacity.

Lambda does not scale on CPU. It scales on **concurrent executions**, and every
limit that will ever page you is denominated in them. The identity worth carrying
around in your head:

    concurrency = requests_per_second × average_duration_seconds

which is why a latency regression becomes a *throttling* incident: nothing about
your traffic changed, but each request now holds a slot for twice as long, so you
need twice as many. It is also why the fix for a throttle is sometimes to make the
function faster rather than to raise the limit.

Three limits interact here, and the SPEC grades on all three:

  * the **account limit** — shared by every function on the node;
  * **reserved** concurrency — a slice carved out of the account limit that both
    *guarantees* (nobody else can take it) and *caps* (you cannot exceed it);
  * **provisioned** concurrency — environments kept initialised and warm, so their
    invocations skip the cold start entirely, at a price.

And the failure mode that makes it real: a burst beyond the ceiling is **throttled,
not queued**, and a function with no reservation can be starved to zero by a noisy
neighbour that has none either.

Scaffold state: the limits are modelled; the admission decision and the scale-up
policy raise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from .config import Settings
from .models import FunctionConfig, FunctionName

__all__ = ["ConcurrencyGovernor", "ConcurrencySnapshot", "Lease"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class ConcurrencySnapshot:
    """What the governor believes right now — the source of the metrics.

    `in_flight` returning to exactly 0 after a burst is an explicit boss-fight
    criterion, so this is worth being able to read at any moment.
    """

    account_limit: int
    account_in_flight: int
    per_function: dict[FunctionName, int] = field(default_factory=dict[FunctionName, int])
    throttled_total: int = 0

    @property
    def account_available(self) -> int:
        return max(0, self.account_limit - self.account_in_flight)


class Lease:
    """A held concurrency slot, released when the invocation ends.

    A context manager on purpose: the boss fight requires that in-flight returns to
    zero after *every* phase, and the ways a slot leaks are all the paths that
    forget to release — an exception, a timeout, a client disconnect, a cancelled
    task. `async with` is the only structure that closes all four at once.
    """

    def __init__(self, governor: ConcurrencyGovernor, function_name: FunctionName) -> None:
        self._governor = governor
        self._function_name = function_name

    async def __aenter__(self) -> Lease:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._governor.release(self._function_name)


class ConcurrencyGovernor:
    """Admits or throttles invocations, and paces how fast the fleet may grow.

    Two separate decisions live here and it is worth not conflating them:

      * **admission** — is there a free slot? If not, throttle (never queue).
      * **scale-up pacing** — a slot is free, but does creating a *new environment*
        for it exceed the rate at which we allow the fleet to grow? This is what
        makes a cold front roll through gradually instead of all at once, and it is
        the number the boss fight measures the accepted-vs-throttled curve against.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._account_limit = settings.account_concurrency_limit
        self._in_flight: dict[FunctionName, int] = {}
        self._throttled_total = 0
        # TODO(V4): the reservation bookkeeping and the scale-up pacer.
        #
        # Reservations are the subtle part. A reservation is carved OUT of the
        # account limit, so the pool available to unreserved functions is
        # `account_limit - sum(reservations)`. Get that subtraction wrong and
        # either reservations do not actually guarantee anything, or the account
        # limit is silently over-subscribed. Both fail V4, and only one of them
        # fails loudly.
        #
        # For the pacer, a token bucket refilled at `scale_up_rate_per_second` with
        # a `burst_concurrency` bank is the natural shape. Use `time.monotonic()`,
        # never `time.time()` — a clock adjustment must not mint or destroy
        # capacity.

    def try_acquire(self, function: FunctionConfig) -> Lease | None:
        """Take a slot, or return None to signal a throttle.

        Returning `None` rather than raising is deliberate: the caller decides what
        a throttle means on its path. On the sync path it is a 429 to the caller;
        on the async path (V5) it is a retry, not an error.
        """
        # TODO(V4): the admission decision, in order:
        #
        #   1. Does this function have a reservation? If so, check against IT — and
        #      the reservation is a cap as well as a floor.
        #   2. If not, check against the UNRESERVED pool, not the raw account
        #      limit. This is the step that makes a noisy neighbour starve its
        #      peers exactly as much as it should and no more.
        #   3. Count the throttle when you refuse. The SPEC requires throttles to
        #      be counted separately from errors — a throttle in your error rate is
        #      how a capacity problem gets misdiagnosed as a bug.
        raise NotImplementedError("V4: admit against reservation or unreserved pool, else throttle")

    def release(self, function_name: FunctionName) -> None:
        """Give a slot back. Must be exact — a leaked slot is a permanent throttle."""
        # TODO(V4): decrement. Be paranoid: this runs on every exit path including
        # cancellation, and double-releasing is as bad as never releasing (it
        # over-admits and quietly breaks the account limit).
        raise NotImplementedError("V4: return the slot to its pool")

    async def await_scale_slot(self) -> None:
        """Wait until the fleet is allowed to grow by one environment.

        Called only on the cold path — reusing a warm environment costs no scale
        budget, which is exactly why a warm fleet absorbs a spike that a cold one
        cannot.
        """
        # TODO(V4): consume from the scale-up bucket, waiting if it is empty.
        # Decide and document what happens when the wait would exceed the
        # invocation's deadline — waiting past it is strictly worse than throttling
        # immediately, because the caller pays the full timeout to be told no.
        raise NotImplementedError("V4: pace environment creation against the scale-up rate")

    def set_reserved(self, function_name: FunctionName, reserved: int | None) -> None:
        """Reserve (or un-reserve) concurrency for a function."""
        # TODO(V4): validate before accepting. Reserving more than the account has
        # left un-reserved must be REFUSED — the real service refuses it too, and
        # accepting it is how you build a limit that does not hold.
        raise NotImplementedError("V4: validate and record the reservation")

    def snapshot(self) -> ConcurrencySnapshot:
        """Current state, for `/metrics` and for tests asserting no leaked slots."""
        return ConcurrencySnapshot(
            account_limit=self._account_limit,
            account_in_flight=sum(self._in_flight.values()),
            per_function=dict(self._in_flight),
            throttled_total=self._throttled_total,
        )
