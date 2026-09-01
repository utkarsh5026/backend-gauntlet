"""V1 - Bencode: the wire's data format. `src/bittorrent/bencode.py`.

Everything in BitTorrent is bencoded: the `.torrent` file, the tracker's reply,
the DHT. Four types, all length- or delimiter-framed::

    i42e            integer   (also `i-1e`; `i-0e` and leading zeros like `i03e` are illegal)
    4:spam          byte-string  (a length, a colon, then exactly that many raw bytes)
    l<values>e      list
    d<k><v>...e     dict      (keys are byte-strings, sorted as raw bytes)

The subtle constraint that makes this a *challenge* rather than a library call:
to compute the infohash (V2) you SHA-1 the **exact original bytes** of the
`info` dictionary. So your decoder has to let you recover a value's precise byte
span, and your encoder has to be **canonical** (sorted keys, no leading zeros,
no whitespace) — otherwise a decode->encode round-trip changes the bytes and
every infohash you produce is wrong. This is the single most common reason a
from-scratch BitTorrent client cannot talk to anyone.

*Concept to internalize:* canonical serialization, and why a content hash is a
hash of *bytes*, not of a parsed structure.

## Why there is no `Value` class here

Rust modelled this as an `enum Value { Int, Bytes, List, Dict }` because it had
to — a Rust `Vec` cannot hold four different types. Python's own types already
*are* that union, so `decode` returns plain `int`, `bytes`, `list` and `dict`
and `Bencode` is a recursive type alias over them. That is not a shortcut: a
wrapper class here would mean every consumer in `metainfo.py` and `tracker.py`
unwrapping before it could use anything, which is Rust-in-Python and buys
nothing the type alias does not already give pyright.

Keys and strings are `bytes`, never `str`, and that is load-bearing rather than
pedantic. The `pieces` field is raw SHA-1 digests concatenated — it is not valid
UTF-8, and a decoder that returns `str` corrupts it silently on the way in. If
you ever find yourself writing `.decode()` to make something type-check, that is
the binary-safety criterion failing early enough to fix cheaply.

## Five Python-specific traps, all of them real

**`int()` is far too permissive to be your integer parser.** All of
``int(b"0042")``, ``int(b" 42 ")``, ``int(b"+42")`` and — the one that surprises
everyone — ``int(b"4_2")`` succeed and give you 42. Bencode allows exactly one
spelling of each number, so the grammar has to be checked *before* the
conversion, not delegated to it.

**Slicing never raises.** ``b"ab"[0:100]`` is ``b"ab"``, not an error. So a
byte-string whose declared length overruns the buffer produces a short string
and a silent corruption instead of a failure. Truncation has to be an explicit
comparison; you cannot lean on an exception that will not come.

**`bool` is a subclass of `int`.** ``isinstance(True, int)`` is `True`, so a
`match` arm of ``case int()`` catches ``True`` and encodes it as ``i1e``. Match
`bool` first and reject it — bencode has no boolean, and silently encoding one
is how a `True` ends up in a torrent nobody can parse.

**Recursion is a denial of service.** ``b"l" * 100_000`` is a perfectly
well-formed prefix, and a recursive-descent decoder meets CPython's recursion
limit and raises `RecursionError` — which is not a `BencodeError`, is not
caught by callers, and takes down the request. "Never panic on malformed input"
means a depth cap you enforce, not a limit you hope to stay under.

**Building bytes with `+=` in a loop is quadratic.** A 400 MiB torrent's
`pieces` string is 20 bytes x 20000 pieces, and concatenating your way through
it copies the accumulated result every time. Append into a `bytearray` (or
collect into a list and `b"".join`) and it is linear.
"""

from __future__ import annotations

__all__ = [
    "MAX_DEPTH",
    "Bencode",
    "KeyPath",
    "decode",
    "decode_with_spans",
    "encode",
]

type Bencode = int | bytes | list[Bencode] | dict[bytes, Bencode]
"""A decoded bencode value, as the Python types it already maps onto.

Byte-strings and dict keys are `bytes` — piece hashes are raw SHA-1 digests and
are not text. See the module docstring on why this is not merely a style
preference."""

type KeyPath = tuple[bytes, ...]
"""Where a value sits in the decoded structure, as the dict keys walked to reach
it: `()` is the whole document, `(b"info",)` is the top-level `info` dict,
`(b"info", b"pieces")` the piece-hash string inside it.

Dict keys only. A path *through* a list is deliberately not expressible, because
nothing in BitTorrent needs the byte span of a list element and supporting it
would double the size of the span bookkeeping for no caller."""

