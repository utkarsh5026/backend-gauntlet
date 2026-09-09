"""Content-defined chunking — the cutter behind chunk-level dedup.

**CDC here means content-defined chunking, not Change Data Capture.**

Whole-object CAS (V1) only ever shares *identical* files. Insert one byte at the
front of a 1 GB object and every byte shifts, the hash changes completely, and
you store a second gigabyte. This module splits a byte stream at boundaries
chosen by the **content** rather than by position, so an edit only disturbs the
chunks it actually touches and everything after it re-aligns.

## Why a rolling hash and not fixed-size blocks

Fixed 64 KB blocks have exactly the problem above: a one-byte insertion at
offset 0 shifts every subsequent block boundary and nothing downstream matches.
A content-defined boundary is a property of the bytes in a small window, so it
moves *with* the data. The window slides byte by byte, and wherever the hash
happens to land on a chosen pattern, that is a cut. Insert a byte and only the
chunk containing it changes; the next boundary reappears in the same place.

## The gear hash

`hash = (hash << 1) + GEAR[byte]` — one shift and one add per byte, no
multiplication and no division, which is the whole reason it is fast enough to
run on every byte of every upload. Each new byte pushes the previous ones one
bit higher, so old bytes fall out of the top on their own: the window is
implicit rather than maintained.

`GEAR` is 256 fixed random-looking 64-bit values. It is derived here from a
SHA-256 of a fixed string rather than being a literal table, so it is
reproducible byte-for-byte on every machine and every Python version — which
matters more than it looks: change the table and every boundary moves, so every
object already in the store stops sharing chunks with anything written
afterwards.

## Normalized chunking (FastCDC's contribution)

A plain rolling-hash cutter produces a geometric size distribution: far too many
runt chunks and a long tail of huge ones. FastCDC fixes it with two masks — a
*strict* one (more bits, cuts are rarer) used before the target average, and a
*lenient* one (fewer bits, cuts are commoner) used after. The effect is to push
the distribution toward the target size from both sides. Combined with the hard
`min_chunk` floor (never even look for a cut before it) and the `max_chunk`
ceiling (force one if none is found), chunk sizes stay in a usable band.
"""

from __future__ import annotations

import hashlib

from .config import CdcSettings
from .errors import InvalidRequest

__all__ = ["GEAR", "CdcChunker", "MASKS", "cut_point"]

_GEAR_SEED = b"backend-gauntlet/06-object-store/cdc/gear/v1"
_U64 = 0xFFFF_FFFF_FFFF_FFFF


def _build_gear() -> tuple[int, ...]:
    """256 stable pseudo-random 64-bit values, one per byte value.

    Derived rather than hard-coded: a literal table is 256 magic numbers nobody
    can check, while this is one line you can re-run. Stability is the actual
    requirement — see the module docstring on why changing it silently destroys
    dedup against everything already stored.
    """
    return tuple(
        int.from_bytes(hashlib.sha256(_GEAR_SEED + bytes([value])).digest()[:8], "little")
        for value in range(256)
    )


GEAR = _build_gear()

MASKS: dict[int, int] = {
    5: 0x0000_0000_0180_4110,
    6: 0x0000_0000_0180_3110,
    7: 0x0000_0000_1803_5100,
    8: 0x0000_0018_0003_5300,
    9: 0x0000_0190_0035_3000,
    10: 0x0000_5900_0353_0000,
    11: 0x0000_D900_0353_0000,
    12: 0x0000_D901_0353_0000,
    13: 0x0000_D903_0353_0000,
    14: 0x0000_D903_1353_0000,
    15: 0x0000_D90F_0353_0000,
    16: 0x0000_D903_0353_7000,
    17: 0x0000_D907_0353_7000,
    18: 0x0000_D907_0753_7000,
    19: 0x0000_D917_0753_7000,
    20: 0x0000_D917_4753_7000,
    21: 0x0000_D917_6753_7000,
    22: 0x0000_D937_6753_7000,
    23: 0x0000_D937_7753_7000,
    24: 0x0000_D937_7757_2000,
    25: 0x0000_DB37_7757_2000,
}
"""FastCDC's published masks, indexed by `log2(target size)`.

The bits are *spread across the word* rather than being a low `(1 << n) - 1`
run. That is deliberate: the gear hash accumulates toward the high bits, so a
low-bit mask would only ever test the handful of bytes most recently shifted in
and the boundary would depend on far less content than intended."""


