"""Wire helpers — **fully wired**, not a vertical.

Two small things the media plane needs before any interesting work starts:

1. `classify` — the RFC 7983 first-byte demultiplex. One UDP port carries STUN
   *and* RTP *and* RTCP (WebRTC bundles them all), so the very first thing the
   pump does with a datagram is decide which it is, from its leading bytes. This
   is mechanical, so it is given to you.
2. `RtpPacket` — read/patch accessors over the fixed 12-byte RTP header. An SFU
   forwards RTP *without decoding it*: it reads a handful of header fields and,
   per subscriber, rewrites some of them. Parsing an RTP header from scratch was
   project 14's V1; here it is plumbing. The learning in *this* project is what
   the rewriter (V2) does with these, not re-deriving the byte layout.

## Why `bytearray`, and what it costs you

Rust's `RtpView<'a>` borrowed `&'a mut [u8]` and the compiler proved nobody else
held it. Python has no such proof and no such borrow: `bytes` is immutable, so
"rewrite the header in place" needs a `bytearray`, and every subscriber needs
its *own* copy because every subscriber gets a different sequence number.

That is not an implementation detail, it is the shape of the boss fight. One
ingress packet at 50 subscribers is 50 `bytearray` allocations of ~1200 bytes
each, and at 1.5 Mbps across three layers that is thousands of allocations a
second feeding the garbage collector. When the Crowded Room's forwarding p99
misses 10 ms, this is the first place to look, and
`docs/15-benchmarks.md` is where the finding goes — with the profile that shows
it, not the intuition that suspected it. (Whether a `memoryview` over one buffer
plus a per-subscriber header patch beats a copy per subscriber is a real
experiment, and a good one.)

## Why the `struct.Struct`s are module-level

`struct.pack(">H", x)` re-parses the format string on every call. `_U16.pack(x)`
against a `Struct` compiled once at import does not. On a path that runs per
packet per subscriber, that difference is measurable — and it is the kind of
CPython-specific cost that does not exist in the Rust this file replaces.
"""

from __future__ import annotations

import struct
from enum import StrEnum

from .errors import BadMagicError, TruncatedError

__all__ = [
    "RTP_MIN_HEADER",
    "RTP_VERSION",
    "Address",
    "PacketKind",
    "RtpPacket",
    "classify",
]

type Address = tuple[str, int]
"""A media-plane transport address: `(host, port)`.

Normalised at the pump boundary. `asyncio` hands IPv6 peers a 4-tuple
(`host, port, flowinfo, scope_id`); the extra two are dropped there so
everything downstream — the nomination table, XOR-MAPPED-ADDRESS, the route
lookup — has exactly one address shape to reason about. Dropping the scope id
does mean link-local IPv6 peers are not addressable, which is a documented
limit, not an oversight: state it in `docs/15-design.md` alongside the rest of
the ICE-lite scope.
"""

RTP_MIN_HEADER = 12
"""The fixed RTP header size, before any CSRC entries or extensions."""

RTP_VERSION = 2
"""The RTP version this SFU speaks."""

_U16 = struct.Struct(">H")
_U32 = struct.Struct(">I")


class PacketKind(StrEnum):
    """What kind of datagram arrived on the muxed UDP port (RFC 7983)."""

    STUN = "stun"
    """First byte in `0x00..=0x03`."""

    RTP = "rtp"
    """First byte in `0x80..=0xBF`, second byte's payload type *not* 192..=223."""

    RTCP = "rtcp"
    """First byte in `0x80..=0xBF`, second byte's packet type in `192..=223`."""

    UNKNOWN = "unknown"
    """DTLS, ZRTP, a TURN channel, or garbage — dropped by this SFU."""


def classify(datagram: bytes) -> PacketKind:
    """Classify a datagram by its leading bytes. Cheap, total, allocation-free.

    Total on *every* input including empty, because this runs before any
    validation on bytes that arrived from anyone at all.
    """
    if not datagram:
        return PacketKind.UNKNOWN
    first = datagram[0]
    if first <= 3:
        return PacketKind.STUN
    if 128 <= first <= 191:
        # RTP and RTCP share the 0x80 band; the second byte disambiguates. An
        # RTCP packet type sits in 192..=223, everything else in that band is RTP.
        if len(datagram) < 2:
            return PacketKind.UNKNOWN
        return PacketKind.RTCP if 192 <= datagram[1] <= 223 else PacketKind.RTP
    return PacketKind.UNKNOWN


class RtpPacket:
    """A read/patch view over one RTP datagram's fixed header.

    Wraps a `bytearray` and patches big-endian fields in place; the payload is
    never touched, copied out, or decoded. This is the SFU's forwarding lens.

    Construction validates, and *raises* rather than returning `None`: a runt or
    wrong-version datagram is a `MediaError` like every other bad datagram, and
    the pump's single `except` around dispatch is where it belongs. That also
    means the fan-out validates once, before the per-subscriber loop, instead of
    re-checking the same bytes for every subscriber.
    """

    __slots__ = ("buf",)

    def __init__(self, buf: bytearray) -> None:
        if len(buf) < RTP_MIN_HEADER:
            raise TruncatedError(need=RTP_MIN_HEADER, got=len(buf))
        if (buf[0] >> 6) != RTP_VERSION:
            raise BadMagicError(f"rtp version {buf[0] >> 6}, want {RTP_VERSION}")
        self.buf = buf

    @property
    def marker(self) -> bool:
        """Marker bit — set on the last packet of a frame, a safe switch boundary."""
        return self.buf[1] & 0x80 != 0

    @property
    def payload_type(self) -> int:
        """7-bit payload type."""
        return self.buf[1] & 0x7F

    @property
    def sequence(self) -> int:
        """Per-packet sequence number."""
        return _U16.unpack_from(self.buf, 2)[0]

    @sequence.setter
    def sequence(self, seq: int) -> None:
        """Overwrite it — the per-subscriber rewriter keeps this contiguous."""
        _U16.pack_into(self.buf, 2, seq & 0xFFFF)

    @property
    def timestamp(self) -> int:
        """Media timestamp, in clock-rate ticks."""
        return _U32.unpack_from(self.buf, 4)[0]

    @timestamp.setter
    def timestamp(self, ts: int) -> None:
        """Overwrite it — rebased per subscriber across a layer switch."""
        _U32.pack_into(self.buf, 4, ts & 0xFFFFFFFF)

    @property
    def ssrc(self) -> int:
        """Synchronization source — identifies the origin stream / simulcast layer."""
        return _U32.unpack_from(self.buf, 8)[0]

    @ssrc.setter
    def ssrc(self, ssrc: int) -> None:
        """Overwrite it — the SFU presents one stable SSRC per subscriber,
        hiding layer switches behind it."""
        _U32.pack_into(self.buf, 8, ssrc & 0xFFFFFFFF)