MAX_DEPTH = 100
"""How deeply values may nest before decoding gives up.

A cap, not a limit anyone legitimately approaches: real torrents nest three or
four deep. It exists because `b"l" * 100_000` is a valid *prefix*, and the
alternative to rejecting it is a `RecursionError` — which the "rejects malformed
input, never panics" criterion does not accept, since callers catch
`BencodeError` and nothing catches that."""


def decode(data: bytes) -> Bencode:
    """Decode exactly one bencode value, which must consume **all** of `data`.

    Raises `BencodeError` on anything malformed: truncation, a leading zero
    (`i03e`), negative zero (`i-0e`), a byte-string length that overruns the
    buffer, unsorted or duplicated dict keys, trailing bytes after a complete
    value, or nesting past `MAX_DEPTH`. Never raises anything else, and never
    returns a partly-decoded value — a caller that catches `BencodeError` has
    caught everything this function does on bad input.

    TODO(V1): write the recursive-descent decoder, then reject trailing bytes at
    the top level. Sketch: dispatch on `data[offset]` — `i` integer, `d` dict,
    `l` list, an ASCII digit a byte-string. Return `(value, next_offset)` from an
    internal helper and let this function assert the offset landed exactly on
    `len(data)`.

    Be strict about the grammar rather than delegating to `int()`, and treat a
    declared length as a claim to check before you slice — both traps are in the
    module docstring, and both produce a decoder that "works" on every valid
    torrent while accepting things no other client would.

    Two details worth deciding once, here, rather than discovering in V2:
    indexing `data[i]` gives an `int` while slicing gives `bytes`, so
    `data[0] == 0x69` and `data[0:1] == b"i"` are both spellings of the same
    check; and dict keys must be verified **sorted as raw bytes and unique** on
    the way in, because that ordering is the same rule your encoder has to
    reproduce and checking it in one place means you only get it wrong once.
    """
    raise NotImplementedError("V1: decode one bencode value, rejecting malformed input")


def encode(value: Bencode) -> bytes:
    """Encode a value **canonically**: sorted dict keys, no leading zeros, no
    whitespace, nothing optional.

    Canonical means there is exactly one valid output for any input, which is
    what makes `encode(decode(x)) == x` hold for every well-formed `x` — and
    that identity is not a nicety, it is what V2's infohash rides on.

    TODO(V1): serialize each arm. Append into a `bytearray` rather than
    concatenating (see the quadratic trap above); sort dict items with
    `sorted(value.items())`, which on `bytes` keys is already the raw-byte order
    the spec wants. Match `bool` before `int` and reject it.

    A judgement call worth making explicitly: this is also where you decide what
    to do with a `str` key or value that reached you by accident. Encoding it as
    UTF-8 is the friendly choice and it is wrong — it means a mistake in
    `metainfo.py` produces a torrent that is subtly not the one you parsed,
    rather than an exception at the line that made it.
    """
    raise NotImplementedError("V1: canonical bencode encoding (sorted keys, no leading zeros)")


def decode_with_spans(data: bytes) -> tuple[Bencode, dict[KeyPath, slice]]:
    """Decode `data`, and record where every dict value *physically was*.

    Returns the decoded value plus a map from `KeyPath` to a `slice` into the
    original buffer, so V2 can hash the `info` dict's exact bytes without ever
    re-encoding them::

        value, spans = decode_with_spans(raw)
        info_bytes = raw[spans[(b"info",)]]        # the bytes, byte-for-byte
        info_hash = hashlib.sha1(info_bytes).digest()

    Why a `slice` and not a `(start, end)` pair: the only thing anyone does with
    the span is index the buffer with it, and `raw[span]` cannot get the argument
    order wrong the way `raw[end:start]` silently can.

    **This function is the reason V1 is a vertical.** Re-encoding the parsed
    `info` dict to hash it looks equivalent and is not: the producer of a
    `.torrent` may have written a key your encoder normalizes, or ordered
    something your encoder reorders, and then your infohash matches nobody and
    every tracker tells you it has never heard of this content. Hashing the
    original bytes is the only version that is correct by construction rather
    than by hoping every client agrees with you.

    TODO(V1): thread a byte offset and a `KeyPath` through the decoder so that
    each time you finish a dict *value* you record
    `spans[path + (key,)] = slice(value_start, value_end)`. The nesting matters
    — `(b"info",)` is what V2 needs, and getting `(b"info", b"pieces")` for free
    is what lets you check your own work against a hex dump.

    Consider whether this and `decode` should share one implementation with the
    span bookkeeping switched off, or stay separate. Sharing is less code and
    means the two can never disagree about what is valid; separate is faster on
    the hot path (tracker replies are decoded constantly and their spans are
    never wanted). Either is defensible — deciding on purpose is the point.
    """
    raise NotImplementedError("V1: decode and record each dict value's exact byte span")
