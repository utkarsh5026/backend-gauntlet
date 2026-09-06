"""V2 — Selective RTP forwarding: the heart of an SFU.

An SFU is the middle path between two extremes. A **mesh** makes every publisher
upload one copy per viewer (N² uploads — dead at about four people). An **MCU**
decodes everyone, composites one mixed picture, and re-encodes it per viewer
(CPU-melting, and it adds a decode/encode hop to every frame). An **SFU** does
neither: it **forwards the publisher's already-encoded RTP packets, payload
untouched, to each subscriber** — one upload from the publisher, one tailored
download per subscriber, no transcode. That is how a 50-person call works.

The catch is that "forward untouched" is not quite true at the *header*. Each
subscriber must see one **continuous** RTP stream: a single stable SSRC,
sequence numbers with **no gaps** (a browser's jitter buffer reads a gap as loss
and NACKs it), and a monotonic timestamp — even though the SFU is *dropping*
packets under that subscriber (a deselected simulcast layer, a packet that lost
the pacing race) and *switching* which origin stream feeds them mid-call (V3).

So per subscriber the SFU keeps a tiny `Rewriter` that maps whatever origin
currently feeds it onto that subscriber's own continuous line, and remembers
enough of the mapping to **translate a NACK back**: when a subscriber asks to
re-send *its* sequence 4127, the SFU has to know that was the origin's 5981.
Reliability, as in project 14, is a thing you route — here, across a rewrite.

(The routing table origin-SSRC -> subscribers is plain bookkeeping the wired
`sfu` core keeps. The *learning* is the rewriter this file builds.)

## Python does not wrap for you

This is the trap of this vertical, and it is specific to the conversion. Rust's
`u16` made "wrapping at 65535" a property of the type; `wrapping_add(1)` was the
whole implementation. Python integers are arbitrary precision, so
`self._last_out_seq + 1` past 65535 gives you **65536**, not 0. It will even
*look* fine on the wire, because `RtpPacket.sequence`'s setter masks before
packing — so the bytes leaving the process are correct while the number you
compare, subtract and index with has silently left the 16-bit world. Then the
NACK-window lookup misses, or the contiguity assertion fires forty minutes into
a call.

Mask at every arithmetic step: `(seq + 1) & 0xFFFF`. And compare sequence
numbers with serial arithmetic (RFC 1982) rather than `<`, because 65535 comes
*before* 0: `((a - b) & 0xFFFF) < 0x8000` means "a is at or after b".

## Bounded means bounded

The obvious NACK history is `dict[int, int]` mapping outbound sequence to origin
sequence, and it is wrong: nothing ever removes from it, so a subscriber that
never NACKs grows it forever and the SPEC's "fixed-size regardless of stream
length" criterion fails silently over hours. The bounded shapes are a
`collections.deque(maxlen=N)` of pairs, or — better on this path — a
preallocated `list` of length N indexed by `out_seq % N`, which is O(1) to write
*and* O(1) to look up, at the cost of having to store the outbound sequence
alongside the origin one so you can tell a live entry from a stale slot that
happens to sit at the same index.
"""

from __future__ import annotations

from .wire import RtpPacket

__all__ = ["NACK_HISTORY", "SEQ_MASK", "Rewriter"]

SEQ_MASK = 0xFFFF
"""RTP sequence numbers are 16-bit. Python's ints are not — see the module docstring."""

NACK_HISTORY = 512
"""How many recent outbound packets stay translatable back to their origin.

A deadline in disguise, exactly as in project 14: a packet older than this is
one a retransmit would deliver too late to be played, so forgetting it is not a
lost capability but a refusal to do useless work. At a typical 1200-byte packet
and 2 Mbps that is roughly two and a half seconds of video, which is already
past any sane jitter buffer. Pick your own number, and record the reasoning
(and the resulting memory per subscriber) in `docs/15-design.md` — the SPEC
grades the bound, not the value.
"""


