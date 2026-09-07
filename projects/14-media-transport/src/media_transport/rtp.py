"""V1 — RTP packetization + depacketization: turn a frame into datagrams and back.

RTP is the thin header that makes a *media stream* out of lonely UDP datagrams.
The header is 12 bytes before any optional CSRCs::

     0                   1                   2                   3
     0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |V=2|P|X|  CC   |M|     PT      |       sequence number         |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                           timestamp                           |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                             SSRC                              |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                        CSRC (0..=15) …                        |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

The **sequence number** increments once per packet and wraps at 65535. The
**timestamp** is the *media* clock (90 kHz for video), so every packet of one
frame shares it and it jumps by the sampling interval between frames — two
different clocks, deliberately, because "which packet" and "when to show it"
are different questions. The **marker bit** is set on the last packet of a
frame. A frame bigger than the path MTU is split with H.264 **FU-A**
fragmentation (start/end bits mark the pieces); small NALs ship whole.

## Three things Python will let you get wrong here

**Slicing does not bounds-check.** `data[4:8]` on a three-byte buffer returns
three bytes rather than raising, so a truncated datagram becomes a plausible
wrong number instead of an error. Check `len(data)` before every read. Prefer
`struct.unpack_from(">HHII", ...)`, which at least raises `struct.error` when
the buffer is short — then convert that into a `TruncatedError` at the boundary
so callers only ever see this module's error family.

**Python integers do not wrap.** `sequence + 1` at 65535 is 65536, not 0, and
`(65536).to_bytes(2, "big")` raises `OverflowError` — but only *eventually*,
several packets after the arithmetic that was actually wrong. Mask deliberately
with `& SEQ_MASK` at the point of increment, not at the point of serialization.
This is the single most common conversion bug from a language with `u16`, and
V2 hits it again from the other direction when it unwraps.

**`bytes` versus `bytearray` versus `memoryview` is a real choice.** Rust had
`Bytes`, where a clone is a refcount bump — which is what let one packet sit in
the retransmit cache and go out on the wire without a copy. Python's `bytes` is
immutable and shareable, so it has the same property and is the right default
here. `memoryview` avoids the copy when you slice a large access unit into
fragments; it is worth reaching for in `packetize`, where the boss fight will
be counting your allocations.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .errors import BadVersionError, OversizedError, TruncatedError

__all__ = [
    "H264_CLOCK_RATE",
    "RTP_MIN_HEADER",
    "RTP_VERSION",
    "SEQ_MASK",
    "TIMESTAMP_MASK",
    "Packetizer",
    "RtpHeader",
    "RtpPacket",
    "depacketize",
]

RTP_VERSION = 2
"""The only RTP version in use, and the cheapest filter on an open port."""

RTP_MIN_HEADER = 12
"""Fixed header size before any CSRC entries."""

H264_CLOCK_RATE = 90_000
"""Video media clock (Hz). The timestamp is in these ticks, not milliseconds —
at 30 fps a frame is 3000 ticks apart, which is the number V2 needs to convert
an RTP-timestamp spacing into a wall-clock one."""

SEQ_MASK = 0xFFFF
"""16-bit sequence space. Python ints do not wrap; this is how you make them."""

TIMESTAMP_MASK = 0xFFFFFFFF
"""32-bit media timestamp space. Same reason."""


@dataclass(frozen=True, slots=True)
class RtpHeader:
    """A parsed RTP header — the fixed 12 bytes plus any CSRC list.

    Frozen because a header is a *value*: two headers with the same fields are
    the same header, and `==` says so, which is exactly what the round-trip
    criterion asserts. `slots=True` because the boss fight allocates one of
    these per packet at 150+ packets a second and `__dict__` per instance is
    pure overhead at that rate.
    """

    marker: bool
    """Set on the **last** packet of a frame. The receiver's "the frame is
    complete" signal, and the reason V2 can release a frame as a unit."""

    payload_type: int
    """7-bit; 96 is a dynamic mapping, typical for H.264."""

    sequence: int
    """Per-packet, wraps at 65535. Masked to 16 bits — see the module docstring."""

    timestamp: int
    """Media clock in `H264_CLOCK_RATE` ticks, shared across a frame's packets."""

    ssrc: int
    """Synchronization source id — identifies the stream. Random per RFC 3550."""

    csrc: tuple[int, ...] = field(default=())
    """Contributing sources; empty for a single unmixed source. A tuple rather
    than a list so the dataclass can stay frozen *and* hashable."""

    @property
    def byte_length(self) -> int:
        """How many bytes this header occupies on the wire.

        A pure function of the CSRC count, which is why `parse` does not have to
        return a length alongside the header the way the Rust did — the payload
        offset is recoverable from the header itself. One returned value instead
        of a tuple, and no way for a caller to pair the wrong two.
        """
        return RTP_MIN_HEADER + 4 * len(self.csrc)

    @classmethod
    def parse(cls, data: bytes) -> RtpHeader:
        """Parse a header off the front of `data`.

        TODO(V1): read the fixed 12 bytes big-endian — the version (top 2 bits
        of byte 0; anything but `RTP_VERSION` is a `BadVersionError`), the CSRC
        count (low 4 bits of byte 0), the marker bit and 7-bit payload type
        (byte 1), then the u16 sequence and the two u32s. `struct.unpack_from`
        with a `">BBHII"` format reads the fixed part in one call; the CSRC
        words are `cc` more u32s after it.

        **Range-check before every read.** The `len(data) < RTP_MIN_HEADER`
        guard below is the first one and it is done for you; the CSRC list needs
        the same treatment, because `cc` comes off the wire and can claim 15
        words (60 bytes) that are not there. A short buffer must raise
        `TruncatedError`, never return a header built from bytes that were not
        sent — see the module docstring on why Python makes that easy to do by
        accident.

        Padding and extension bits are parsed-and-ignored here. If you decide to
        honour them, say so in `docs/14-design.md`; ignoring a padding bit means
        trailing pad bytes reach the depacketizer as payload.
        """
        if len(data) < RTP_MIN_HEADER:
            raise TruncatedError(need=RTP_MIN_HEADER, got=len(data))
        version = data[0] >> 6
        if version != RTP_VERSION:
            raise BadVersionError(version)
        raise NotImplementedError("V1: decode marker/PT/seq/ts/SSRC and the CSRC list")

    def to_bytes(self) -> bytes:
        """Serialize this header.

        TODO(V1): the exact inverse of `parse`. Byte 0 is
        `version << 6 | padding << 5 | extension << 4 | len(csrc)`, byte 1 is
        `marker << 7 | payload_type`, then sequence/timestamp/SSRC and each CSRC
        word, all big-endian — `struct.pack(">BBHII", ...)` plus the CSRCs.

        Mask `sequence` and `timestamp` on the way in rather than trusting the
        caller: `struct.pack(">H", 65536)` raises `struct.error` from inside
        this method, which points at the serializer when the bug is in whoever
        incremented past the wire width.

        `to_bytes` then `parse` must reproduce this header exactly — that is the
        round-trip criterion, and it is an `==` on a frozen dataclass.
        """
        raise NotImplementedError("V1: encode the fixed 12 bytes + CSRC list, big-endian")


