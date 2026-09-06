"""V4 — Bandwidth estimation: figure out how much each subscriber's link can take.

Layer selection (V3) is only as good as the number handed to it: *how many
bits/sec can this subscriber actually receive right now?* Nobody tells the SFU —
it has to **estimate from feedback**, and the estimate has to move as the link
does (someone starts a download, the train enters a tunnel). This is the
receive-side congestion controller WebRTC calls GCC, and it runs **per
subscriber**.

Two signals feed it.

The **delay-based** signal is the subtle, early one. From transport-wide
congestion control feedback (TWCC) the estimator watches the *inter-arrival
delay gradient*: if packets sent 10 ms apart start arriving 15 ms apart, a queue
is building on the path **before** it overflows into loss. Ease off now, and the
subscriber never sees a dropped frame at all.

The **loss-based** signal is the blunt backstop. Sustained loss (from RTCP
receiver reports) means you are already over — cut hard. Near-zero loss means
probe up. A real GCC blends both, clamps to `[min, max]`, and the allocator then
divides that per-subscriber budget across the streams the subscriber receives,
which is exactly what `LayerSelector.set_budget` consumes.

How sophisticated you go is yours to choose — even a clean loss-based AIMD with
a delay-gradient trigger passes — but it must **converge, back off, and
recover**, and the control law you picked belongs in `docs/15-design.md`.

## NaN is the criterion, and Python will not stop you

"the estimate never goes negative, zero-stuck, unbounded, or NaN" reads like
defensive boilerplate. It is not: this is a controller driven by *arithmetic on
attacker-supplied timestamps*, and Python's floats are IEEE doubles with no
guard rails at all. Two samples with identical send times make the gradient a
`0/0` `ZeroDivisionError` — or, if you computed it with floats, a `nan`. And a
`nan` does not announce itself:

    >>> max(150_000, min(nan, 4_000_000))
    nan

Every comparison against a `nan` is `False`, so the idiomatic clamp passes it
straight through, `int(nan)` raises `ValueError` two layers away in the metrics
gauge, and the traceback names a line that is entirely innocent. Check
`math.isfinite` on anything derived from feedback *before* it reaches the clamp,
and guard the division rather than the result.

`fraction_lost` has the same shape from the other direction: it arrives as a
byte off the wire divided by 256, so a hostile sender can hand you 0.996 forever
and drive a multiplicative decrease into the floor. The clamp is what makes that
merely bad quality instead of a stuck session.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["ArrivalSample", "BandwidthEstimator", "split_budget"]

HEADROOM = 0.85
"""Fraction of a budget the allocator is willing to hand out.