class Rewriter:
    """Per-subscriber header rewriter.

    Turns a possibly-switching, possibly-gappy origin stream into one continuous
    RTP stream for a single subscriber. One `Rewriter` lives per **subscriber**,
    not per origin — which is exactly what lets a simulcast layer switch stay
    invisible downstream.
    """

    __slots__ = ("_state", "out_ssrc")

    def __init__(self, out_ssrc: int) -> None:
        self.out_ssrc = out_ssrc
        """The stable SSRC this subscriber sees regardless of origin switches."""

        # TODO(V2): the state your continuity scheme needs. Roughly:
        #   * the last outbound sequence number handed out (so the next is +1,
        #     masked), and the last outbound timestamp;
        #   * the current origin SSRC, so you can *detect* a switch — and the
        #     origin sequence/timestamp you saw when it happened, so you can
        #     rebase rather than jump;
        #   * a bounded outbound-seq -> origin-seq history (see the module
        #     docstring on why a `dict` is the wrong shape).
        # Declare each in `__slots__` above; a per-subscriber object at 50+
        # subscribers is one of the few places in this project where the
        # per-instance `__dict__` you would otherwise get is worth removing.
        self._state: None = None

    def rewrite(self, packet: RtpPacket) -> int:
        """Rewrite `packet` in place for this subscriber; return the outbound sequence.

        Stamps the stable outbound SSRC, the next contiguous outbound sequence
        number, and a continuous timestamp. The returned sequence is what the
        caller indexes a retransmit cache by.

        TODO(V2): assign `(last_out_seq + 1) & SEQ_MASK` — **not** the origin
        sequence, so gaps the SFU introduced never show up as loss downstream.
        Rebase the timestamp so it stays monotonic across an origin switch (the
        origin's clock restarts from an unrelated value; the subscriber's must
        not go backwards). Write all three through `packet.ssrc`,
        `packet.sequence` and `packet.timestamp`, and record the
        outbound -> origin sequence pair in the bounded history.

        Note what "in place" means here and did not mean in Rust: `packet` wraps
        a `bytearray` the caller owns and there is no borrow checker watching
        it, so the caller's contract — one fresh copy per subscriber — is a
        convention you have to keep rather than one the compiler keeps for you.
        Two rewriters over one buffer is the isolation criterion failing.
        """
        raise NotImplementedError("V2: stamp ssrc + contiguous seq + continuous ts; record it")

    def skip(self) -> None:
        """Note that one origin packet was **not** forwarded to this subscriber.

        A deselected layer, or a packet that lost the pacing race. The outbound
        line must stay gapless anyway.

        TODO(V2): advance whatever bookkeeping keeps the next `rewrite`
        contiguous. If the answer is "nothing at all", that is a legitimate
        design — say why in `docs/15-design.md`, because it means your scheme
        counts outbound packets rather than tracking an offset from the origin,
        and that choice has consequences for the timestamp rebase.
        """
        raise NotImplementedError("V2: account for a dropped origin packet, gaplessly")

    def to_origin_seq(self, out_seq: int) -> int | None:
        """Translate a subscriber's NACK back to the origin sequence, if still known.

        TODO(V2): look `out_seq` up in the bounded history. Return the origin
        sequence if it is there, `None` if it has aged out — too old to usefully
        retransmit, the same deadline logic as project 14.

        Correct behaviour **across the 16-bit wrap** is an explicit criterion,
        and it is the case a ring buffer gets wrong for free: a stale slot at
        `out_seq % NACK_HISTORY` holds a plausible-looking origin sequence from
        512 packets ago, so returning it is worse than returning `None` — the
        publisher re-sends a packet nobody asked for and the subscriber gets a
        duplicate it will happily decode into a glitch. Store the outbound
        sequence in the slot and check it matches before you trust the entry.
        """
        raise NotImplementedError("V2: map an outbound seq back to its origin seq")
