"""V4 - The peer wire protocol: the raw-TCP conversation between two peers.
`src/bittorrent/peer.py`.

Once you have a peer's address (V3) you open a TCP connection and speak the wire
protocol directly. There is no HTTP to hide behind and no framing you did not
write. Two phases:

1. **Handshake** - a fixed 68 bytes::

       <19><"BitTorrent protocol"><8 reserved bytes><20-byte infohash><20-byte peer id>

   If the peer's infohash is not the one you dialed for, you hang up before
   exchanging a single message.

2. **Messages** - a stream of `<4-byte big-endian length><1-byte id><payload>`
   frames. A length of `0` is a keep-alive and carries no id at all. The core
   ids are `choke(0)`, `unchoke(1)`, `interested(2)`, `not_interested(3)`,
   `have(4)`, `bitfield(5)`, `request(6)`, `piece(7)`, `cancel(8)`.

Two things make this a real exercise rather than a serialization chore. First,
**framing**: TCP is a byte stream, so a message may arrive split across reads,
or two may arrive in one read, and only the length prefix tells you where one
ends. Second, the **choke/interest state machine**: each side tracks four
booleans and nothing flows until they line up. And underneath both: never trust
a peer. Every length is a claim, and a claim gets checked before it gets
allocated for.

*Concept to internalize:* turning a byte stream into messages, the
choke/interest state machine, and "bound before you allocate" as a habit.

## Why the messages are dataclasses in a union

Rust used one `enum Message` with payloads because it had no alternative.
Python's own union plus `match` is the same idea in the language's own grammar::

    match message:
        case Piece(index=i, begin=b, block=data): ...
        case Have(index=i): ...
        case Unknown():     pass          # forward compatibility, see below

The alternative — one `Message` class with an `id` and an optional payload —
type-checks worse (every field is `| None` and every reader re-proves which
combination is live) and reads worse. This is not Rust-in-Python; it is the
shape Python grew `match` for.

`Unknown` is not padding. The horizontal checklist grades that an unrecognized
message id is **ignored, not fatal**: peers negotiate extensions (BEP 10's
extended messages, the fast extension's `have_all`) and one you do not implement
must not end the session. A decoder that raises on an unknown id is a client
that drops modern peers, so the unknown case is a *value* you can return and
ignore rather than an exception you have to remember not to raise. The same
applies to the 8 reserved handshake bytes: they carry capability flags, they are
routinely non-zero, and rejecting a handshake because of them is the same bug in
the other phase.

## The bit-order trap in `bitfield`

Piece `i` lives in byte `i // 8`, at the bit `0x80 >> (i % 8)` - **high bit
first**. Every from-scratch client gets this backwards once, and the symptom is
not an error: you download the wrong pieces, they fail their SHA-1, and you
conclude your hashing is broken. BEP 3 also requires the spare bits in the final
byte to be **zero**, so a peer that sets them is malformed and a bitfield whose
length is not `ceil(piece_count / 8)` is a peer to drop.

## Reading from an `asyncio.StreamReader`

`reader.read(n)` returns *what has arrived*, up to `n` - it is not
`readexactly`, and a short read is the normal case rather than an error.
`reader.readexactly(n)` waits for exactly `n` and raises
`asyncio.IncompleteReadError` if the peer closes first, which is the clean
end-of-connection signal and needs catching rather than escaping.

And the one that matters for the security criterion: `readexactly` **buffers
whatever you ask for**. `await reader.readexactly(0xFFFFFFFF)` does not fail
fast, it starts allocating. The cap has to be checked against the number in the
length prefix, before the call — checking how many bytes have actually arrived
is checking the wrong thing, because a hostile peer sends the 4-byte header and
then nothing at all.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum

from .types import HASH_LEN, InfoHash, PeerId

__all__ = [
    "BLOCK_SIZE",
    "HANDSHAKE_LEN",
    "PROTOCOL",
    "RESERVED_LEN",
    "Bitfield",
    "Cancel",
    "Choke",
    "Handshake",
    "Have",
    "Interested",
    "KeepAlive",
    "Message",
    "MessageId",
    "NotInterested",
    "PeerState",
    "Piece",
    "Request",
    "Unchoke",
    "Unknown",
    "decode_message",
    "encode_message",
    "pack_bitfield",
    "read_handshake",
    "read_message",
    "unpack_bitfield",
]

PROTOCOL = b"BitTorrent protocol"
"""The fixed 19-byte protocol string (`pstr`) in the handshake."""

RESERVED_LEN = 8
"""The reserved bytes between the protocol string and the infohash.