def _mask_for(size: int, *, strict: bool) -> int:
    """Pick a mask for a target chunk size.

    `strict` selects one extra bit (cuts roughly half as often), used before the
    average is reached; the lenient one drops a bit and is used after.
    """
    bits = max(size.bit_length() - 1, 0)
    shifted = bits + 1 if strict else bits - 1
    clamped = min(max(shifted, min(MASKS)), max(MASKS))
    return MASKS[clamped]


def cut_point(data: bytes, min_size: int, avg_size: int, max_size: int) -> int:
    """Where the next chunk should end, as an offset into `data`.

    Returns `len(data)` when no boundary was found — the caller decides whether
    that means "hold this buffer and wait for more bytes" or "this is EOF, emit
    it". Never returns less than `min(min_size, len(data))`.
    """
    length = len(data)
    if length <= min_size:
        return length

    limit = min(length, max_size)
    normal = min(avg_size, limit)
    mask_strict = _mask_for(avg_size, strict=True)
    mask_lenient = _mask_for(avg_size, strict=False)

    fingerprint = 0
    # Start at `min_size`: bytes before the floor cannot produce a boundary, so
    # hashing them would only cost time.
    index = min_size

    while index < normal:
        fingerprint = ((fingerprint << 1) + GEAR[data[index]]) & _U64
        if not fingerprint & mask_strict:
            return index + 1
        index += 1

    while index < limit:
        fingerprint = ((fingerprint << 1) + GEAR[data[index]]) & _U64
        if not fingerprint & mask_lenient:
            return index + 1
        index += 1

    return limit


class CdcChunker:
    """Incremental content-defined chunker over a stream of network frames.

    HTTP body frames are **not** chunks. They arrive at whatever size the socket
    and the client chose, and a boundary can fall anywhere — including in the
    middle of a frame, or several frames after the last one. So frames are
    concatenated into a pending buffer and cut independently of how they
    arrived; feed the same bytes in one frame or a thousand and the chunks come
    out identical. That property is the reason dedup works across clients at
    all.
    """

    __slots__ = ("_pending", "settings")

    def __init__(self, settings: CdcSettings) -> None:
        _validate(settings)
        self.settings = settings
        self._pending = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        """Absorb `data`; return every chunk that completed on a boundary."""
        if not data:
            return []
        self._pending.extend(data)
        return self._drain(eof=False)

    def finish(self) -> list[bytes]:
        """Flush the tail after EOF, including a final short chunk."""
        return self._drain(eof=True)

    def _drain(self, *, eof: bool) -> list[bytes]:
        out: list[bytes] = []
        min_size = self.settings.min_chunk
        avg_size = self.settings.avg_chunk
        max_size = self.settings.max_chunk

        while True:
            pending = len(self._pending)
            if pending == 0 or (not eof and pending < min_size):
                break

            end = cut_point(bytes(self._pending), min_size, avg_size, max_size)

            # A cut that consumed the whole buffer is not a boundary — it is
            # "ran out of bytes". Holding it until more arrive is what keeps
            # chunking independent of frame sizes; the exceptions are EOF (there
            # will be no more bytes) and hitting `max_chunk` (the forced cut,
            # which bounds memory).
            if end >= pending and not eof and pending < max_size:
                break

            out.append(bytes(self._pending[:end]))
            del self._pending[:end]

        return out


def _validate(settings: CdcSettings) -> None:
    """Reject an incoherent chunk-size band before it can cut anything."""
    if not settings.min_chunk <= settings.avg_chunk <= settings.max_chunk:
        raise InvalidRequest(
            "CDC requires min_chunk <= avg_chunk <= max_chunk, got "
            f"min={settings.min_chunk} avg={settings.avg_chunk} "
            f"max={settings.max_chunk}"
        )
    if settings.min_chunk < 64:
        raise InvalidRequest(f"CDC min_chunk must be >= 64, got {settings.min_chunk}")
