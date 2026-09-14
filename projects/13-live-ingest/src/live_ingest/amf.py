"""V2 (part 1) — AMF0: RTMP's command serialization.

Module: `src/live_ingest/amf.py`.

RTMP carries its control RPC — `connect`, `createStream`, `publish`, and the
`_result` / `onStatus` replies — as **AMF0**-encoded values inside type-20
command messages. AMF0 is a compact typed format: a 1-byte **type marker**, then
the value:

    0x00 number    IEEE-754 double, big-endian (8 bytes) — every number, ids too
    0x01 boolean   1 byte: zero is false, anything else true
    0x02 string    u16 big-endian length, then that many UTF-8 bytes
    0x03 object    <u16-len key><value> pairs, ended by an empty key + 0x09
    0x05 null      marker only

A command body is several values concatenated — name, transaction id, command
object (or null), arguments — which is why `decode` returns a list. Object
*keys* carry no marker (just length + bytes); only values do. That asymmetry is
what makes `00 00 09` an unambiguous terminator, and it trips everyone once.

Pure functions over `bytes`, no I/O, on purpose: the SPEC's proof is a property
test that `decode(encode(v)) == v` for any value, and that random bytes never
crash the decoder. `docs/01-amf0-and-the-publish-state-machine.md` decodes a
real `connect` by hand.

## AMF0 maps onto Python's own types — which is both the gift and the trap

There is no need for a tagged union of AMF0 values: Python already *has*
those five types, so a decoded value is just `float`, `bool`,
`str`, `dict` or `None`, and a reply is written as ordinary literals:

    encode("_result", 1.0, {"code": "NetConnection.Connect.Success"}, None)

Five consequences you have to handle deliberately:

**`bool` is a subclass of `int`.** `isinstance(True, int)` is `True`. An encoder
that checks for numbers first writes every boolean as a 0x00 double, and a
publisher reading `publish`'s reply sees `1.0` where it wanted `true`. Check
`bool` before any numeric check.

**The type checker lets `int` through where `float` is declared.** Pyright
accepts `encode("x", 1)` against an `AmfValue` that says `float`, because `int`
is implicitly assignable to `float` in Python's type system. At runtime the
value is an `int`, so the encoder has to accept one — and still write it as a
double, because AMF0 has no integer type.

**Equality will not catch a type bug.** `True == 1 == 1.0` in Python, so an
`assert decode(encode(v)) == v` round-trip test passes even when booleans come
back as numbers. Compare `type()` too. And `float("nan") != float("nan")`, so a
property test generating doubles must either exclude NaN or compare bits.

**A `dict` preserves insertion order.** AMF0 objects are ordered on the wire;
Python dicts keep that order, so a decode→encode round-trip can be byte-exact.
(A sorted map would reorder the keys and could not.)

**Recursion is an attack surface.** An object value that is an object that is
an object… decoded recursively reaches Python's recursion limit (~1,000 frames)
and raises `RecursionError` — not a `ProtocolError`, so it escapes the session's
handling as an unexpected crash. A few kilobytes of `03 00 01 61` from an
unauthenticated peer can do that. Bound the depth.
"""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "OBJECT_ENCODING_AMF0",
    "AmfObject",
    "AmfValue",
    "Marker",
    "decode",
    "encode",
]

type AmfValue = float | bool | str | None | dict[str, AmfValue]
"""One decoded AMF0 value. See the module docstring on `bool`/`int`/`float`."""

type AmfObject = dict[str, AmfValue]
"""An AMF0 object — a command object, an `onStatus` info object."""

OBJECT_ENCODING_AMF0 = 0.0
"""`objectEncoding` advertised in the `connect` reply: this ingest speaks AMF0
only (message types 18/20), never AMF3 (15/17)."""


class Marker(IntEnum):
    """AMF0 type markers for the value kinds a publish flow uses."""

    NUMBER = 0x00
    BOOLEAN = 0x01
    STRING = 0x02
    OBJECT = 0x03
    NULL = 0x05
    OBJECT_END = 0x09
    """Not a value: terminates an object, after a zero-length key."""


def decode(data: bytes) -> list[AmfValue]:
    """Decode the run of AMF0 values that makes up one command body (V2).

    `b""` decodes to `[]`. Raises `TruncatedError` when a declared length runs
    past the end, `MalformedError` for an unsupported marker, invalid UTF-8
    (convert the `UnicodeDecodeError`) or a missing object terminator.

    TODO(V2): walk `data` with an offset, one value at a time until it is
    exhausted. Build it on a helper that decodes **exactly one** value and
    returns it with the offset just past it — that one primitive serves both
    the top-level run and each object field's value, and it is what stops a
    nested value from swallowing its siblings or the `00 00 09`.
    `struct.unpack_from(">d", data, offset)` reads a number and raises
    `struct.error` on a short buffer; a string slice does not raise at all, so
    check its length first.
    """
    raise NotImplementedError("V2: decode a run of AMF0 values (number/boolean/string/object/null)")


def encode(*values: AmfValue) -> bytes:
    """Encode AMF0 values into a command body — the inverse of `decode` (V2).

    Used to build the `_result` and `onStatus` replies the session sends back.

    TODO(V2): marker then payload per value; objects as key/value pairs plus
    `00 00 09`. Mind the `bool`-before-number ordering and the `int` case (module
    docstring). A string longer than 65,535 UTF-8 bytes does not fit a u16
    length — that is AMF0's separate long-string marker, which you do not have
    to support, but you do have to refuse rather than silently truncate. Build
    into one `bytearray` and convert once.
    """
    raise NotImplementedError("V2: encode AMF0 values for a command reply (_result / onStatus)")