Extension flags live here — BEP 10 sets a bit, DHT sets another, the fast
extension a third. They are frequently non-zero against real clients and must
never be a reason to reject a handshake."""

HANDSHAKE_LEN = 1 + len(PROTOCOL) + RESERVED_LEN + HASH_LEN + HASH_LEN
"""68, and always 68. The one frame in this protocol with no length prefix,
because both ends already know how long it is."""

BLOCK_SIZE = 16 * 1024
"""The block size every client requests and every client expects, and a
*protocol constant* rather than a tunable — a peer asking for more is refused by
convention, so "tuning" it is a way to stop interoperating. This is why it lives
here and not in `config.py`."""


class MessageId(IntEnum):
    """Peer message ids. A keep-alive has no id — it is a zero length."""

    CHOKE = 0
    UNCHOKE = 1
    INTERESTED = 2
    NOT_INTERESTED = 3
    HAVE = 4
    BITFIELD = 5
    REQUEST = 6
    PIECE = 7
    CANCEL = 8


@dataclass(frozen=True, slots=True)
class KeepAlive:
    """The zero-length frame. Sent every couple of minutes so an idle connection
    is not reaped by a NAT that has stopped believing in it."""


@dataclass(frozen=True, slots=True)
class Choke:
    """ "I will not answer your requests." The default state of a new
    connection."""


@dataclass(frozen=True, slots=True)
class Unchoke:
    """ "You may request from me now." The seeder's scarce resource (V6)."""


@dataclass(frozen=True, slots=True)
class Interested:
    """ "You have pieces I want." Says nothing about whether you will get
    them."""


@dataclass(frozen=True, slots=True)
class NotInterested:
    """ "You have nothing I need" — usually because you finished."""


@dataclass(frozen=True, slots=True)
class Have:
    """The peer just completed and verified a piece."""

    index: int


@dataclass(frozen=True, slots=True)
class Bitfield:
    """Everything the peer has, as packed bits, sent once right after the
    handshake.

    Kept as raw bytes rather than unpacked at construction because validating it
    needs the piece count, which this layer does not know — see
    `unpack_bitfield`.
    """

    raw: bytes


@dataclass(frozen=True, slots=True)
class Request:
    """ "Send me `length` bytes of piece `index` starting at `begin`.\""""

    index: int
    begin: int
    length: int


@dataclass(frozen=True, slots=True)
class Piece:
    """A block of data. The only message that carries payload worth having, and
    the only one whose size is not trivially bounded."""

    index: int
    begin: int
    block: bytes


@dataclass(frozen=True, slots=True)
class Cancel:
    """ "Never mind about that request." Endgame's other half (V5): you ask
    several peers for the same block and cancel the losers."""

    index: int
    begin: int
    length: int


@dataclass(frozen=True, slots=True)
class Unknown:
    """A message id this client does not implement.

    A value, not an error — see the module docstring. Keeping the body means a
    session can log what it skipped, which is how you notice that the peer has
    been trying to speak an extension to you for ten minutes.
    """

    id: int
    body: bytes


type Message = (
    KeepAlive
    | Choke
    | Unchoke
    | Interested
    | NotInterested
    | Have
    | Bitfield
    | Request
    | Piece
    | Cancel
    | Unknown
)
"""Everything that can come off the wire, as one type `match` can exhaust."""


@dataclass(frozen=True, slots=True)
class Handshake:
    """The 68-byte opening frame, parsed."""

    info_hash: InfoHash
    peer_id: PeerId
    reserved: bytes = b"\x00" * RESERVED_LEN
    """The peer's capability flags. Kept rather than discarded so a session can
    tell whether the peer speaks the extension protocol — and, more immediately,
    so nothing is tempted to reject a handshake for having them set."""

    def encode(self) -> bytes:
        """Serialize to the 68 bytes on the wire.

        TODO(V4): `bytes([19]) + PROTOCOL + reserved + info_hash.raw +
        peer_id.raw`. The length byte is 19 and the string is 19 bytes; writing
        `len(PROTOCOL)` rather than a literal `19` is how those two stay true
        together.
        """
        raise NotImplementedError("V4: build the 68-byte handshake")

    @classmethod
    def decode(cls, raw: bytes) -> Handshake:
        """Parse and validate 68 received bytes, raising `PeerError` if they are
        not a handshake.

        TODO(V4): check the length is exactly `HANDSHAKE_LEN`, the first byte is
        19, and the next 19 bytes are `PROTOCOL`; then slice out the reserved
        bytes, the infohash and the peer id.

        **Do not validate the reserved bytes** — they carry extension flags and
        are routinely non-zero. **Do not compare the infohash here either**: this
        function knows what the peer *said*, not what you dialed for. The caller
        holds the expected infohash and the mismatch check is theirs, which keeps
        this a pure parse and puts the security decision at the layer that has
        the information to make it.
        """
        raise NotImplementedError("V4: validate pstrlen/pstr, extract infohash + peer id")


