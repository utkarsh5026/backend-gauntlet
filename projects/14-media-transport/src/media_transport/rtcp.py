"""V3 — RTCP + selective retransmission (NACK): recover the losses that still matter.

RTCP is the back-channel that rides alongside RTP. A **Receiver Report** (RR)
tells the sender what the receiver actually sees — fraction lost, cumulative
lost, highest sequence, inter-arrival jitter. A **generic NACK** (RTPFB, RFC
4585 feedback message type 1) names lost packets compactly: a **PID** (a base
sequence number) plus a 16-bit **BLP** bitmask covering the next 16 sequence
numbers, so one 32-bit FCI word can request up to 17 packets. The sender keeps a
bounded **retransmit cache** of recently sent packets and, on a NACK, resends
the ones it still holds — but only the ones that can still arrive **before their
playout deadline**.

That last clause is the whole idea. This is reliability you *choose*, per
packet, against a clock — not TCP's total reliability that you pay for on every
byte whether the data is still worth having or not. A retransmit that arrives
after its playout moment is worse than useless: it consumed bandwidth the
congestion controller wanted, and the receiver drops it as late.

Everything here parses hostile bytes off an open UDP port, so every length word
is range-checked before it is used to slice.

## The shape, and why it is not the Rust one

Rust modelled the parsed packets as one `enum RtcpPacket` with three variants,
because that is how you get a heterogeneous list in a language with no runtime
types. Python has runtime types. Three small frozen dataclasses and a union
alias say the same thing with less machinery, and `match packet: case Nack():`
is exhaustive-checked by pyright the same way the Rust `match` was by rustc.

The FCI packing is a pair of module-level functions rather than methods, because
they are pure — a set of sequence numbers in, a list of words out, and back —
and the SPEC grades them by name (`nack_bitmask_packs_missing`,
`nack_packs_across_wrap`). A pure function is the easiest thing in the world to
property-test, and that is exactly what those criteria want.

## The bitmask, and the wrap

The BLP is where people get quietly wrong. Bit *i* of the mask means "PID + 1 +
i is also lost" — the PID itself is not in the mask, so one word covers 17
sequence numbers, not 16. And "PID + 1 + i" is 16-bit arithmetic: a PID of 65530
covers 65531…65535 and then 0…11, wrapping mid-word. In Rust that came free
from `u16` overflow; in Python you have to write `& SEQ_MASK` yourself, and a
test that only ever uses small sequence numbers will never tell you that you
forgot.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from .errors import TruncatedError
from .rtp import RtpPacket

__all__ = [
    "BLP_BITS",
    "FMT_GENERIC_NACK",
    "PT_BYE",
    "PT_RR",
    "PT_RTPFB",
    "PT_SDES",
    "PT_SR",
    "RTCP_HEADER",
    "Bye",
    "Nack",
    "ReceiverReport",
    "RetransmitCache",
    "RtcpPacket",
    "pack_fci",
    "parse_compound",
    "unpack_fci",
]

PT_SR = 200
"""Sender Report."""
PT_RR = 201
"""Receiver Report."""
PT_SDES = 202
"""Source Description."""
PT_BYE = 203
"""Goodbye — the source is leaving."""
PT_RTPFB = 205
"""Transport-layer feedback; the generic NACK lives here."""

FMT_GENERIC_NACK = 1
"""RTPFB feedback message type for a generic NACK (RFC 4585 §6.2.1)."""

RTCP_HEADER = 4
"""The common header every RTCP packet starts with: version|padding|count,
packet type, and a 16-bit length **in 32-bit words minus one**. That "minus
one" is not a typo and it is the classic off-by-one in this format: a
4-byte packet carries length 0."""

BLP_BITS = 16
"""Bits in the bitmask, so one FCI word covers PID plus 16 more = 17 numbers."""


@dataclass(frozen=True, slots=True)
class ReceiverReport:
    """One stream's reception quality, from the receiver's side.

    This is the sender's only window into what the far end experiences, and V4
    consumes it directly: `fraction_lost` is the loss signal the bitrate
    estimator backs off on.
    """

    reporter_ssrc: int
    """SSRC of the receiver sending this report."""
    media_ssrc: int
    """SSRC of the stream being reported on."""
    fraction_lost: int
    """Loss since the last report, as an 8-bit fixed-point numerator over 256.

    Kept as the raw wire byte (0–255) rather than a float, because this is the
    wire type and this dataclass is the wire. Divide by 256 once, at the
    boundary where it reaches the congestion controller — see `congestion.py`,
    which takes a float fraction for exactly that reason."""
    cumulative_lost: int
    """24-bit, signed per spec — it can go *negative* when duplicates outnumber
    losses, which is a real thing that happens with retransmission and a real
    way to get a nonsense loss rate if you parse it unsigned."""
    highest_sequence: int
    """Extended (32-bit) highest sequence received: the wrap count in the high
    16 bits, the sequence in the low 16. The same unwrapping V2 does, shipped
    across the wire so both ends agree which cycle they are in."""
    jitter: int
    """Inter-arrival jitter, in clock-rate **ticks** (not seconds — see
    `JitterStats.jitter`, which holds the same quantity in the unit the rest of
    this process uses)."""


@dataclass(frozen=True, slots=True)
class Nack:
    """A generic NACK: "resend these sequence numbers".

    The decoded set. The wire form packs them as PID+BLP words; `fci()` and
    `from_fci()` cross that boundary.
    """

    sender_ssrc: int
    """SSRC of the receiver asking."""
    media_ssrc: int
    """SSRC of the stream whose packets are missing."""
    lost: tuple[int, ...] = field(default=())
    """The missing 16-bit sequence numbers. A tuple so the dataclass stays
    frozen and hashable, and so nothing downstream can mutate a request that has
    already been serialized."""

    @classmethod
    def from_missing(cls, sender_ssrc: int, media_ssrc: int, missing: Sequence[int]) -> Nack:
        """Build a NACK for a set of missing sequence numbers."""
        return cls(sender_ssrc, media_ssrc, tuple(missing))

    def fci(self) -> list[int]:
        """This NACK's missing set as RFC 4585 FCI words."""
        return pack_fci(self.lost)


