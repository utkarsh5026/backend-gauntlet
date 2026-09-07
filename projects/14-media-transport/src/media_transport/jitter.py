"""V2 — the jitter buffer: a smooth playout out of a jittery arrival.

The network hands you packets early, late, out of order and duplicated. Playing
each the instant it lands would stutter. The jitter buffer holds a small window
— a *target delay* — releases packets **in sequence order** once they have
waited it out, drops duplicates and packets too late to use, and tracks the gaps
so V3 can NACK them. All of that costs a small, bounded amount of added latency,
and that tradeoff *is* what this buffer is.

Two subtleties make it more than a sorted list.

**The sequence number wraps.** It is 16 bits, so after 65535 comes 0, and a
naive comparison decides that packet 0 arrived 65535 places *before* packet
65534. You unwrap it to a monotonically increasing index — keep a running
high-water mark, and when a new 16-bit value looks like it went backwards by
more than half the sequence space, it actually went forwards across the wrap.
At 1.5 Mbps in 1200-byte packets the space wraps roughly every seven minutes,
so this is not a corner case in a five-minute boss fight: it is the fourth
minute.

**You have to estimate the jitter to size the buffer.** RFC 3550 defines a
smoothed inter-arrival estimate: for consecutive packets, compare the wall-clock
arrival spacing against the RTP-timestamp spacing (converted to seconds through
the clock rate); the difference is that packet's jitter contribution, smoothed
as `J += (|D| - J) / 16`. Too small a buffer and you stutter, too large and you
add needless latency — and the boss fight grades both ends.

## Choosing the data structure, in Python

The Rust used a `BTreeMap<u64, ...>`: ordered iteration, ordered insert, cheap
"what is the smallest key". Python's stdlib has no ordered map, and the
substitutes are not equivalent — this is a real design decision, not a
translation:

* **`dict` + `heapq`.** The dict is the membership test (duplicate detection is
  `index in self._packets`, O(1)); the heap gives you the smallest index in
  O(log n). The catch is that a heap cannot be scanned in order without
  destroying it, and `missing()` wants exactly that scan.
* **`dict` + `bisect.insort` into a sorted list of indices.** Ordered iteration
  and ordered lookup both work, `insort` is O(n) memmove but on a C-level list
  that is fast up to thousands of entries, and the buffer is capped at
  `capacity` anyway.
* **`sortedcontainers.SortedDict`.** The closest thing to the `BTreeMap`. Not a
  dependency here — add it if you decide the ergonomics are worth it, and say so
  in the design doc.

What is *not* acceptable is `min(self._packets)` on every `pop_frame`. That is
an O(n) scan at every 10 ms playout tick, and it is the kind of thing that looks
fine in a unit test and shows up as a flat 8% of your flamegraph in the boss
fight. Whichever structure you pick, "give me the lowest index" has to be
better than linear.

## Time is float seconds, from `time.monotonic`

Not `time.time()`. The wall clock can step backwards — NTP correction, a VM
resume — and a playout deadline computed across that step either fires
instantly or never. `time.monotonic()` cannot go backwards, which is the only
property a deadline needs. Arrival times are passed in rather than read inside
`insert` so tests can drive the clock without sleeping; a test that sleeps for a
100 ms playout target is a test suite that takes minutes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rtp import RtpPacket

__all__ = ["JitterBuffer", "JitterStats"]


@dataclass(frozen=True, slots=True)
class JitterStats:
    """A snapshot of the buffer's quality signals, for metrics and RTCP reports.

    Frozen and built on demand rather than mutated in place: a caller that holds
    one of these is holding a reading, not a live view of the buffer. That
    distinction stops the classic bug where a metric is scraped mid-update and
    reports a depth that never actually existed.
    """

    jitter: float = 0.0
    """Smoothed inter-arrival jitter estimate, in **seconds**.

    RFC 3550 defines it in clock-rate ticks; converting once here means nothing
    downstream — the metric, the receiver report, the design doc — has to
    remember which unit it is holding. The RR field is in ticks, so convert back
    at that one boundary."""

    buffered_packets: int = 0
    duplicates: int = 0
    """Repeated sequence numbers discarded. Includes retransmits that arrived
    after the original finally showed up — which is a signal about V3's NACK
    timing, not just about the network."""

    late: int = 0
    """Packets that arrived after their playout deadline had passed."""

    skipped: int = 0
    """Sequence gaps given up on so playout could continue."""


class JitterBuffer:
    """A reorder + playout buffer for one stream (SSRC).

    Keyed by the **unwrapped** sequence number so ordering survives the 16-bit
    wrap, and capped at `capacity` so a peer flooding future sequence numbers
    cannot grow this process without bound.
    """

    __slots__ = (
        "_capacity",
        "_clock_rate",
        "_duplicates",
        "_highest",
        "_jitter",
        "_late",
        "_packets",
        "_skipped",
        "_target_delay",
    )

    def __init__(self, target_delay: float, clock_rate: int, capacity: int) -> None:
        self._target_delay = target_delay
        """Seconds a packet must wait before it may be released."""
        self._clock_rate = clock_rate
        self._capacity = capacity

        # TODO(V2): the buffer's state. What is here is the minimum the wiring
        # needs to run; the shape is yours to choose (see the module docstring
        # on `dict` + `bisect` versus a heap). At minimum you will want:
        #   - the held packets, keyed by unwrapped index, each with its arrival
        #     time so `pop_frame` can check the deadline;
        #   - whatever index structure makes "the lowest held index" cheap;
        #   - the unwrap anchor: the previous 16-bit sequence, or None until the
        #     first packet establishes the base;
        #   - the previous (arrival, rtp_timestamp) pair for the RFC 3550
        #     jitter estimate;
        #   - the playout floor: the highest index already released or skipped,
        #     which is what makes "late" and "duplicate" decidable.
        self._packets: dict[int, tuple[RtpPacket, float]] = {}
        self._highest = 0
        self._jitter = 0.0
        self._duplicates = 0
        self._late = 0
        self._skipped = 0

    def __len__(self) -> int:
        """Packets currently held.

        `__len__` rather than an `is_empty()` method, so the session loop's
        idle check reads `if buffer:` — Python's own emptiness protocol, and it
        works before a single packet has ever arrived.
        """
        return len(self._packets)

    @property
    def stats(self) -> JitterStats:
        """The latest quality snapshot."""
        return JitterStats(
            jitter=self._jitter,
            buffered_packets=len(self._packets),
            duplicates=self._duplicates,
            late=self._late,
            skipped=self._skipped,
        )

    def insert(self, packet: RtpPacket, arrival: float) -> None:
        """Admit a packet that arrived at `arrival` (`time.monotonic()` seconds).

        TODO(V2): unwrap the packet's 16-bit sequence to a monotonic index —
        establish the anchor on the first packet, and thereafter decide whether
        a value that looks smaller than the high-water mark is *older* or is
        *newer across the wrap*. The standard test is the half-space one: a
        signed 16-bit difference, `(new - old + 32768) % 65536 - 32768`, is
        positive for forward and negative for backward. Python's `%` on
        negatives returns a non-negative result, which is exactly what you want
        here and the opposite of C's — one of the few places the language does
        the modular arithmetic favour rather than the trap.

        Then decide the packet's fate:

        * **duplicate** — the index is already held, or is at or below the
          playout floor: count it and discard. Do not overwrite; the first copy
          arrived earlier and its arrival time is the honest one for the jitter
          estimate.
        * **late** — below the floor because its frame has already been released
          or skipped: count it as late and discard. A retransmit that lost its
          race lands here, and the count is how you tell V3 its deadline bound
          is set wrong.
        * otherwise **hold it**, advance the high-water mark, and update the
          RFC 3550 jitter estimate from this arrival against the previous one
          (see the module docstring for the formula).

        Finally enforce `capacity`. Dropping the *newest* packet when full is
        usually wrong: the packets you are holding are the ones closest to being
        played, and evicting the oldest to make room for a future one throws
        away a frame you were about to release in favour of one you may never
        need. Whichever you choose, choose it deliberately and write it down —
        "bounded memory everywhere" is a graded criterion and this is where it
        is actually enforced.
        """
        raise NotImplementedError("V2: unwrap, classify dup/late, hold in order, update jitter")

    def pop_frame(self, now: float) -> list[RtpPacket] | None:
        """Release the next complete frame ready for playout, or `None`.

        TODO(V2): if the oldest held packet has waited out `target_delay`
        (`now - arrival >= self._target_delay`), release the next **complete
        frame**: the run of consecutive indices from the playout floor up to and
        including one whose header has the **marker** bit. Return `None` while
        the head frame is still incomplete *and* still inside its delay window.

        Once the window has passed and the gap has not filled, **skip** it —
        advance the floor past the missing index, count `skipped`, and try
        again. This is the criterion that stops the buffer stalling forever on a
        packet that is never coming, and it is the difference between a stream
        that degrades and a stream that freezes.

        `None` rather than an empty list for "nothing ready": an empty list
        would be a frame with no packets, which is not a thing, and the session
        loop's `while (frame := buffer.pop_frame(now)) is not None` reads
        exactly as the intent.

        Watch the deadline arithmetic. "Has waited `target_delay`" is about the
        *head* packet's arrival, not the current packet's — a packet that
        arrived 5 ms ago behind a packet that arrived 200 ms ago is not what
        gates the release. Getting this backwards produces a buffer that adds
        latency and never smooths anything, which is the worst of both.
        """
        raise NotImplementedError("V2: release the next in-order complete frame past target_delay")

    def missing(self) -> list[int]:
        """The 16-bit sequence numbers missing below the high-water mark.

        These are V3's NACK candidates. TODO(V2): scan from the playout floor to
        the highest index admitted, collect the indices you are not holding, and
        convert each back to a 16-bit sequence with `& SEQ_MASK` — V3 packs wire
        sequence numbers, not your internal unwrapped ones.

        Two things to think about before this ships. It runs on the feedback
        timer, so it must not be an O(capacity) scan that rebuilds a list of
        thousands every 200 ms; tracking gaps as they appear in `insert` is
        cheaper than rediscovering them. And a packet already NACK'd twenty
        milliseconds ago is still missing now — returning it again every tick is
        how you melt the back-channel. The SPEC puts the rate limiting in V3, so
        this can honestly return every gap; just know which of the two layers
        yours is doing it in, and write that down.
        """
        raise NotImplementedError("V2: report the sequence gaps below the high-water mark")
