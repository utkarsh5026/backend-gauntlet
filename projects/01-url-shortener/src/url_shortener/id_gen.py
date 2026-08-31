"""V1 - Distributed, coordination-free ID generation (Snowflake-style).

GOAL: generate 64-bit, time-ordered, unique ids entirely in-process - no DB
sequence, no network round-trip - then base62-encode them into short slugs.

Classic Snowflake layout (the bit budget is tunable):

    [ 1 unused ][ 41 bits ms since epoch ][ 10 bits node id ][ 12 bits sequence ]

* 41 bits of ms  -> ~69 years from the chosen epoch
* 10 bits node   -> up to 1024 instances
* 12 bits seq    -> up to 4096 ids per node per millisecond

**Why a lock and not a compare-and-swap loop.** The state that has to move
atomically is the pair `(last_timestamp, sequence)`: read it, decide the next
sequence, write it back, with nobody interleaving. A lock-free CAS loop is the
usual answer in a language with real parallel threads. Python is not that
language here - the create path runs on one event loop thread, and `.acquire()`
on an uncontended `threading.Lock` is a couple of hundred nanoseconds. So the
lock *is* the idiomatic tool, and it buys something the CAS loop did not: while
it is held there are no interleavings to reason about, so `now < last_timestamp`
can only mean one thing - the wall clock genuinely moved backwards.

Note there is no `async` here on purpose. Minting an id is pure arithmetic; it
never awaits, so making it a coroutine would add a scheduler round-trip to the
hot path and buy nothing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import NamedTuple

__all__ = [
    "CUSTOM_EPOCH_MS",
    "IdGenerator",
    "IdParts",
    "base62_decode",
    "base62_encode",
    "decode",
]

CUSTOM_EPOCH_MS = 1_704_067_200_000
"""A custom epoch keeps ids small for longer (ms since the Unix epoch, ~2024-01-01)."""

NODE_ID_BITS = 10
SEQUENCE_BITS = 12

MAX_NODE_ID = 1 << NODE_ID_BITS  # 1024
MAX_SEQUENCE = 1 << SEQUENCE_BITS  # 4096
SEQUENCE_MASK = MAX_SEQUENCE - 1  # 0xfff
NODE_ID_MASK = MAX_NODE_ID - 1  # 0x3ff
TIMESTAMP_SHIFT = NODE_ID_BITS + SEQUENCE_BITS  # 22

BASE62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE62_INDEX = {char: value for value, char in enumerate(BASE62_ALPHABET)}


class IdParts(NamedTuple):
    """The three Snowflake fields recovered from an id by :func:`decode`.

    `timestamp_ms` is milliseconds since :data:`CUSTOM_EPOCH_MS`, not since the
    Unix epoch - add the custom epoch back to get wall-clock time.
    """

    timestamp_ms: int
    node_id: int
    sequence: int


def _now_ms() -> int:
    """Milliseconds since :data:`CUSTOM_EPOCH_MS`.

    `time.time()` is the right clock here despite being the non-monotonic one:
    the ids must be comparable *across* processes and restarts, which a
    per-process monotonic clock cannot promise. Paying for that is exactly why
    the backwards-clock case below needs a policy.
    """
    return max(0, int(time.time() * 1000) - CUSTOM_EPOCH_MS)


def assemble_id(timestamp_ms: int, sequence: int, node_id: int) -> int:
    """Pack the three fields into one 64-bit id."""
    return (timestamp_ms << TIMESTAMP_SHIFT) | (node_id << SEQUENCE_BITS) | sequence


def decode(id_: int) -> IdParts:
    """Split a generated id back into its Snowflake fields.

    Pure observability - the demo dashboard uses it to show that a slug's id is
    time-ordered and carries the node id.
    """
    return IdParts(
        timestamp_ms=id_ >> TIMESTAMP_SHIFT,
        node_id=(id_ >> SEQUENCE_BITS) & NODE_ID_MASK,
        sequence=id_ & SEQUENCE_MASK,
    )


def base62_encode(value: int) -> str:
    """Encode a non-negative integer as base62 (digits, uppercase, lowercase).

    Zero encodes as `"0"` rather than the empty string. An id of 0 needs
    timestamp, node and sequence all zero, so it is unreachable in practice -
    but an empty slug would be a routing hazard, and this is one character.
    """
    if value < 0:
        raise ValueError("base62_encode expects a non-negative integer")
    if value == 0:
        return BASE62_ALPHABET[0]

    digits: list[str] = []
    while value > 0:
        value, remainder = divmod(value, 62)
        digits.append(BASE62_ALPHABET[remainder])
    # Digits come out least-significant first.
    digits.reverse()
    return "".join(digits)


def base62_decode(slug: str) -> int:
    """Inverse of :func:`base62_encode`.

    Raises `ValueError` on any character outside the alphabet, so it doubles as
    a cheap "is this even a generated slug" check.
    """
    value = 0
    for char in slug:
        digit = _BASE62_INDEX.get(char)
        if digit is None:
            raise ValueError(f"not a base62 string: {slug!r}")
        value = value * 62 + digit
    return value


class IdGenerator:
    """A Snowflake id source for one node.

    Thread-safe and cheap: one instance per process, held in the app state. The
    node id must be unique per running instance - that, and nothing else, is
    what keeps two instances from ever minting the same id.
    """

    __slots__ = ("_clock", "_last_timestamp", "_lock", "_sequence", "node_id")

    def __init__(self, node_id: int, *, clock: Callable[[], int] | None = None) -> None:
        """Build a generator for `node_id`.

        `clock` returns milliseconds since :data:`CUSTOM_EPOCH_MS`. Production
        uses the wall clock; tests inject a controllable one to drive a
        timestamp regression, which `time.time()` cannot reproduce on demand.
        """
        if not 0 <= node_id < MAX_NODE_ID:
            raise ValueError(f"node_id must fit in {NODE_ID_BITS} bits (0..{MAX_NODE_ID - 1})")
        self.node_id = node_id
        self._clock: Callable[[], int] = clock if clock is not None else _now_ms
        self._lock = threading.Lock()
        self._last_timestamp = 0
        self._sequence = 0

    def next_id(self) -> int:
        """Return the next unique, time-ordered 64-bit id.

        Within a millisecond the sequence counter advances; when it exhausts its
        12 bits we wait for the next millisecond rather than reuse a value.

        TODO(V1): the last open acceptance box - "clock moving backwards has a
        defined, non-corrupting behavior". Today a backwards step lands in the
        `while` below and spins until the wall clock catches up, which for a
        one-off NTP slew is fine and for a *step* backwards (VM restore, a large
        correction) is not: it blocks. And it blocks harder in Python than it did
        in Rust, because this spin holds the only event-loop thread - no other
        request progresses while it runs. Decide the policy (refuse and raise?
        borrow from the sequence? track an offset and carry on?), write it down
        in `docs/01-design.md`, and tick the box.
        """
        with self._lock:
            timestamp = self._clock()

            if timestamp < self._last_timestamp:
                # The clock went backwards. See the TODO above.
                while timestamp < self._last_timestamp:
                    timestamp = self._clock()

            if timestamp == self._last_timestamp:
                sequence = self._sequence + 1
                if sequence >= MAX_SEQUENCE:
                    # This millisecond is spent; wait for the next one.
                    while timestamp <= self._last_timestamp:
                        timestamp = self._clock()
                    sequence = 0
            else:
                sequence = 0

            self._last_timestamp = timestamp
            self._sequence = sequence
            return assemble_id(timestamp, sequence, self.node_id)

    def next_slug(self) -> str:
        """A fresh id, base62-encoded."""
        return self.next_id_and_slug()[1]

    def next_id_and_slug(self) -> tuple[int, str]:
        """A fresh id together with its slug, from a *single* generated id.

        The create path needs both: the id is the row's primary key, the slug is
        its public short code - the same number, base62-encoded.
        """
        id_ = self.next_id()
        return id_, base62_encode(id_)
