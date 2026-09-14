"""V1 — RTMP handshake + chunk-stream reader: parse the wire by hand.

Module: `src/live_ingest/rtmp.py`.

RTMP runs over raw TCP. A connection opens with a three-message **handshake**
(C0/C1 ↔ S0/S1/S2 ↔ C2, each side echoing the other's 1528 random bytes), after
which data flows as a **chunk stream**: every logical message is split into
chunks of at most a negotiated size (default 128 bytes), each prefixed by a
**basic header** (a 2-bit `fmt` + a chunk-stream id 1, 2 or 3 bytes wide) and —
for fmt 0/1/2 — a **message header** that is *delta-compressed* against the
previous chunk on the same chunk-stream id (fmt 3 repeats it entirely). This
module does the socket reads and reassembles chunks back into whole `Message`s;
the commands and codecs inside them are V2 and V3.

`ingest.py` calls `handshake` once per connection, then loops on
`ChunkStreamReader.read_message`. `docs/00-rtmp-chunk-stream.md` teaches the wire
format in full.

## Five Python details that decide whether this works

**`StreamReader.readexactly` is the right primitive — and it lies about
nothing.** It returns exactly `n` bytes or raises `asyncio.IncompleteReadError`
(whose `.partial` holds what did arrive) when the peer hangs up first. It never
returns short. That is the whole "truncated chunk ends the session cleanly"
criterion, provided you turn that exception into a `TruncatedError` at the
boundary instead of letting a stdlib exception leak up as if it were a bug.

**…but it allocates what you ask for.** `readexactly(n)` buffers until it has
`n` bytes, so a peer-declared `n` is a peer-chosen allocation. A 24-bit message
length caps at 16 MiB; a Set Chunk Size is 31 bits. Range-check every declared
length *before* the read that trusts it — that ordering is the security
criterion, not a detail of it.

**Big-endian is free; one field is not.** `int.from_bytes(b, "big")` reads the
24-bit timestamps and lengths directly (no `struct` code for 24 bits). The
message stream id in a fmt-0 header is the protocol's single **little-endian**
field — `int.from_bytes(b, "little")` — and it is a classic V1 bug.

**Accumulate into a `bytearray`, copy once.** A message reassembled from 1,600
chunks by `payload += chunk` on `bytes` is 1,600 allocations of a growing
buffer. `bytearray.extend` amortizes; `bytes(partial)` once the message is
complete is the single copy that makes `Message.payload` immutable.

**You can test this without a socket.** `asyncio.StreamReader()` is a plain
object: `reader.feed_data(chunk_bytes); reader.feed_eof()` and `read_message`
consumes it exactly as it would a connection. That is what makes a property
test over arbitrary bytes (`hypothesis`) cheap enough to run thousands of cases.

## The uvloop note

Production runs on **uvloop** (`uvicorn[standard]`, `loop="auto"`); pytest runs on
the stdlib loop. Streams (`start_server`, `StreamReader`, `StreamWriter`) behave
the same on both. The `loop.sock_*` family does **not** — uvloop does not
implement it — so never "optimize" this into a raw socket plus
`loop.sock_recv_into`. It would pass every test and fail in the container.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "EXTENDED_TIMESTAMP",
    "HANDSHAKE_SIZE",
    "MAX_CHUNK_SIZE",
    "MAX_MESSAGE_SIZE",
    "RTMP_VERSION",
    "ChunkStreamId",
    "ChunkStreamReader",
    "ChunkStreamState",
    "Message",
    "MessageStreamId",
    "MessageType",
    "handshake",
]

RTMP_VERSION = 0x03
"""The version byte in C0/S0. Plain RTMP; RTMPE (encrypted) would be 0x06."""

HANDSHAKE_SIZE = 1536
"""Each of C1/S1/C2/S2: `time(4) + zero-or-time2(4) + random(1528)`."""

DEFAULT_CHUNK_SIZE = 128
"""Chunk payload size until a Set Chunk Size message changes it."""

MAX_CHUNK_SIZE = 0x7FFF_FFFF
"""Set Chunk Size is 31 bits on the wire (the top bit must be zero)."""

EXTENDED_TIMESTAMP = 0xFF_FFFF
"""A 24-bit timestamp/delta of exactly this value means four more bytes follow."""

MAX_MESSAGE_SIZE = 16 * 1024 * 1024
"""Hard cap on a declared message length — the OOM guard. Generous for a 1080p
keyframe, and far below what 64 hostile chunk streams could otherwise claim."""


class MessageType(IntEnum):
    """RTMP message type ids this ingest cares about.

    An `IntEnum` so `message.type_id == MessageType.VIDEO` compares against the
    raw wire int with no conversion. Unknown ids stay plain `int` in `Message` —
    forward-compatible traffic must not crash the reader.
    """

    SET_CHUNK_SIZE = 1
    """Protocol control: new max chunk payload size (4-byte BE)."""
    ABORT = 2
    """Protocol control: discard the partial message on a chunk-stream id."""
    ACKNOWLEDGEMENT = 3
    USER_CONTROL = 4
    """Stream begin, ping, …"""
    WINDOW_ACK_SIZE = 5
    SET_PEER_BANDWIDTH = 6
    AUDIO = 8
    """FLV audio tag: AAC sequence header, then AAC frames."""
    VIDEO = 9
    """FLV video tag: AVC sequence header, then NALUs."""
    AMF0_DATA = 18
    """`@setDataFrame` / `onMetaData`."""
    AMF0_COMMAND = 20
    """`connect`, `createStream`, `publish`, `_result`, `onStatus`."""


class ChunkStreamId(IntEnum):
    """Conventional chunk-stream ids (the 1-byte basic-header form, 2..63)."""

    PROTOCOL_CONTROL = 2
    COMMAND = 3


class MessageStreamId(IntEnum):
    """Message stream ids — the little-endian field in a fmt-0 header."""

    NET_CONNECTION = 0
    """`connect`, `createStream` and their `_result` replies."""
    DEFAULT = 1
    """The first NetStream id `createStream` hands out: `publish`, media, `onStatus`."""


@dataclass(frozen=True, slots=True)
class Message:
    """One fully reassembled RTMP message."""

    type_id: int
    """Wire type id; compare against `MessageType`."""
    stream_id: int
    """Message stream id (see `MessageStreamId`)."""
    timestamp: int
    """Absolute milliseconds, reconstructed from deltas and extended timestamps."""
    payload: bytes
    """All of the message's chunks, concatenated. Never contains a chunk header."""


