"""The live registry and per-stream window — **wired** plumbing, not a vertical.

This is the shared state between the two planes: publisher sessions (RTMP)
write built fMP4 bytes into it, viewers (HTTP) read them back out. Each live
stream is a bounded ring of segments and parts plus a **live edge** — the newest
`(msn, part)` a viewer can ask for — and a signal that fires whenever that edge
moves, which is what a blocking playlist reload parks on.

What lands here is already-built `bytes`. The *building* is V3 (`fmp4.py`) and
the *playlist rendering* is V4 (`llhls.py`); this module only holds the pieces
and hands them out.

## Build once, serve N — and why `bytes` makes that free

A part is muxed once, on the publisher's session, and stored here. Two hundred
viewers fetching it get two hundred references to **the same `bytes` object**:
Python's `bytes` is immutable, so sharing one is a refcount increment, never a
copy — the direct equivalent of cloning a refcounted `Bytes` in Rust. Starlette
writes a `Response(content=...)` body straight from that object. Keep it that
way: a `bytearray` here, or a `bytes(view)` per request, quietly turns fan-out
into per-viewer memcpy, and the boss fight's "each part muxed once" counter will
not catch it because the *mux* still happened once.

## Bounded memory on an unbounded stream

A broadcast never ends on its own, so the window is a fixed ring:
`collections.deque(maxlen=window_segments)`. Appending a new segment to a full
deque silently drops the oldest from the front — the eviction *is* the append.
RAM tracks the window, not airtime, which is why a ten-hour stream fits in the
same few megabytes as a ten-minute one.

## Why there are no locks

The Rust version put the window behind a `Mutex` because sessions and handlers
ran on a multi-threaded runtime. Here everything runs on **one event loop
thread**, and none of these methods `await` — so each runs to completion before
any other coroutine gets the thread, which makes each one atomic with respect to
every session and every handler. That guarantee holds exactly as long as nobody
adds an `await` in the middle of a mutation. If you ever do, you have written a
race, and no test on one loop will show it to you.

## Parking many requests on one signal

`await_edge` is the mechanism LL-HLS blocking reload needs: hundreds of held
playlist requests that all wake on the next `push_part`, with no thread and no
poll loop per request. It uses the *generation event* idiom: each edge change
sets the current `asyncio.Event` — waking every coroutine parked on it at once —
and installs a fresh one for the next change. A waiter re-checks its target
after waking, so a wake that does not reach its target just parks it again. The
*policy* built on top (what a request waits for, and for how long) is V4's.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NamedTuple

from .config import Settings

__all__ = [
    "LiveEdge",
    "LiveRegistry",
    "LiveStream",
    "Part",
    "Segment",
    "WindowSnapshot",
]


class LiveEdge(NamedTuple):
    """The newest `(msn, part)` a viewer can ask for.

    A `NamedTuple` for one reason: tuples compare lexicographically, so
    `LiveEdge(12, 3) >= LiveEdge(12, 1)` and `LiveEdge(13, 0) > LiveEdge(12, 9)`
    are already the right ordering — msn major, part minor — with no `__lt__`
    to write or get wrong.
    """

    msn: int
    part: int


@dataclass(frozen=True, slots=True)
class Part:
    """One built LL-HLS part: ~200–350 ms of fMP4 (`moof` + `mdat`)."""

    data: bytes
    duration: float
    """Seconds — what `#EXT-X-PART:DURATION=` advertises."""
    independent: bool
    """True when this part begins on an IDR keyframe (`INDEPENDENT=YES`)."""


@dataclass(slots=True)
class Segment:
    """One media segment in the window: its msn, its parts, and — once closed —
    the full-segment bytes a non-LL player fetches."""

    msn: int
    program_date_time: datetime
    """Wall clock at the segment's start, for `#EXT-X-PROGRAM-DATE-TIME`."""
    parts: list[Part] = field(default_factory=list[Part])
    complete: bool = False
    """False while still accumulating parts at the live edge."""
    data: bytes | None = None
    """Present once `complete`."""
    duration: float = 0.0


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    """What the playlist renderer (V4) walks: the window at one instant.

    The `segments` are copies whose `parts` lists are copied too, so a renderer
    holding a snapshot never sees a part appear mid-render. That copy is a list
    of references — the part *bytes* are never copied.
    """

    segments: tuple[Segment, ...]
    ended: bool
    edge: LiveEdge | None