def encode_message(message: Message) -> bytes:
    """Frame a message: 4-byte big-endian length, then id and payload.

    TODO(V4): build each arm. `KeepAlive` is exactly `b"\\x00\\x00\\x00\\x00"` —
    a length of zero and nothing after it. Every other message's length counts
    the id byte *plus* the payload, which is the off-by-one worth getting right
    once: a `have` is 5 on the wire, not 4.

    `int.to_bytes(4, "big")` is the length prefix and `struct.pack(">B3I", ...)`
    is a `request`; either is fine, and mixing them arbitrarily within one
    function is how the byte order eventually differs between two arms.

    Raising on `Unknown` is a defensible choice — you cannot re-emit a message
    you did not understand and should not pretend to — but decide it explicitly
    rather than discovering it when a proxy round-trips one.
    """
    raise NotImplementedError("V4: frame this message (4-byte BE length + id + payload)")


def decode_message(payload: bytes) -> Message:
    """Turn one **deframed** payload (the id byte and everything after it) into a
    `Message`.

    The length prefix is already consumed and the empty payload is already a
    `KeepAlive` by the time you get here — this function's contract starts at the
    id byte, so it can be tested with hand-built `bytes` and no socket at all.

    TODO(V4): dispatch on `payload[0]` and slice the fixed-width fields out of
    the rest. Every arm has an exact expected size and checking it is the point:
    a `have` whose body is not 4 bytes, a `request` whose body is not 12, a
    `piece` whose body is under 8 — each is a `PeerError`, never an index past
    the end of a slice.

    An id that is not in `MessageId` returns `Unknown(id, body)`. That is the
    forward-compatibility criterion, and it is one line: raising instead is the
    bug the criterion exists to catch.

    Note that slicing cannot save you here — `payload[1:5]` on a 3-byte payload
    returns 2 bytes rather than raising, and `int.from_bytes` will happily turn
    them into a plausible-looking small number. Length checks are explicit or
    they do not happen.
    """
    raise NotImplementedError("V4: id + body -> Message, with bounds checks")


async def read_message(reader: object, *, max_bytes: int) -> Message:
    """Read exactly one message off a stream, reassembling across TCP reads.

    `reader` is an `asyncio.StreamReader`; it is typed loosely here only so the
    scaffold does not commit you to a transport before you have chosen one (a
    `Protocol` with `readexactly` is the tighter type once you have).

    TODO(V4): `await reader.readexactly(4)` for the length. Zero is a
    `KeepAlive`. Otherwise **compare the length against `max_bytes` before
    reading anything**, raise `PeerError` if it is over, then
    `readexactly(length)` and hand the result to `decode_message`.

    That ordering is the whole security criterion. `readexactly` will buffer
    whatever number you give it, so a peer's `0xFFFFFFFF` becomes a 4 GiB
    allocation attempt if the check comes second — and it costs one comparison
    to be a rejection instead.

    Catch `asyncio.IncompleteReadError` and turn it into something the session
    loop can treat as a clean disconnect. A peer closing mid-frame is ordinary,
    not exceptional, and it should not be logged as a protocol violation.

    This function is where "TCP is a stream, not a message queue" actually bites,
    and `readexactly` is what makes it look easy — the framing test that feeds
    the bytes **one at a time** is the proof that you leaned on it rather than
    on the shape of your reads.
    """
    raise NotImplementedError("V4: frame one message off the stream (length, cap, readexactly)")