@dataclass(slots=True)
class ChunkStreamState:
    """What one chunk-stream id remembers so a fmt 1/2/3 chunk can inherit.

    The wire is undecodable without this: a fmt-3 chunk is *pure payload*, and
    every header field it implies comes from here. One instance per csid, in
    the reader's dict.
    """

    timestamp: int = 0
    """Absolute timestamp of the in-flight (or last) message, ms."""
    timestamp_delta: int = 0
    """Last delta — re-applied when a fmt-3 chunk starts a *new* message."""
    message_length: int = 0
    type_id: int = 0
    stream_id: int = 0
    extended_timestamp: bool = False
    """Whether the last fmt 0/1/2 header here used the extended field — a fmt 3
    that follows then carries (and you must consume) the four extra bytes too."""
    partial: bytearray = field(default_factory=bytearray)
    """The in-progress message. Non-empty ⇒ the next fmt-3 chunk is a
    continuation; empty ⇒ it starts a new message."""


async def handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Perform the RTMP **simple** handshake as the server (V1).

    C0 (1 byte, must be `RTMP_VERSION`) + C1 (`HANDSHAKE_SIZE` bytes) arrive;
    you answer S0 + S1 (your own time + 1528 random bytes) + S2 (an echo of C1);
    then C2 arrives, which must echo S1's random block.

    Raises `HandshakeError` on a wrong version or a bad C2 echo, and
    `TruncatedError` if the peer hangs up mid-block.

    TODO(V1): `readexactly` each block; build S1 with `os.urandom`; send
    S0+S1+S2 as **one** `writer.write` followed by `await writer.drain()` —
    a client that has sent C0+C1 is waiting on all three, and three small
    writes are three chances for Nagle to delay the last one. Compare C2 against
    S1 from the random offset (byte 8) onward: bytes 4–8 are the peer's read
    time, which legitimately differs. (The complex HMAC-digest handshake is a
    From-the-field item, not V1.)
    """
    raise NotImplementedError("V1: RTMP server handshake (C0/C1 <-> S0/S1/S2 <-> C2)")


class ChunkStreamReader:
    """Reassembles one connection's chunk stream into whole `Message`s.

    Holds the per-chunk-stream inheritance state and the current (negotiable)
    chunk size. One reader per connection, for the life of the connection —
    the state is the connection's, not the message's.
    """

    def __init__(self, *, max_message_size: int = MAX_MESSAGE_SIZE) -> None:
        self.max_message_size = max_message_size
        self._chunk_size = DEFAULT_CHUNK_SIZE
        self._streams: dict[int, ChunkStreamState] = {}

    @property
    def chunk_size(self) -> int:
        """Current max payload bytes per chunk."""
        return self._chunk_size

    def set_chunk_size(self, size: int) -> None:
        """Apply a Set Chunk Size — the reassembly boundary changes from now on.

        Clamped to `1..max_message_size`: a chunk can never usefully be larger
        than the largest message this reader will accept, so a publisher
        announcing `MAX_CHUNK_SIZE` cannot turn one chunk into an unbounded read.
        """
        self._chunk_size = max(1, min(size, self.max_message_size))

    async def read_message(self, reader: asyncio.StreamReader) -> Message:
        """Read chunks until one complete `Message` is reassembled (V1).

        TODO(V1): per chunk —
          1. basic header: `fmt` = top 2 bits; csid = low 6 bits, where `0`
             means one more byte (id = byte + 64) and `1` means two more,
             **little-endian** (id = value + 64);
          2. message header sized by fmt: 11 / 7 / 3 / 0 bytes (see
             `docs/00-rtmp-chunk-stream.md` §3.2 for which fields each carries);
          3. extended timestamp: four more bytes if the 24-bit field is
             `EXTENDED_TIMESTAMP` — or if this is fmt 3 on a csid whose last
             header was extended;
          4. update this csid's `ChunkStreamState`: fmt 0/1/2 apply new fields
             (an error if a message is still incomplete there); fmt 3 with an
             empty `partial` starts a new message and re-applies the delta; fmt
             3 with a non-empty one continues it;
          5. range-check `message_length` against `max_message_size` **before**
             reading payload, then `readexactly(min(chunk_size, remaining))` —
             the last chunk of a message is short, and reading a full chunk
             there swallows the next chunk's header;
          6. when `partial` reaches `message_length`, build the `Message`, and
             if it is a Set Chunk Size, apply it before returning so the very
             next chunk uses the new boundary.

        Raises `TruncatedError` (convert `IncompleteReadError`), `OversizedError`
        and `MalformedError` — never an `IndexError`, never an unbounded
        allocation. One exception to the conversion: a peer that closes cleanly
        *between* chunks is not a protocol error, it is a broadcast ending. An
        `IncompleteReadError` on a chunk's very first byte with nothing partial
        anywhere can propagate as-is — `ingest.py` counts any `EOFError` as a
        normal close. Decide, too, whether the csid dict is bounded: a hostile
        peer can mint 65,599 of them.
        """
        raise NotImplementedError(
            "V1: reassemble chunks (fmt 0-3 + extended ts + set-chunk-size) into a Message"
        )