class LiveStream:
    """One live stream's bounded window of built media, plus its edge signal."""

    def __init__(self, key: str, window_segments: int) -> None:
        self.key = key
        self._init: bytes | None = None
        self._segments: deque[Segment] = deque(maxlen=window_segments)
        self._next_msn = 0
        """Monotonic and never reused: an msn permanently names its bytes, which
        is what makes `Cache-Control: immutable` on a segment honest."""
        self._ended = False
        self._edge: LiveEdge | None = None
        self._edge_changed = asyncio.Event()

    # -- producer side (a publisher session, V2/V3) -------------------------

    def set_init(self, data: bytes) -> None:
        """Install the CMAF init segment, once the codec config is known."""
        self._init = data

    def push_part(self, part: Part, *, start_segment: bool) -> LiveEdge:
        """Append a built part at the live edge and wake every parked waiter.

        `start_segment=True` (a keyframe boundary) opens a new segment first;
        so does the very first part. Opening a segment on a full window evicts
        the oldest one. Returns the new edge.
        """
        if start_segment or not self._segments:
            self._segments.append(Segment(msn=self._next_msn, program_date_time=datetime.now(UTC)))
            self._next_msn += 1
        segment = self._segments[-1]
        segment.parts.append(part)
        segment.duration += part.duration
        self._edge = LiveEdge(segment.msn, len(segment.parts) - 1)
        self._wake()
        return self._edge

    def finish_segment(self, data: bytes) -> None:
        """Close the segment at the live edge with its full-segment bytes."""
        if self._segments:
            segment = self._segments[-1]
            segment.complete = True
            segment.data = data

    def mark_ended(self) -> None:
        """The publisher is gone: the playlist gains `#EXT-X-ENDLIST`, and every
        held request wakes rather than waiting on an edge that will never move."""
        self._ended = True
        self._wake()

    def _wake(self) -> None:
        self._edge_changed.set()
        self._edge_changed = asyncio.Event()

    # -- consumer side (HTTP handlers, V4) ----------------------------------

    @property
    def edge(self) -> LiveEdge | None:
        """The current live edge, or `None` before the first part exists."""
        return self._edge

    @property
    def ended(self) -> bool:
        return self._ended

    @property
    def init(self) -> bytes | None:
        """The init segment, or `None` until V3 has built it."""
        return self._init

    async def await_edge(self, target: LiveEdge) -> LiveEdge | None:
        """Park until the edge reaches `target`, or the stream ends.

        Returns the edge at that moment. No timeout here on purpose — the bound
        is policy, and policy is V4's (`llhls.media_playlist` wraps this in one).
        """
        while True:
            if self._edge is not None and self._edge >= target:
                return self._edge
            if self._ended:
                return self._edge
            # Read the event into a local *before* awaiting: `_wake` replaces
            # the attribute, and a waiter must park on the generation that was
            # current when it checked, or it can miss the wake entirely.
            changed = self._edge_changed
            await changed.wait()

    def segment_bytes(self, msn: int) -> bytes | None:
        """Full bytes of a *completed* segment still in the window."""
        segment = self._find(msn)
        return segment.data if segment is not None and segment.complete else None

    def part_bytes(self, msn: int, part: int) -> bytes | None:
        """Bytes of part `(msn, part)`, if still in the window."""
        segment = self._find(msn)
        if segment is None or not 0 <= part < len(segment.parts):
            return None
        return segment.parts[part].data

    def snapshot(self) -> WindowSnapshot:
        """The window at this instant, safe to render from. See `WindowSnapshot`."""
        return WindowSnapshot(
            segments=tuple(
                dataclasses.replace(segment, parts=list(segment.parts))
                for segment in self._segments
            ),
            ended=self._ended,
            edge=self._edge,
        )

    def _find(self, msn: int) -> Segment | None:
        # msns in the window are contiguous, so this is index arithmetic rather
        # than a scan — O(1) however large the window is configured.
        if not self._segments:
            return None
        index = msn - self._segments[0].msn
        if 0 <= index < len(self._segments):
            return self._segments[index]
        return None


class LiveRegistry:
    """The streams currently on air, keyed by stream key.

    Publisher sessions create entries; viewers look them up. A plain `dict` —
    see the module docstring on why a single event loop needs no lock around it.
    """

    def __init__(self, settings: Settings) -> None:
        self._streams: dict[str, LiveStream] = {}
        self._allowed = settings.allowed_keys
        self._window_segments = settings.live_window_segments

    def authorize(self, key: str) -> bool:
        """May this key publish? An empty allow-list means any key (dev only).

        TODO(security, horizontal): this is the auth gate the SPEC grades. A
        static allow-list is the floor; a real deployment verifies a signed
        token or calls an `on_publish` webhook here. Two things worth deciding
        deliberately: plain `in` short-circuits on the first differing byte, so
        compare secrets with `hmac.compare_digest`; and never log `key` — log a
        hash or a short prefix.
        """
        return not self._allowed or key in self._allowed

    def open(self, key: str) -> LiveStream:
        """Get-or-create the stream for `key` (publisher side).

        TODO(V2, security): get-or-create means a *second* publisher to a key
        that is already live gets the same window and interleaves its parts
        with the first one's. Whether that is refused, or takes over, is a
        decision the publish state machine has to make before calling this.
        """
        stream = self._streams.get(key)
        if stream is None:
            stream = LiveStream(key, self._window_segments)
            self._streams[key] = stream
        return stream

    def get(self, key: str) -> LiveStream | None:
        """The stream for `key` (viewer side), or `None` if nothing is on air."""
        return self._streams.get(key)

    def close(self, key: str) -> None:
        """Remove a stream when its publisher disconnects.

        Viewers already holding a reference keep it; `mark_ended` (called first)
        is what wakes them.
        """
        self._streams.pop(key, None)

    def live_keys(self) -> list[str]:
        """Keys currently on air, sorted — for `GET /live`."""
        return sorted(self._streams)

    def __len__(self) -> int:
        return len(self._streams)
