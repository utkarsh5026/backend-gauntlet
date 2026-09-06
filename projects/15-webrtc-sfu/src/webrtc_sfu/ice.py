"""V1 — ICE / STUN connectivity: let a browser behind NAT actually reach the SFU.

Before a single media byte flows, the two sides have to find a path through NAT
and prove to each other that the path works. That is **ICE**, and its packets are
**STUN** messages. Signaling (the HTTP plane) hands each side the other's
`ufrag`/`pwd` and a candidate address; then each side fires **STUN Binding
requests** at the other's candidates and watches for **success responses**. The
first candidate pair that completes a check round-trip and gets **nominated**
becomes the path media rides.

This SFU is an **ICE-lite** server: it does not gather reflexive candidates and
never sends its own checks. It answers the browser's checks and remembers which
source address won — but answering *correctly* is the whole vertical.

A **STUN message** is a 20-byte header followed by 4-byte-aligned attribute TLVs::

     0                   1                   2                   3
     0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |0 0|     STUN Message Type      |         Message Length        |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                    Magic Cookie (0x2112A442)                  |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                    Transaction ID (96 bits)                   |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |            Attributes: TLV, each padded to 4 bytes ...         |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

The Binding **response** must echo the transaction id and carry an
**XOR-MAPPED-ADDRESS** (the source address the SFU saw, XORed with the magic
cookie + txid so a NAT rewriting payloads cannot corrupt it), a
**MESSAGE-INTEGRITY** (HMAC-SHA1 over the message, keyed by the ICE `pwd`), and
a **FINGERPRINT** (`CRC32(message) ^ 0x5354554E`). Get any of them wrong and the
browser silently discards your response; nothing is logged anywhere, the call
just never connects. That silence is what makes the unit tests worth more here
than in any other vertical of this project.

## Everything you need is in the standard library

`hmac.new(key, msg, hashlib.sha1).digest()` is MESSAGE-INTEGRITY.
`zlib.crc32(msg) ^ 0x5354554E` is FINGERPRINT. `struct` reads the big-endian
header fields, `ipaddress.ip_address` tells an IPv4 peer from an IPv6 one, and
`int.to_bytes` / `int.from_bytes` do the XOR arithmetic. There is no dependency
to add for any of it, which is the point: the crypto here is two stdlib calls,
and every bug you will actually hit is in the *framing* around them — what is
covered, how long the length field claims to be, what order things go in.

## Two Python-specific traps on this path

**Slicing does not bounds-check.** `data[4:8]` on a five-byte buffer returns one
byte and raises nothing. Rust would have panicked and told you where; Python
hands you a short `bytes` that flows on and fails somewhere unrelated. Every
length in a STUN message is attacker-controlled, so compare it against
`len(data)` *before* you slice — that is the "bounds-checked and total on
garbage" criterion, and in Python it is entirely on you.

**Compare MACs with `hmac.compare_digest`, never `==`.** Python's `bytes.__eq__`
short-circuits on the first differing byte, so how long it takes leaks how many
leading bytes were right. That is a byte-at-a-time forgery oracle against a live
UDP port that answers as fast as you can ask. `compare_digest` is constant-time
and is a one-word change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .wire import Address

__all__ = [
    "BINDING_METHOD",
    "FINGERPRINT_XOR",
    "STUN_HEADER_LEN",
    "STUN_MAGIC_COOKIE",
    "Fingerprint",
    "IceAgent",
    "IceControlled",
    "IceControlling",
    "IceResult",
    "MessageIntegrity",
    "Priority",
    "StunAttribute",
    "StunClass",
    "StunMessage",
    "UnknownAttribute",
    "UseCandidate",
    "Username",
    "XorMappedAddress",
    "fingerprint",
    "message_integrity",
]

STUN_MAGIC_COOKIE = 0x2112A442
"""Bytes 4..8 of every RFC 5389 message; also XORed into mapped addresses."""

STUN_HEADER_LEN = 20
"""Type + length + cookie + 96-bit transaction id."""

FINGERPRINT_XOR = 0x5354554E
"""RFC 5389 §15.5 — the constant FINGERPRINT XORs the CRC32 with."""

BINDING_METHOD = 0x001
"""The only STUN method ICE uses on the media port."""


class StunClass(IntEnum):
    """The four STUN message classes (the top bits of the 14-bit message type)."""

    REQUEST = 0b00
    INDICATION = 0b01
    SUCCESS_RESPONSE = 0b10
    ERROR_RESPONSE = 0b11


@dataclass(frozen=True, slots=True)
class Username:
    """`USERNAME` = `<remote-ufrag>:<local-ufrag>` on an inbound check."""

    value: str


@dataclass(frozen=True, slots=True)
class XorMappedAddress:
    """`XOR-MAPPED-ADDRESS` — the reflexive address, obfuscated by cookie ^ txid."""

    address: Address


@dataclass(frozen=True, slots=True)
class MessageIntegrity:
    """`MESSAGE-INTEGRITY` — a 20-byte HMAC-SHA1 keyed by the ICE `pwd`."""

    mac: bytes


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """`FINGERPRINT` — `CRC32(message) ^ FINGERPRINT_XOR`."""

    crc: int


@dataclass(frozen=True, slots=True)
class Priority:
    """`PRIORITY` of the candidate the peer is checking from."""

    value: int


@dataclass(frozen=True, slots=True)
class UseCandidate:
    """`USE-CANDIDATE` — the controlling side asking to nominate this pair.

    A flag attribute: zero-length value, so the type's *presence* is the whole
    message. It is also the single most consequential attribute in this file,
    because acting on it is what writes an entry into the SFU's routing table.
    """


@dataclass(frozen=True, slots=True)
class IceControlling:
    """`ICE-CONTROLLING` tie-breaker — set by the side that nominates."""

    tiebreaker: int


@dataclass(frozen=True, slots=True)
class IceControlled:
    """`ICE-CONTROLLED` tie-breaker — set by the side that follows."""

    tiebreaker: int


@dataclass(frozen=True, slots=True)
class UnknownAttribute:
    """Any attribute type this SFU does not model.

    Kept rather than discarded so a parse -> encode round-trip is genuinely the
    identity the SPEC asks it to be. Note that RFC 5389 splits these in two by
    *number*: comprehension-required types (below 0x8000) that a strict peer
    must reject, and comprehension-optional ones it may ignore. Deciding which
    behaviour you implement, and saying so in the design doc, is part of V1.
    """

    attr_type: int
    value: bytes


type StunAttribute = (
    Username
    | XorMappedAddress
    | MessageIntegrity
    | Fingerprint
    | Priority
    | UseCandidate
    | IceControlling
    | IceControlled
    | UnknownAttribute
)
"""A closed union, so `match` over it can be checked for exhaustiveness.