Never 100%: a controller that allocates everything it believes in can only ever
discover the link got *worse*, because nothing is left to probe upward with. The
number is a policy decision — document yours.
"""


@dataclass(frozen=True, slots=True)
class ArrivalSample:
    """One transport-feedback sample.

    A packet the SFU sent at `sent_ms` that the subscriber reported receiving at
    `arrived_ms`. The *gradient* of `(arrived_ms - sent_ms)` across samples is
    the delay signal; the absolute clock offset between the two machines cancels
    out, which is why this works without any clock sync at all.

    Milliseconds as floats rather than integers: the SFU stamps `sent_ms` from
    its own `time.monotonic()`, and rounding that to a whole millisecond throws
    away precision at exactly the scale the gradient measures — a queue building
    by 3 ms across packets sent 10 ms apart is the signal, and integer
    milliseconds quantize it into noise.
    """

    sent_ms: float
    arrived_ms: float
    size_bytes: int


class BandwidthEstimator:
    """Per-subscriber downlink bandwidth estimator — a GCC-lite receive-side controller.

    Holds the current estimate and whatever smoothed state the control law needs
    (a delay-gradient trendline, a loss-driven AIMD rate), clamped to
    `[min_bps, max_bps]`.
    """

    __slots__ = ("_estimate_bps", "_state", "max_bps", "min_bps")

    def __init__(self, start_bps: int, min_bps: int, max_bps: int) -> None:
        self.min_bps = min_bps
        self.max_bps = max_bps
        self._estimate_bps = max(min_bps, min(start_bps, max_bps))

        # TODO(V4): the controller state. Keep it O(1) per sample — this runs on
        # the feedback hot path, per subscriber, and at 50 subscribers an O(n)
        # pass over a growing history is the difference between a controller and
        # a leak. Roughly: the previous sample's (sent, arrived) pair for the
        # inter-departure/inter-arrival difference, a smoothed trend (an
        # exponential moving average is one line and needs no window), and
        # whichever of the delay- and loss-based rates you keep separately so
        # the two can be min'd.
        self._state: None = None

    @property
    def estimate(self) -> int:
        """The current estimate in bits/sec, always within `[min_bps, max_bps]`."""
        return self._estimate_bps

    def on_transport_feedback(self, samples: Sequence[ArrivalSample]) -> int:
        """Update from a batch of transport feedback — the delay-based signal.

        TODO(V4): walk `samples` in order and compute the inter-arrival delay
        **gradient**: for consecutive samples, how `arrived_ms` spacing compares
        to `sent_ms` spacing. A rising gradient means a growing queue — over-use
        — so decrease the estimate multiplicatively. A flat or negative gradient
        means the path is clear, so increase it additively, probing.

        Clamp into `[min_bps, max_bps]` and store. An empty batch is a no-op
        that returns the current estimate, not an error and not a decay to zero.

        Guard the arithmetic before the clamp, not after: two samples with equal
        `sent_ms` divide by zero, unordered or duplicated samples make the
        gradient meaningless, and a hostile client controls every one of these
        values. See the module docstring on what a `nan` does to a clamp.
        """
        raise NotImplementedError("V4: delay-gradient over-use detector -> AIMD, clamped")

    def on_loss(self, fraction_lost: float) -> int:
        """Update from an RTCP receiver report's loss fraction — the backstop.

        TODO(V4): the WebRTC rule of thumb. Above ~10% loss, decrease
        multiplicatively (`estimate *= 1 - 0.5 * fraction_lost`). Below ~2%,
        increase additively to probe up. In between, hold — that dead band is
        deliberate, and removing it is how a controller starts oscillating.

        Clamp to `[min_bps, max_bps]`. The final estimate is the **minimum** of
        the loss-based and delay-based results: the more conservative signal
        wins, because being wrong downward costs quality and being wrong upward
        costs the call.

        `fraction_lost` comes off the wire and is not to be trusted: clamp it
        into `[0.0, 1.0]` and reject non-finite values before it multiplies
        anything.
        """
        raise NotImplementedError("V4: loss-based AIMD, clamped, min'd with the delay estimate")


def split_budget(budget_bps: int, stream_count: int) -> list[int]:
    """Divide a subscriber's budget across the streams it receives.

    With one video stream this is nearly trivial (all of it, less a margin). It
    becomes real with several — a screen-share next to a camera — where the
    allocator decides the split *before* each stream's `LayerSelector` picks a
    layer from its share.

    A plain function, not a class with one static method: it holds no state
    between calls and nothing else about it wants a `self`. (The Rust had an
    `Allocator` struct because a free function needed a home; Python has
    modules for that.)

    TODO(V4): reserve headroom (`HEADROOM`, or your own documented number) so
    the split never hands out 100%, then divide the rest across `stream_count`.
    The result must sum to **at most** `budget_bps` — watch the integer division,
    because `[budget // n] * n` is short by the remainder and a naive
    round-to-nearest is long by it, and "sums to <= budget" is the criterion.

    A priority scheme (camera over screen-share, or an equal split) is yours to
    choose and document. `stream_count == 0` returns an empty list; it is a real
    state — a subscriber that has joined and not yet subscribed to anything.
    """
    raise NotImplementedError("V4: reserve headroom, divide the budget across the streams")
