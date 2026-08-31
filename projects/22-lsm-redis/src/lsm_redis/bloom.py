"""V5 — Bloom filters: skip the SSTables that cannot hold the key.
`src/lsm_redis/bloom.py`.

A read for a key that is not in the memtable has to consult SSTables newest to
oldest. With many files per level a **miss** is the worst case: you touch every
one of them only to find nothing. A bloom filter per SSTable turns most of that
into a single in-memory check. It is a bit array plus `k` hash functions;
`insert` sets `k` bits for a key, `maybe_contains` checks those same `k` bits:

* all `k` bits set -> the key *might* be present; read the file to be sure.
* any bit clear -> the key is **definitely not** present; skip the file entirely.

The one-sided error is the whole design: **no false negatives** ever, and a
tunable **false-positive** rate (occasionally read a file for nothing). The
classic sizing is ``bits ≈ -n·ln(p)/ln(2)²`` and ``k ≈ (bits/n)·ln 2``;
`BLOOM_BITS_PER_KEY = 10` gives about a 1% false-positive rate, which is
LevelDB's default and the number V5's measurement criterion checks against.

*Concept to internalize:* trading a little space and a tunable false-positive
rate for skipping disk I/O, and why the no-false-negatives guarantee is
non-negotiable in a database — a false negative silently drops a key that is
really on disk, and nothing downstream can detect it.

## The Python trap that will cost you a day if you meet it in production

**Never use the built-in `hash()`.** `hash(b"key")` is seeded from
`PYTHONHASHSEED`, which is **random per process** (that is a security feature —
it is what stops an attacker feeding you colliding dict keys). A filter built
with `hash()` and persisted into an SSTable will, after a restart, probe
completely different bits than the ones it set. Every lookup becomes a coin
flip: false negatives, at scale, on committed data, with no error anywhere. The
same code passes every test that builds and queries a filter inside one process
— which is every test you would naturally write.

Use something deterministic across processes: `hashlib.blake2b(key,
digest_size=16)` gives you 128 bits in one pass, and splitting that into two
64-bit halves gives you `h1` and `h2` for free. Then Kirsch–Mitzenmacher
double hashing — probe `i` is `(h1 + i*h2) % nbits` — synthesizes all `k`
indices without hashing `k` times. `zlib.crc32` with two different seeds is a
cheaper alternative worth benchmarking; it is deterministic too.

Building that is part of the exercise, which is why no hashing dependency is
declared.

## Bit arrays in CPython, honestly

`bytearray` with `bits[i >> 3] |= 1 << (i & 7)` is the direct translation and it
is fine — but a pure-Python loop doing `k` of those per key, over every key in a
flush, is genuinely slow, and this is a hot path (it runs once per key on write
and up to once per SSTable on every read miss). Two alternatives worth
measuring before you assume:

* a Python `int` as one enormous bitmask (`mask |= 1 << i`), which is C-speed
  bit twiddling but reallocates a big integer on every set;
* `k` small `bytearray` operations but with the indices computed in one
  comprehension rather than a loop body.

Whichever wins, the number belongs in `docs/22-benchmarks.md` — "the filter is
faster than the disk read it avoids" is the entire justification for the
component, and on CPython that is a claim you should check rather than assume.
"""

from __future__ import annotations

from typing import Self

__all__ = ["Bloom"]


class Bloom:
    """A bloom filter over a fixed key set, serialized into each SSTable (V4).

    Built once during a flush, then read-only for the life of the file — which
    is the property that makes it safe to share across concurrent readers with
    no lock at all.
    """

    def __init__(self, bits: bytearray, k: int) -> None:
        self.bits = bits
        """The packed bit array, 8 bits per byte."""
        self.k = k
        """Probes per key."""

    @classmethod
    def sized(cls, expected_keys: int, bits_per_key: int) -> Self:
        """Size a filter for `expected_keys` at `bits_per_key`, picking `k` to
        minimize the false-positive rate.

        TODO(V5): allocate `ceil(expected_keys * bits_per_key / 8)` bytes and
        set `k = round(bits_per_key * ln 2)`, clamped to at least 1. That
        formula is not folklore: `k` too small wastes bits, `k` too large fills
        the array and the FP rate climbs again, and `ln 2` is where the curve
        bottoms out.

        `expected_keys == 0` must still produce a valid, tiny filter that
        answers "definitely not present" for everything and raises nothing —
        an empty SSTable is a legal thing for a compaction to produce.
        """
        raise NotImplementedError(
            "V5: size the bit array from bits_per_key and pick k = round(bits_per_key * ln2)"
        )

    @classmethod
    def from_parts(cls, bits: bytes, k: int) -> Self:
        """Rebuild a filter from its serialized form, read back out of an
        SSTable footer (V4). Wired — the interesting sizing is in `sized`.

        Copied into a `bytearray` rather than kept as the `bytes` slice it
        arrived in: the filter is logically immutable after load, but keeping a
        slice of a larger read buffer alive would pin that whole buffer in
        memory for the lifetime of the table.
        """
        return cls(bytearray(bits), k)

    def insert(self, key: bytes) -> None:
        """Record `key`'s membership by setting its `k` bits.

        TODO(V5): derive two base hashes of `key` (see the module docstring —
        **not** `hash()`), then for `i` in `range(self.k)` set the bit at
        `(h1 + i * h2) % (len(self.bits) * 8)`.

        Every key inserted here MUST later be reported present by
        `maybe_contains`. That is the contract, and the only way to break it
        accidentally is to compute the indices differently in the two methods —
        so write the index derivation **once** and call it from both. A copy of
        four lines is how a filter starts lying.
        """
        raise NotImplementedError("V5: set the k bits for `key` via double hashing")

    def maybe_contains(self, key: bytes) -> bool:
        """`False` = definitely absent (skip the SSTable). `True` = maybe
        present (read it).

        TODO(V5): probe the same `k` bits `insert` would set; return `False` the
        moment one is clear, `True` if all are set. Returning early on the first
        clear bit is not just an optimization — for a key that is absent, which
        is the case this whole component exists for, it usually costs one probe
        instead of `k`.
        """
        raise NotImplementedError("V5: return False only if some of `key`'s k bits are clear")