@dataclass(frozen=True, slots=True)
class Bye:
    """RFC 3550 BYE — the named sources are leaving.

    The graceful-shutdown criterion sends one of these so the peer learns the
    stream ended rather than inferring it from silence, which takes a timeout.
    """

    ssrcs: tuple[int, ...] = field(default=())


type RtcpPacket = ReceiverReport | Nack | Bye
"""What `parse_compound` returns, one per sub-packet it understood.

A union alias rather than a base class: these three share no behaviour and no
fields, only the fact that they arrive in the same datagram. `match` over the
union is checked for exhaustiveness by pyright, which is the property the Rust
enum was really providing.
"""


def pack_fci(missing: Sequence[int]) -> list[int]:
    """Pack missing sequence numbers into PID+BLP FCI words.

    TODO(V3): sort and dedupe `missing`, then greedily group: take the lowest
    remaining sequence as the **PID**, and set bit *i* of the 16-bit **BLP** for
    every sequence equal to `(PID + 1 + i) & SEQ_MASK` that is also missing.
    Emit `PID << 16 | BLP` as one word, drop everything that word covered, and
    repeat until nothing is left. One word covers up to 17 numbers; spill into
    more words as needed.

    The wrap is the part to test first, not last. "Sort the sequence numbers"
    is ambiguous when the set straddles 65535 → 0: numerically 0 sorts first,
    but in sequence order it comes last. Decide what the input means — the
    sensible contract is that `missing` is already in playout order because V2
    produced it that way from unwrapped indices — and state it. Then
    `(PID + 1 + i) & SEQ_MASK` handles the arithmetic, and a set like
    `{65533, 65535, 1}` packs into a single word.

    `pack_fci` then `unpack_fci` must return exactly the input set. That
    round-trip is the criterion, and it is the natural thing to hand to
    Hypothesis.
    """
    raise NotImplementedError("V3: greedily pack the missing set into PID+BLP words")


def unpack_fci(words: Sequence[int]) -> list[int]:
    """Expand FCI words back into the sequence numbers they name.

    TODO(V3): for each word, the PID is the high 16 bits and the BLP the low 16.
    Emit the PID, then `(PID + 1 + i) & SEQ_MASK` for every set bit *i* of the
    mask. The masking is not optional — `PID + 17` at 65534 is 65551, which is
    not a sequence number and will index nothing in the retransmit cache.
    """
    raise NotImplementedError("V3: expand PID+BLP words back into sequence numbers")


