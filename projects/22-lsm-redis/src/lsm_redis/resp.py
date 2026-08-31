"""V1 — RESP: the wire real `redis-cli` speaks. `src/lsm_redis/resp.py`.

Redis clients talk **RESP** (REdis Serialization Protocol) over a raw TCP byte
stream. There is no HTTP, no envelope around the whole request — just typed
values back to back, each self-describing by its first byte::

    +OK\\r\\n                        simple string
    -ERR unknown command\\r\\n       error
    :42\\r\\n                        integer
    $5\\r\\nhello\\r\\n                bulk string (length-prefixed bytes)
    $-1\\r\\n                        null bulk string  (a GET miss -> nil)
    *2\\r\\n$3\\r\\nGET\\r\\n$1\\r\\nk\\r\\n array (how clients send a command)

A client sends every command as an **array of bulk strings**. Your job in V1 is
the codec: pull one complete command off a byte buffer that may hold a fraction
of a frame *or* several pipelined ones, and serialize a reply back. Two
properties make this the hard, interesting part:

1. **Streaming / partial frames.** TCP hands you arbitrary chunks.
   `parse_command` must return `None` (need more bytes) *without consuming* a
   partial frame, and only advance the buffer once a whole command is present.
2. **Pipelining.** A client may fire many commands before reading any reply, so
   one read can contain several. The connection loop drains them in a `while`.

*Concept to internalize:* framing a request/response protocol over a raw stream
— length-prefix vs delimiter, why "read a line" is not enough, and how
pipelining falls out for free once parse and serialize are buffer-oriented.

## Why `Reply` is plain Python types

Rust modelled the reply side as an `enum Resp` because it had to. Python does
not: the six RESP reply types map onto types you already have, and the mapping
is not a cute trick — it is the same distinction the protocol is making.

===============  =============  ==========================================
RESP             Python         why
===============  =============  ==========================================
``+OK``          ``str``        a status word: ASCII, short, never binary
``:42``          ``int``
``$5 hello``     ``bytes``      a *stored value*: arbitrary binary
``$-1``          ``None``       the miss `redis-cli` renders as ``(nil)``
``*2 …``         ``list``
``-ERR …``       ``Error``      the one case with no natural Python type
===============  =============  ==========================================

`str` for a status and `bytes` for a value is exactly the "binary-safe"
criterion expressed in the type system: a value that happens to contain
``\\r\\n`` or a NUL is `bytes` and survives; a status line is `str` and is yours,
so it never contains either. If you ever find yourself calling `.decode()` on a
key or a value to make something type-check, that is the bug the criterion is
about, arriving early enough to fix cheaply.

**The `bool` trap.** In Python `bool` is a subclass of `int`, so a `match` arm
of `case int()` catches `True` and encodes it as `:1`. That is even *almost*
right for redis, which has no boolean type — but it means a stray `True`
serializes silently instead of failing loudly. Match `bool` first, or do not
accept it into `Reply` at all.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Command", "Error", "Reply", "encode", "parse_command"]

CRLF = b"\r\n"


@dataclass(frozen=True, slots=True)
class Error:
    """A RESP error line: ``-<message>\\r\\n``.

    A wrapper class rather than a bare `str` because a reply of `str` already
    means a *simple string* — and the difference between `+ERR nope` and
    `-ERR nope` on the wire is the difference between a client printing "ERR
    nope" as a successful result and a client raising. `message` carries no CRLF
    of its own; the error word convention lives in `errors.py`.
    """

    message: str


type Reply = str | int | bytes | None | Error | list[Reply]
"""What a command handler returns, as the table in the module docstring."""

type Command = list[bytes]
"""A parsed client command: raw bulk-string arguments, e.g.
``[b"SET", b"user:1", b"alice"]``. Element 0 is the command name.

Bytes, not `str`, all the way through. A key may be any byte sequence a client
chooses, and decoding it to compare or store it is both lossy and a needless
copy on the hot path."""


def encode(value: Reply, out: bytearray) -> None:
    """Append the RESP encoding of `value` to `out`.

    Writing into a caller-owned buffer rather than returning a fresh `bytes` is
    what lets the connection loop batch a whole pipeline of replies into a
    single `write` — which is most of why pipelining is fast, and is worth more
    than any micro-optimization inside this function.

    TODO(V1): emit the type marker, the length prefix where one applies, the
    payload, and each CRLF, for every arm of `Reply`. `bytearray.extend` is the
    append; `b"%d" % n` or `str(n).encode()` turns a length into ASCII digits.
    Arrays recurse. Watch the `bool` trap in the module docstring, and remember
    that a `str` reply must be encoded ASCII-safe — a status containing a
    newline would forge a frame boundary, which is the protocol's version of a
    header-injection bug.
    """
    raise NotImplementedError("V1: serialize a RESP reply into `out`")


def parse_command(buf: bytearray, max_bulk_len: int) -> Command | None:
    """Try to take **one** complete command off the front of `buf`.

    The contract — get this right and both pipelining and partial reads work:

    * a whole command is present -> return it, **and remove its bytes from
      `buf`**;
    * only a partial frame is present -> return `None` and **leave `buf`
      exactly as it was**, so the connection loop can read more and call again;
    * the framing is malformed (bad type byte, non-numeric length, a declared
      length above `max_bulk_len`) -> raise `ProtocolError`.

    `max_bulk_len` is redis's `proto-max-bulk-len`. It must be checked against
    the number in the header *before* you reserve anything for it: the whole
    point is that `$1000000000000\\r\\n` costs you a comparison, not a
    `MemoryError`. Checking how many bytes have actually arrived is checking the
    wrong thing — an attacker sends the header and then nothing.

    TODO(V1): decode the array-of-bulk-strings form every client uses. Sketch:
    find the first CRLF with `buf.find(CRLF)`; if there is none, you are not
    done — return `None`. Read `*N`, then loop N times reading `$len` + `len`
    bytes + CRLF, returning `None` the moment the buffer runs out. Only when the
    whole command is in hand do you touch `buf`. Stretch: accept an *inline*
    command (a bare ``PING\\r\\n`` typed into netcat), which is a second framing
    in the same stream and forces you to decide from one byte which you are in.

    **Two Python-specific traps.**

    Do not mutate `buf` as you go and unwind on failure — parse against an
    offset, and apply `del buf[:offset]` exactly once, at the end, on success.
    A half-consumed buffer after a partial frame is the bug this contract
    exists to prevent, and it is invisible until a client sends a large value
    in small packets.

    `del buf[:n]` is a memmove of everything after `n`. Draining a pipeline of
    1000 small commands out of one buffer that way is quadratic, and it is the
    kind of quadratic that never shows up in a unit test and owns your p99
    under the boss fight's `-P 16` pipelining. The fix is an explicit cursor:
    parse from an offset, compact once per socket read instead of once per
    command. Design for that now — it changes this signature, and changing it
    later means changing every test.
    """
    raise NotImplementedError(
        "V1: decode one `*N $len …` command, advancing `buf` only when complete"
    )