This is the Python shape of what Rust spelled as an `enum`: several small frozen
dataclasses and an alias naming the set. `frozen=True` buys `__eq__` and
`__hash__` for free, which is exactly what the round-trip criterion needs — you
compare parsed attribute lists with `==` and mean it.
"""


@dataclass(frozen=True, slots=True)
class StunMessage:
    """A parsed STUN message."""

    stun_class: StunClass
    method: int
    """12-bit method — `BINDING_METHOD` is the only one ICE uses here."""

    transaction_id: bytes
    """Exactly 12 bytes. Plain `bytes`, not a wrapper type: it is an opaque
    token that is only ever compared and echoed, and a class around it would be
    ceremony with no invariant to enforce."""

    attributes: tuple[StunAttribute, ...] = ()

    @classmethod
    def parse(cls, data: bytes) -> StunMessage:
        """Parse a STUN message off a datagram, or raise a `MediaError`.

        TODO(V1): check `len(data)` covers the 20-byte header and raise
        `TruncatedError(need=STUN_HEADER_LEN, got=len(data))` if not; check the
        top two bits of `data[0]` are zero and the cookie at `data[4:8]` is
        `STUN_MAGIC_COOKIE`, else `BadMagicError`. Split the 14-bit message type
        into class and method (the class bits are *interleaved* into the type,
        not a prefix — RFC 5389 §6 has the layout, and getting it wrong is the
        classic first bug here). Then walk the attribute TLVs from offset 20: a
        2-byte type, a 2-byte length, `length` bytes of value, then padding up to
        a 4-byte boundary — **and the padding is not counted in `length`**.

        Range-check every length against `len(data)` *before* slicing and raise
        `MalformedError` when one overruns: in Python an over-long length does
        not fault, it silently yields a short slice. Un-XOR an
        `XOR-MAPPED-ADDRESS` with the cookie (and, for IPv6, the cookie followed
        by the transaction id).

        A `dict` mapping attribute type -> a small parse function is a clean
        shape for the walk; an `IntEnum` of the type codes from RFC 5389 §18.2
        keeps the magic numbers in one place.
        """
        raise NotImplementedError("V1: decode class/method + txid, walk the attribute TLVs")

    def encode(self, integrity_key: bytes | None = None) -> bytes:
        """Serialize to bytes, appending MESSAGE-INTEGRITY then FINGERPRINT.

        TODO(V1): write the 20-byte header (the type packed back from class +
        method, a provisional length, the cookie, the txid) then each attribute
        TLV, padded to a 4-byte boundary.

        The ordering is the whole game, and both trailing attributes are special
        in the same way — each is computed over a message whose **length field
        already counts the attribute about to be appended**:

        * MESSAGE-INTEGRITY (when `integrity_key` is given): set the length as
          if the 24-byte integrity attribute were already there, HMAC *that*,
          then append it. See `message_integrity`.
        * FINGERPRINT: same trick, over everything including the integrity
          attribute, with the length covering the 8 bytes FINGERPRINT itself
          adds.

        Build into a `bytearray` and patch the length with `struct.pack_into`
        rather than rebuilding the buffer — you will be rewriting that field
        three times and each rewrite is two bytes at offset 2.
        """
        raise NotImplementedError("V1: serialize header + attributes, then integrity, then crc")


def message_integrity(message: bytes, key: bytes) -> bytes:
    """The STUN MESSAGE-INTEGRITY MAC: HMAC-SHA1 of `message` keyed by the ICE `pwd`.

    `message` must already carry a length field that includes the 24-byte
    integrity attribute which will follow it.

    TODO(V1): `hmac.new(key, message, hashlib.sha1).digest()` — 20 bytes. This
    is the check that proves a Binding request really came from the peer holding
    the shared `pwd`, and it is the reason an open UDP port is not an open door.
    Verify with `hmac.compare_digest`, never `==` (see the module docstring).
    """
    raise NotImplementedError("V1: HMAC-SHA1(message, key) -> 20 bytes")


def fingerprint(message: bytes) -> int:
    """The STUN FINGERPRINT: `CRC32(message) ^ FINGERPRINT_XOR`.

    TODO(V1): `zlib.crc32(message) ^ FINGERPRINT_XOR`, over everything up to
    (not including) the fingerprint attribute. Cheap insurance that lets a
    receiver reject a non-STUN packet that merely looked like one on a muxed
    port — which, given `wire.classify` decides that from a single byte, is a
    real possibility rather than a theoretical one.
    """
    raise NotImplementedError("V1: crc32(message) ^ FINGERPRINT_XOR")


@dataclass(frozen=True, slots=True)
class IceResult:
    """What the SFU should do after handling one inbound STUN message.

    One result object with two optional fields rather than the three-variant sum
    type the Rust used, because the two things the caller does with it are
    independent: it sends `response` if there is one, and it updates the routing
    table if `nominated` is set. A nomination arrives *together with* the
    response that must still be sent, so a sum type would have had to grow a
    fourth variant meaning "both" the moment it met the real protocol.
    """

    response: bytes | None = None
    """An encoded Binding success response to send back to the source address."""

    nominated: Address | None = None
    """Set when this check nominated a pair — the peer's media path is now here."""