def parse_compound(data: bytes) -> list[RtcpPacket]:
    """Parse a compound RTCP datagram into the packets it contains.

    RTCP packets are stacked: one datagram routinely carries an SR or RR, an
    SDES, and any feedback, concatenated. So this walks the buffer rather than
    parsing one thing.

    TODO(V3): loop while at least `RTCP_HEADER` bytes remain. Read the common
    header — `version|padding|count` in byte 0, packet type in byte 1, and a
    16-bit **length in 32-bit words minus one** — and compute this sub-packet's
    size as `(length + 1) * 4`.

    **Validate that size against what is actually left before advancing.** A
    length word that overruns the datagram is `MalformedError`; so is a length
    of zero-that-does-not-advance, because a sub-packet that consumes nothing is
    an infinite loop, and an infinite loop inside a datagram handler on an open
    UDP port is a denial of service that anyone can trigger with four bytes.
    That is the one bug in this function that is worse than a crash.

    Then decode what you act on: `PT_RR` into a `ReceiverReport` (the count
    field says how many report blocks follow the 8-byte header, each 24 bytes),
    `PT_RTPFB` with `FMT_GENERIC_NACK` in the count field into a `Nack` (via
    `unpack_fci` over the FCI words after the 12-byte feedback header), and
    `PT_BYE` into a `Bye`. Skip the types you do not act on — SR, SDES, an
    unknown feedback format — by advancing past them rather than failing;
    refusing a datagram because it contained an SDES you did not want is how you
    end up unable to talk to any real stack.

    Returns an empty list for a well-formed datagram containing nothing you act
    on. That is not an error and must not be treated as one.
    """
    if len(data) < RTCP_HEADER:
        raise TruncatedError(need=RTCP_HEADER, got=len(data))
    raise NotImplementedError("V3: walk the compound packet, length-checking each sub-packet")


def serialize(packet: RtcpPacket) -> bytes:
    """Serialize one RTCP packet to a datagram.

    TODO(V3): write the common header — `version << 6 | count` (where "count" is
    the report-block count for an RR, the SSRC count for a BYE, and the *feedback
    message type* `FMT_GENERIC_NACK` for a NACK, which is the field being reused
    and the thing to get wrong once), the packet type, and the length in 32-bit
    words minus one — then the body:

    * `ReceiverReport`: the reporter SSRC, then a 24-byte report block
      (media SSRC, fraction lost in the top byte of a word whose low 24 bits are
      the cumulative loss, extended highest sequence, jitter, LSR, DLSR).
    * `Nack`: sender SSRC, media SSRC, then the FCI words from `fci()`.
    * `Bye`: the SSRC list.

    RTCP packets must be a whole number of 32-bit words, so anything that is not
    naturally aligned is padded — and the length word has to count the padding.
    Compute the length *after* you know the body, not from an assumption about
    it.

    A module-level function taking the union rather than a method on each class,
    so the `match` that dispatches on packet type lives in one readable place
    instead of being spread across three classes. `serialize` then
    `parse_compound` must round-trip the fields.
    """
    raise NotImplementedError("V3: encode the RTCP common header + body (RR / NACK / BYE)")


class RetransmitCache:
    """A bounded history of recently sent packets, so a NACK can be answered.

    Bounded by `capacity`: the oldest packet is evicted once full. That eviction
    is a *memory* bound, not a staleness policy — at 1.5 Mbps a 1024-packet ring
    holds several seconds, far longer than any playout deadline, so the deadline
    check in the sender is what actually decides not to retransmit. Confusing
    the two is how you end up believing you are deadline-aware when you are
    merely forgetful.
    """

    __slots__ = ("_by_sequence", "_capacity", "_order")

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        # TODO(V3): choose the structure. A NACK asks for up to 17 packets at
        # once and arrives on the feedback path, so `get` wants to be O(1) — a
        # `dict` keyed by 16-bit sequence, with a `deque` of sequences recording
        # insertion order for eviction, is the usual answer.
        #
        # The trap in that pairing: `deque(maxlen=capacity)` silently discards
        # from the far end when you append past the bound, and it does not tell
        # you what it discarded. Do that and the dict never shrinks — you have
        # written an unbounded cache that *looks* bounded, and "bounded memory
        # everywhere" is a graded criterion you would be failing invisibly. Pop
        # explicitly and delete the dict entry in the same step.
        self._by_sequence: dict[int, RtpPacket] = {}
        self._order: deque[int] = deque()

    def __len__(self) -> int:
        return len(self._by_sequence)

    @property
    def capacity(self) -> int:
        return self._capacity

    def record(self, packet: RtpPacket) -> None:
        """Record a just-sent packet, evicting the oldest if at capacity.

        TODO(V3): store it under its 16-bit sequence and evict from the front
        until the cache holds at most `capacity`. Both structures must stay in
        step — see the constructor on the way that goes wrong quietly.

        The sequence space wraps, so after ~65k packets a new packet reuses a
        sequence number an old one had. With a ring far smaller than 65536 the
        old one is long evicted and the overwrite is correct; convince yourself
        of that rather than assuming it, because the failure mode is
        retransmitting a packet from seven minutes ago in answer to a NACK for
        one from now.
        """
        raise NotImplementedError("V3: store by sequence, evict the oldest past capacity")

    def get(self, sequence: int) -> RtpPacket | None:
        """Fetch a still-cached packet by 16-bit sequence, or `None`.

        TODO(V3): a dict lookup. `None` for a miss — an evicted packet is the
        normal case, not an error, and raising here would turn "that one is too
        old" into an exception on the feedback path.
        """
        raise NotImplementedError("V3: look up a cached packet by sequence number")
