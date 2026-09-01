"""The two 20-byte identities that thread through every module.

Plumbing, not a challenge — fully implemented, like the `common_*` packages. The
*interesting* thing about these types is conceptual and lives in the SPEC: an
`InfoHash` is content-addressing (SHA-1 of the bencoded `info` dict, V2), and a
`PeerId` is a per-run random identity (see `client.generate_peer_id`).

## Why a wrapper class and not `bytes`

A bare `bytes` would work on the wire and fail everywhere else. Three things
this buys, each of which is a real bug it stops:

* **The 20-byte invariant is checked once, at construction.** A `bytes` that is
  19 long produces a handshake that a peer silently drops, and you debug the
  socket for an hour. Here it raises at the boundary that built it.
* **`hex()` and `raw` are different accessors with different jobs.** Nearly
  every infohash bug in a first implementation is a hex string sent where raw
  bytes belonged, or the reverse — the tracker query (V3) and the handshake (V4)
  both take the raw 20, while URLs, logs and JSON take the 40 hex characters.
  Two names, no ambiguity.
* **It is hashable and immutable**, so it works as a dict key in the torrent
  registry, which is exactly how the swarm names things.

`frozen=True, slots=True` is what makes those claims hold: no `__dict__` to
attach state to, no assignment after `__post_init__`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

__all__ = ["HASH_LEN", "InfoHash", "PeerId"]

HASH_LEN = 20
"""Both identities are 20 bytes: SHA-1's digest size, which is where the number
came from and why it is the same for a peer id that is not a hash at all."""


@dataclass(frozen=True, slots=True)
class InfoHash:
    """The 20-byte SHA-1 of a torrent's bencoded `info` dictionary.

    The name every peer and tracker uses for this content. Computed in
    `metainfo.py` (V2). There is no registry that issues these — two clients
    agree they mean the same file because they independently hashed the same
    bytes, which is the whole idea the vertical is teaching.
    """

    raw: bytes
    """The 20 bytes themselves. **This** is what goes on the wire — the tracker
    query's `info_hash` parameter and the handshake's bytes 28..48 — never the
    hex string."""

    def __post_init__(self) -> None:
        if len(self.raw) != HASH_LEN:
            raise ValueError(f"an infohash is {HASH_LEN} bytes, got {len(self.raw)}")

    def hex(self) -> str:
        """Lowercase 40-character hex — for URLs, logs, and the control plane's
        JSON. Never for the wire."""
        return self.raw.hex()

    @classmethod
    def from_hex(cls, text: str) -> Self | None:
        """Parse 40 hex characters back into an infohash.

        `None` rather than an exception because every caller is validating
        untrusted input (a path parameter, a magnet's `xt`) and wants to answer
        with a 400 rather than to handle an exception. `bytes.fromhex` is strict
        about non-hex characters but says nothing about length, so both are
        checked here.
        """
        try:
            raw = bytes.fromhex(text.strip())
        except ValueError:
            return None
        return cls(raw) if len(raw) == HASH_LEN else None

    def __str__(self) -> str:
        return self.hex()

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source: type[Any], _handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """Render as the hex string in FastAPI responses.

        Without this, pydantic would try to serialize `raw` and hand the control
        plane 20 bytes of binary where a JSON string belongs. The Rust side
        needed a hand-written `Serialize` impl for the same reason and for the
        same one-line result.
        """
        return core_schema.no_info_plain_validator_function(
            _validate_info_hash,
            serialization=core_schema.plain_serializer_function_ser_schema(
                InfoHash.hex, return_schema=core_schema.str_schema()
            ),
        )


def _validate_info_hash(value: object) -> InfoHash:
    """Accept an `InfoHash`, 20 raw bytes, or 40 hex characters."""
    match value:
        case InfoHash():
            return value
        case bytes() | bytearray():
            return InfoHash(bytes(value))
        case str():
            parsed = InfoHash.from_hex(value)
            if parsed is None:
                raise ValueError("an infohash is 40 hex characters")
            return parsed
        case _:
            raise ValueError(f"cannot read an infohash from {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class PeerId:
    """This client's 20-byte identity for a run.

    A client prefix plus random bytes — see `client.generate_peer_id`. Sent in
    the handshake (V4) and every announce (V3). Unlike an infohash it is *not*
    derived from anything: it is arbitrary, and the only rules are that it is 20
    bytes and stable for the run.
    """

    raw: bytes

    def __post_init__(self) -> None:
        if len(self.raw) != HASH_LEN:
            raise ValueError(f"a peer id is {HASH_LEN} bytes, got {len(self.raw)}")

    def hex(self) -> str:
        return self.raw.hex()

    def __str__(self) -> str:
        """The printable form for logs.

        Peer ids are conventionally mostly-ASCII (`-PY0001-` and then random
        bytes), so `repr` of the raw bytes reads better than hex for the half
        that is text — but the random half is genuinely binary, so this decodes
        with a replacement character rather than pretending otherwise.
        """
        return self.raw.decode("ascii", "replace")
