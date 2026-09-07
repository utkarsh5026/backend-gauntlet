"""A synthetic constant-bitrate media source — **fully wired**, not a vertical.

It emits fixed-size "access units" at a frame rate, a keyframe every GOP, with a
90 kHz RTP timestamp advancing per frame. It is deliberately *not* an encoder:
it produces byte buffers shaped like access units so the sender pipeline
(packetize → pace → send) has something to carry before you point a real
`ffmpeg -f rtp` feed or a camera at it.

This is the moral equivalent of project 13's wired registry — plumbing the
vertical work plugs into, given to you so the scaffold runs end to end the
moment you fill in the todos.

## Pacing a generator on an event loop

The Rust used `tokio::interval` with `MissedTickBehavior::Skip`. asyncio has no
interval primitive, so the cadence is hand-rolled against a monotonic deadline,
and the "skip" behaviour has to be written out — which is worth doing once,
because the naive version is a bug that compounds.

`await asyncio.sleep(1 / fps)` in a loop does not give you `fps` frames a
second. It gives you one frame every `1/fps` **plus however long the body took**
plus the loop's scheduling latency, so the stream drifts slower and slower and
the RTP timestamps — which advance by a fixed tick count per frame — drift away
from wall-clock time. Over a five-minute boss fight that is a visible desync,
and it looks exactly like a jitter-buffer bug.

Tracking an absolute next-deadline fixes the drift. Skipping deadlines that have
already passed (rather than firing them back to back to "catch up") is the other
half: under load, a burst of frames emitted at once is the last thing a
congestion controller needs, and a frame whose moment has passed is not worth
sending late.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .rtp import H264_CLOCK_RATE, TIMESTAMP_MASK

__all__ = ["Frame", "SyntheticSource"]


@dataclass(frozen=True, slots=True)
class Frame:
    """One synthetic access unit handed to the packetizer."""

    data: bytes
    """Encoded bytes — here, filler sized to hit the target bitrate."""
    keyframe: bool
    """True at a GOP boundary. V3 and V4 both care: a keyframe is the frame
    whose loss is worth recovering hardest, and the burst that most stresses the
    pacer."""
    rtp_timestamp: int
    """90 kHz media timestamp for this frame, shared by all its packets."""


class SyntheticSource:
    """A CBR frame generator paced against a monotonic deadline.

    `bitrate_bps` is re-read from the congestion controller between frames by
    the sender loop, so the synthetic stream actually responds to V4 instead of
    ignoring it — which is what makes the convergence criterion observable
    without a real encoder in the loop.
    """

    __slots__ = ("_frame_index", "_fps", "_gop", "_next_deadline", "_ticks_per_frame")

    def __init__(self, fps: int, gop: int) -> None:
        self._fps = max(1, fps)
        self._gop = max(1, gop)
        self._ticks_per_frame = H264_CLOCK_RATE // self._fps
        self._frame_index = 0
        self._next_deadline = 0.0

    @property
    def frame_interval(self) -> float:
        """Seconds between frames — the interval the pacer spreads a frame over."""
        return 1.0 / self._fps

    async def next_frame(self, bitrate_bps: int) -> Frame:
        """Wait for the next frame's moment, then produce it at `bitrate_bps`.

        The bitrate is a parameter rather than constructor state because it
        changes: the sender passes the congestion controller's current target on
        every call, so a backed-off estimate immediately produces smaller
        frames. That is the feedback loop closing.
        """
        now = time.monotonic()
        if self._next_deadline == 0.0:
            self._next_deadline = now
        # Skip, don't catch up: deadlines already in the past are abandoned
        # rather than fired back to back. See the module docstring.
        while self._next_deadline <= now:
            self._next_deadline += self.frame_interval
        await asyncio.sleep(self._next_deadline - now)

        frame_bytes = max(1, (bitrate_bps // 8) // self._fps)
        frame = Frame(
            data=bytes(frame_bytes),
            keyframe=self._frame_index % self._gop == 0,
            rtp_timestamp=(self._frame_index * self._ticks_per_frame) & TIMESTAMP_MASK,
        )
        self._frame_index += 1
        return frame
