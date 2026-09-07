"""Erasure-coding lab — From the field, ungraded.

Survive lost disks for a fraction of replication's cost: split a blob into `k`
data shards plus `m` parity shards so that **any `k` of `n = k + m`** rebuild the
original bit-exact. Three-way replication costs 3× and survives 2 failures;
RS(4,2) costs 1.5× and survives the same 2. That ratio is why every large store
eventually gets here.

This is a **codec lab**, not a storage backend. Identity stays the plaintext
SHA-256, and nothing here is on the request path — wiring shards under
`objects/` is a separate step with its own placement and repair problems.

Build order, because each layer needs the one above it:

1. `gf256` — GF(2⁸) with the `0x11D` reduction polynomial and log/antilog tables
2. `reed_solomon` — systematic RS(4,2) encode and reconstruct
3. `lrc` — Local Reconstruction Codes, for cheap single-shard repair
4. `durability` — the nines calculator that turns `(k, m, AFR, window)` into a
   number you can defend

Teach-yourself: `docs/12-how-erasure-coding-works.md`.
"""

from __future__ import annotations

__all__ = [
    "DurabilityInput",
    "DurabilityReport",
    "ErasureError",
    "Gf256",
    "InvalidLayout",
    "Lrc",
    "LrcParams",
    "ReedSolomon",
    "RepairStats",
    "Shard",
    "Singular",
    "TooManyErasures",
    "Unsupported",
    "compute_durability",
]


class ErasureError(Exception):
    """Base for codec failures.

    Deliberately *not* an `AppError`: this lab is offline, and mapping a
    singular matrix onto an HTTP status would imply a request caused it.
    """


class Singular(ErasureError):
    """A zero denominator, or a matrix that cannot be inverted."""


class TooManyErasures(ErasureError):
    """Fewer than `k` surviving shards — the data is genuinely gone.

    Carries both numbers because "we needed 4 and had 3" is the whole incident
    report, and a bare message loses it.
    """

    def __init__(self, need: int, have: int) -> None:
        super().__init__(f"too many erasures: need {need} shards, have {have}")
        self.need = need
        self.have = have


class InvalidLayout(ErasureError):
    """Shard lengths disagree, or the data length is incompatible with `k`."""


class Unsupported(ErasureError):
    """Parameters outside what this lab implements."""


from .durability import (  # noqa: E402
    DurabilityInput,
    DurabilityReport,
    compute_durability,
)
from .gf256 import Gf256  # noqa: E402
from .lrc import Lrc, LrcParams, RepairStats  # noqa: E402
from .reed_solomon import ReedSolomon, Shard  # noqa: E402