class IceAgent:
    """Per-peer ICE state: the SFU's local credentials, and the address that won.

    An **ICE-lite** agent. It validates and answers the browser's connectivity
    checks and records the nominated pair; it never gathers candidates and never
    sends a check of its own.
    """

    __slots__ = ("_nominated", "local_pwd", "local_ufrag", "remote_ufrag")

    def __init__(self, local_ufrag: str, local_pwd: str, remote_ufrag: str) -> None:
        self.local_ufrag = local_ufrag
        self.local_pwd = local_pwd
        self.remote_ufrag = remote_ufrag
        self._nominated: Address | None = None

    @property
    def peer(self) -> Address | None:
        """The source address that won ICE nomination — where media flows."""
        return self._nominated

    def handle(self, message: StunMessage, source: Address) -> IceResult:
        """Handle one inbound STUN message from `source`.

        TODO(V1): for a **Binding request**, in this order and no other:

        1. Check `USERNAME` is exactly `f"{self.local_ufrag}:{self.remote_ufrag}"`.
        2. Check `MESSAGE-INTEGRITY` against `self.local_pwd`, with
           `hmac.compare_digest`. Raise `IntegrityError` if it fails — an
           unauthenticated check must never nominate a path, and *that* is why
           the order matters: every side effect below happens after this line.
        3. Build a Binding success response echoing `message.transaction_id`,
           carrying `XorMappedAddress(source)`, signed with `local_pwd` and
           fingerprinted, and return it as `IceResult.response`.
        4. If the request carried `UseCandidate`, also record `source` in
           `self._nominated` and return it as `IceResult.nominated`.

        Anything else — a response to a check this ICE-lite agent never sent, an
        indication, a stray message — is a bare `IceResult()`: nothing to send,
        nothing to change.

        Note that the credentials are asymmetric, and mixing them up produces a
        agent that works against your own test client and fails against every
        browser: the USERNAME a peer *sends* you reads `<local>:<remote>` from
        your point of view, and the response you sign uses your **local** pwd.
        """
        raise NotImplementedError(
            "V1: authenticate the request, answer it, nominate on USE-CANDIDATE"
        )