@dataclass(frozen=True, slots=True)
class RtpPacket:
    """One RTP packet: a header and its media payload.

    `payload` is `bytes` rather than `bytearray` on purpose. It is immutable, so
    handing the same packet to the wire *and* to the retransmit cache shares one
    buffer instead of copying it — the property Rust got from `Bytes` and the
    reason a retransmit costs no allocation.
    """

    header: RtpHeader
    payload: bytes

    @classmethod
    def parse(cls, data: bytes) -> RtpPacket:
        """Parse a whole datagram into header + payload.

        TODO(V1): `RtpHeader.parse`, then take everything after
        `header.byte_length` as the payload. Two lines, and the second one is
        the reason `byte_length` is a property.
        """
        raise NotImplementedError("V1: parse the header, then slice off the payload")

    def to_bytes(self) -> bytes:
        """Serialize the whole packet.

        TODO(V1): the header's bytes followed by the payload.
        """
        raise NotImplementedError("V1: header bytes + payload")


class Packetizer:
    """Splits access units into RTP packets for one stream.

    Holds the running sequence number — the one piece of per-stream state
    packetization needs — plus the immutable SSRC, payload type and MTU budget.
    """

    __slots__ = ("_mtu", "_payload_type", "_sequence", "_ssrc")

    def __init__(self, ssrc: int, payload_type: int, mtu: int, initial_sequence: int) -> None:
        if mtu <= RTP_MIN_HEADER:
            raise OversizedError(f"mtu {mtu} cannot hold a {RTP_MIN_HEADER}-byte RTP header")
        self._ssrc = ssrc
        self._payload_type = payload_type
        self._mtu = mtu
        self._sequence = initial_sequence & SEQ_MASK

    @property
    def ssrc(self) -> int:
        return self._ssrc

    @property
    def next_sequence(self) -> int:
        """The sequence number the next emitted packet will carry.

        Exposed so tests can assert the consecutive-sequence criterion without
        reaching into a private attribute, and so the retransmit cache can
        reason about what it should still be holding.
        """
        return self._sequence

    def packetize(self, access_unit: bytes, rtp_timestamp: int) -> list[RtpPacket]:
        """Split one encoded access unit into RTP packets.

        TODO(V1): walk the access unit's NAL units. One that fits in
        `self._mtu - RTP_MIN_HEADER` ships as a **single** packet; a larger one
        is split into **FU-A** fragments, each carrying a two-byte
        fragmentation-unit header (an indicator byte that borrows the original
        NAL's F and NRI bits with type 28, then a byte with start/end bits and
        the original NAL type). Assign each packet the next sequence — masked
        with `SEQ_MASK`, because Python will happily hand you 65536 — the shared
        `rtp_timestamp`, this stream's SSRC and payload type, and set the marker
        bit on the **last** packet of the frame only.

        No emitted packet may exceed `self._mtu` once serialized. Budget the
        header *and* the FU header against it, not just the payload; the
        criterion is about the datagram, and being 2 bytes over is the same
        failure as being 200 over.

        Two Python-specific notes. Slice with a `memoryview` over the access
        unit rather than `access_unit[i:j]` if you want to avoid copying every
        fragment — at 1.5 Mbps that is a copy of the whole stream through the
        GC. And the synthetic source in `media.py` emits a single flat buffer
        with no NAL start codes, so decide early whether you scan for
        `0x00000001` boundaries (real H.264) or treat the access unit as one
        NAL; either is a passing V1 as long as `docs/14-design.md` says which.
        """
        raise NotImplementedError("V1: single-NAL / FU-A packetize the access unit")


def depacketize(packets: Sequence[RtpPacket]) -> bytes:
    """Reassemble one frame's packets, in sequence order, into an access unit.

    TODO(V1): concatenate the payloads, undoing FU-A fragmentation — a
    single-NAL payload is emitted as-is; a run of FU-A fragments from the
    start-bit packet to the end-bit packet is stitched back into the original
    NAL, dropping the two-byte FU header from each fragment and reconstructing
    the original NAL header byte from the indicator's F/NRI bits and the
    fragment header's type bits.

    A missing fragment — a gap in the sequence run, a run that never sees an end
    bit, an end bit with no start — must raise `MalformedError` rather than
    return the concatenation of what did arrive. Emitting corrupt bytes is worse
    than emitting nothing: a decoder handed a half-frame produces visible
    garbage that looks like a codec bug, while a dropped frame looks like exactly
    what it is.

    Build the result with a `list` of pieces and one `b"".join(...)` at the end
    rather than `result += fragment` in a loop. The `+=` form is quadratic — it
    reallocates and copies the whole accumulated buffer every iteration — and a
    keyframe fragmented into 30 packets is where you will feel it.
    """
    raise NotImplementedError("V1: reassemble single-NAL + FU-A payloads into one access unit")
