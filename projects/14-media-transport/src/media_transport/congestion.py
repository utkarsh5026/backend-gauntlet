"""V4 — congestion control: pace to the bandwidth the path actually has.

UDP will not slow you down when the link is full. It just drops your packets,
spikes the queueing delay, and quietly destroys the stream — there is no
receive window, no `send()` that blocks, nothing at all between your loop and
the wire. So the sender owns congestion control: **estimate** the available
bandwidth from feedback, and **pace** output to match, backing off when the path
congests and probing up when it clears.

Two signals drive the estimate. **Loss-based**: rising loss means you are
overshooting, so cut the rate (AIMD-style); near-zero loss means probe higher.
**Delay-based**: a growing one-way delay *gradient* means a queue is building
**before** it overflows into loss — the earlier, gentler signal that a real
controller (Google's GCC, the basis of WebRTC) blends in. You choose how
sophisticated to go; even a clean loss-based AIMD with a pacer is a passing V4.
But it must **converge, back off, and recover**, and the target is always
**clamped** to `[min, max]`.

The controller also owns the **pacer**: a leaky-bucket token gate that spreads
packets across the frame interval at the target rate instead of firing a whole
frame at once. Bursting is what builds the queue — and therefore the latency —
that everything above is trying to avoid. A sender that respects its average
rate but delivers it in 33 ms bursts thirty times a second is not paced.

## The pacer's shape is different in Python, on purpose

Rust exposed `can_send(now, bytes) -> bool`: a gate you poll. Poll a boolean in
an async Python loop and you have written a spin — `while not can_send(): await
asyncio.sleep(0)` burns the one thread that also runs the jitter buffer and the
HTTP server, and "no blocking call on the event loop" is a graded criterion you
would be failing by construction.

So this exposes `delay_before(now, size) -> float`: **how long until these bytes
may go out**, in seconds, `0.0` meaning now. The sender does
`await asyncio.sleep(delay)`, which hands the loop back for exactly as long as
the pacer says and not one iteration more. Same control law, a shape that fits
the runtime.

## NaN is a criterion here, and Python will not stop you

"the target is clamped to [min, max] and never goes negative, zero-stuck, or
unbounded" reads like defensive boilerplate. It is not. This is a control loop
driven by **arithmetic on numbers an attacker supplies**, and Python's floats
are IEEE doubles with no guard rails:

    >>> from math import nan
    >>> max(300_000, min(nan, 4_000_000))
    nan

Every comparison against a `nan` is `False`, so the idiomatic clamp passes it
straight through. It then poisons the estimate permanently — `nan * 0.85` is
`nan` — `int(nan)` raises `ValueError` two layers away in a metrics gauge, and
the traceback names a line that is entirely innocent. Two feedback samples with
identical timestamps make the gradient a `0/0`: `ZeroDivisionError` with ints,
a silent `nan` with floats. Check `math.isfinite` on anything derived from
feedback **before** it reaches the clamp, and guard the division rather than the
result.

`fraction_lost` has the same shape from the other direction. It arrives as a
byte off the wire over 256, so a hostile receiver can report 0.996 forever and
walk a multiplicative decrease straight into the floor. The clamp is what makes
that merely bad quality instead of a dead session — which is the actual reason
the criterion is written the way it is.
"""

from __future__ import annotations

__all__ = ["CongestionController"]


