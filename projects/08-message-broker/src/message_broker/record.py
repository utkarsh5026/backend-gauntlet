"""Shared record types — the vocabulary every layer speaks.

Plumbing, not a vertical: these are the plain data the log stores and the API
returns. Key and value are `bytes`, not `str`, because a broker moves opaque
payloads: the moment you decode on the way in you have lied about what was
stored. Python's `bytes` is immutable and refcounted, so passing one from the
produce path through the append path and back out to a fetch response copies
nothing — the same property Rust needed `Bytes` for.

An **offset** is a plain `int`: a logical position within a *single* partition's
log. Offsets are per-partition (V3), assigned monotonically by the log (V1), and
start at 0.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Record", "StoredRecord"]


@dataclass(frozen=True, slots=True)
class Record:
    """A record as handed to the broker to append.

    The offset is deliberately absent — the log assigns it on append and returns
    it, which is what makes the offset a fact about the log rather than a claim
    by the producer.

    `frozen=True` because nothing downstream of produce has any business
    mutating a record in flight; `slots=True` because at produce rates these are
    the most-allocated object in the process and the per-instance `__dict__` is
    pure overhead.
    """

    value: bytes
    """The payload."""

    key: bytes | None = None
    """Optional partition key. Same key -> same partition (V3), and it is stored
    in the frame so a fetch can return it."""

    timestamp_ms: int = 0
    """Producer timestamp (epoch millis). Stamped at produce time if the client
    did not supply one."""


@dataclass(frozen=True, slots=True)
class StoredRecord:
    """A record read back from the log: what was stored, plus where it lives."""

    offset: int
    timestamp_ms: int
    key: bytes | None
    value: bytes