async def read_handshake(reader: object) -> Handshake:
    """Read and parse the 68-byte opening frame.

    TODO(V4): `readexactly(HANDSHAKE_LEN)` and hand it to `Handshake.decode`.
    Separate from `read_message` because the handshake is the one frame with no
    length prefix — reading it with the message path would consume its first
    four bytes as a length and get a very confusing number.
    """
    raise NotImplementedError("V4: read the 68-byte handshake off the stream")


def unpack_bitfield(raw: bytes, piece_count: int) -> set[int]:
    """Turn a packed bitfield into the set of piece indices the peer has.

    A `set[int]` rather than a list of bools because every consumer asks set
    questions: "does this peer have piece 7" is `7 in peer.has`, availability
    counting across peers is a `Counter` update, and rarest-first (V5) is a
    `min` over the pieces you are missing. A `list[bool]` would answer all three
    more slowly and read worse doing it.

    TODO(V4): piece `i` is set when `raw[i // 8] & (0x80 >> (i % 8))` — **high
    bit first**, which is the trap in the module docstring.

    Validate rather than trust, on two counts, both of which are `PeerError`:
    the length must be exactly `ceil(piece_count / 8)` (`-(-piece_count // 8)`),
    and the spare bits past `piece_count` in the final byte must be **zero**
    (BEP 3 requires it, and a peer setting them is claiming pieces the torrent
    does not have).
    """
    raise NotImplementedError("V4: unpack a bitfield, high bit first, into piece indices")


def pack_bitfield(indices: Iterable[int], piece_count: int) -> bytes:
    """Pack piece indices into the wire bitfield the seeder sends (V6).

    TODO(V4): the exact inverse of `unpack_bitfield`, into a
    `bytearray(-(-piece_count // 8))`, setting `buf[i // 8] |= 0x80 >> (i % 8)`.
    Leave the spare bits in the final byte at zero — you are on the other side of
    the check you just wrote, and a strict peer like `transmission` will drop
    you for setting them.

    Writing both directions in one place is what makes the round-trip property
    (`unpack(pack(s)) == s`) a one-line test, and that test is worth more than
    either function's own.
    """
    raise NotImplementedError("V4: pack piece indices into a bitfield, high bit first")


@dataclass(slots=True)
class PeerState:
    """The four flags each side tracks about one connection, plus what the peer
    has.

    Both ends start out choking and uninterested, which means **nothing flows on
    a fresh connection until two messages have been exchanged**. That is not an
    accident of the protocol, it is the protocol: data moves only when one side
    is interested *and* the other has unchoked it, and every stalled
    from-scratch client is a state machine that never sent one of the two.

    Mutable on purpose — this is per-connection state owned by the one coroutine
    serving that connection, which is why nothing here needs a lock.
    """

    am_choking: bool = True
    """We are refusing this peer's requests. The seeder's choke algorithm (V6)
    is precisely the policy that decides when this becomes `False`."""

    am_interested: bool = False
    """We want something this peer has. Derived from their bitfield against our
    own missing pieces — sent when it changes, not on a timer."""

    peer_choking: bool = True
    """They are refusing ours. While this is `True`, sending requests is wasted
    bandwidth: they are discarded."""

    peer_interested: bool = False
    """They want something we have. The input to our choke decision."""

    has: set[int] = field(default_factory=set[int])
    """Piece indices the peer has advertised, via `bitfield` then `have`."""

    def apply(self, message: Message, piece_count: int) -> None:
        """Fold an incoming message into this connection's state.

        TODO(V4): flip the matching flag for `Choke` / `Unchoke` / `Interested`
        / `NotInterested`, add the index on `Have`, and replace `has` with
        `unpack_bitfield(...)` on `Bitfield`. Everything else — `Request`,
        `Piece`, `Cancel`, `KeepAlive`, `Unknown` — changes no state here and is
        handled by the session that owns this object.

        This is the state the download loop (V5) reads to decide what to ask
        for, and the seeder (V6) reads to decide whom to unchoke. Keeping it a
        plain synchronous fold over one message — no I/O, no `await` — is what
        makes it testable without a socket, which is how the "flips the right
        flag" proof stays a unit test.

        Two details that are decisions rather than transcription. A `Have` for an
        index at or past `piece_count` is a lying peer and is worth a `PeerError`
        rather than a silently-ignored set entry. And a second `Bitfield`
        arriving mid-session is out of spec — deciding whether that is a drop or
        a shrug is exactly the "never trust a peer" judgement, and either answer
        is fine as long as it is one you made.
        """
        raise NotImplementedError("V4: advance the choke/interest state machine")