class CongestionController:
    """Loss- (and optionally delay-) based bitrate estimator with a token pacer.

    One per outbound stream. Holds the current target, the pacer's token level,
    and whatever smoothed state your control law needs.
    """

    __slots__ = ("_last_refill", "_max_bitrate", "_min_bitrate", "_target", "_tokens")

    def __init__(self, start_bitrate: int, min_bitrate: int, max_bitrate: int) -> None:
        self._min_bitrate = min_bitrate
        self._max_bitrate = max_bitrate
        # Clamped at construction, not just on update: a configured start above
        # the ceiling is not a valid estimate to begin from, and the criterion
        # says "always within [min, max]" with no exception for the first
        # second of the run.
        self._target = max(min_bitrate, min(start_bitrate, max_bitrate))

        # TODO(V4): the pacer's state, and whatever your control law needs.
        # Keep it O(1) per sample — this runs on the feedback path and a growing
        # history of samples is a leak with extra steps. Roughly: the token
        # level in bytes and when it was last refilled (below), plus, if you go
        # delay-based, the previous sample's (sent, arrived) pair and a smoothed
        # trend. An exponential moving average is one line and needs no window.
        self._tokens = 0.0
        self._last_refill = 0.0
        """`time.monotonic()` seconds. Zero means "not yet refilled" — the first
        `delay_before` should establish the baseline rather than crediting
        itself with however many seconds the process has been up."""

    @property
    def target_bitrate(self) -> int:
        """The current target send rate in bits/sec, always in `[min, max]`.

        Read by the pacer, the metrics gauge, and the media source (which sizes
        frames against it, so the synthetic stream actually responds to the
        controller instead of ignoring it).
        """
        return self._target

    @property
    def bounds(self) -> tuple[int, int]:
        """`(min, max)` bits/sec — the clamp the criteria are written against."""
        return (self._min_bitrate, self._max_bitrate)

    def on_receiver_report(self, fraction_lost: float, jitter: float) -> int:
        """Update the estimate from a receiver report. Returns the new target.

        `fraction_lost` is a **fraction in [0, 1]**, not the raw 8-bit wire
        numerator — the `/ 256` happens once, at the parse boundary, so this
        method never has to wonder which it is holding. `jitter` is in seconds,
        for the same reason.

        TODO(V4): apply the control law. The WebRTC rule of thumb is a good
        starting point and a defensible V4 on its own: above ~10% loss decrease
        multiplicatively (`target *= 1 - 0.5 * fraction_lost`), below ~2%
        increase additively to probe upward, and in between **hold**. That dead
        band is deliberate — removing it is how a controller starts oscillating,
        and "without oscillating wildly" is one of the criteria.

        Then clamp to `[min, max]`. Clamp the *inputs* too: reject non-finite
        values and pin `fraction_lost` into `[0, 1]` before it multiplies
        anything. See the module docstring on why an unguarded `nan` is a
        permanent failure rather than a bad reading.

        Returning the new target rather than `None` makes the caller's
        `gauge.set(cc.on_receiver_report(...))` a single line and makes the
        method trivially testable — the criteria are all statements about what
        the target became.
        """
        raise NotImplementedError("V4: AIMD/GCC-lite update from loss, clamped to [min, max]")

    def on_delay_sample(self, sent_delta: float, arrival_delta: float) -> int:
        """Feed one inter-packet delay sample — the delay-based signal.

        Two packets the sender emitted `sent_delta` seconds apart that the
        receiver saw `arrival_delta` seconds apart. The difference is this
        sample's contribution to the one-way delay **gradient**, and the
        absolute clock offset between the two machines cancels out — which is
        why this works with no clock sync at all.

        TODO(V4): accumulate the gradient (a smoothed trend, not a single
        sample — one packet's spacing is noise). A persistently rising gradient
        is a queue building, so trim the target *before* it overflows into loss;
        a flat or negative gradient is headroom to probe into. The final target
        is the **minimum** of the delay-based and loss-based results: the more
        conservative signal wins, because being wrong downward costs picture
        quality and being wrong upward costs the call.

        Guard the arithmetic before the clamp, not after. `sent_delta` can be
        zero (two packets stamped in the same instant), which is a division by
        zero or a silent `nan` depending on how you wrote it, and every value
        here is ultimately derived from something the far end reported.

        A pure loss-based controller is a legitimate V4. If that is your choice,
        leave this as a documented no-op returning the current target and say so
        in `docs/14-design.md` — an undocumented no-op and a deliberate one look
        identical in the code and completely different in a review.
        """
        raise NotImplementedError("V4: delay-gradient over-use detector (or a documented no-op)")

    def delay_before(self, now: float, size: int) -> float:
        """Seconds to wait before `size` bytes may go on the wire. `0.0` = now.

        TODO(V4): the token bucket. Refill by
        `target_bitrate / 8 * (now - last_refill)` bytes, cap the level at a
        small burst budget (a packet or two — a bucket that can accumulate a
        whole frame's worth of tokens is not pacing anything, it is just
        delaying the burst), and update `last_refill`. If the level covers
        `size`, return `0.0`; otherwise return how long the deficit takes to
        refill at the current rate: `(size - tokens) / (target_bitrate / 8)`.

        This is what spreads a frame's packets across the frame interval instead
        of firing them all at once, and the spacing it produces is directly what
        `pacer_spreads_sends` measures.

        Two things to be careful of. `target_bitrate` is clamped above zero by
        `min_bitrate`, so the division is safe — but only because of that clamp,
        which is one more reason it is a criterion. And a very large `size`
        against a small burst budget can return a long delay; that is correct
        behaviour (the packet genuinely cannot go out yet), but the sender is
        awaiting it, so a bug here shows up as a stream that stalls rather than
        as anything that looks like congestion control.
        """
        raise NotImplementedError("V4: token bucket — refill by rate x elapsed, return the wait")

    def on_sent(self, size: int) -> None:
        """Debit the pacer for `size` bytes actually sent.

        TODO(V4): subtract the bytes from the token level.

        Separate from `delay_before` because the sender may decide not to send
        after all — the shutdown signal arrives, the socket errors — and a pacer
        that debited on the *question* rather than the *answer* would throttle a
        stream for packets that never existed.
        """
        raise NotImplementedError("V4: debit the token bucket by the bytes sent")
