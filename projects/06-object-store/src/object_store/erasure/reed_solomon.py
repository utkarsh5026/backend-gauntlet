"""Systematic Reed–Solomon RS(k, m) — encode and reconstruct.

Lab target: **RS(4,2)** — 4 data shards, 2 parity, 6 total. Overhead 1.5×, and
it survives any 2 losses. Compare three-way replication: 3× overhead for the
same tolerance.

## Systematic, and why that matters

"Systematic" means the first `k` shards *are* the plaintext, split into equal
pieces. Reading an intact object is then just concatenating shards 0..k-1 with
no decoding at all — the field arithmetic is paid only on the repair path, which
is rare. A non-systematic code would make every single read a matrix multiply.

## Layout: the hand-checkable P+Q construction

- Split the plaintext into `k` equal shards, zero-padding so the length divides.
- **P** (shard 4) = `d0 ⊕ d1 ⊕ d2 ⊕ d3` — coefficients `[1,1,1,1]`, plain XOR.
- **Q** (shard 5) = `1·d0 ⊕ 2·d1 ⊕ 4·d2 ⊕ 8·d3` — a Vandermonde row, base 2.

Why the two rows differ is the whole idea. P alone recovers *one* loss: XOR the
survivors. It cannot recover two, because with two unknowns and one equation
there are 256 consistent answers. Q supplies a second, *independent* equation —
independent precisely because its coefficients are distinct powers of a
generator, so no subset of columns is linearly dependent. Two equations, two
unknowns, one solution.

## Reconstruct

Pick any `k` survivors. Their rows of the generator matrix form a `k × k`
submatrix `G'` with `G' · d = y`, where `y` is the survivors' bytes. Invert `G'`
and multiply. Any `k` rows of a Vandermonde matrix are invertible, which is the
formal statement of "any `k` of `n` suffice".
"""

from __future__ import annotations

from dataclasses import dataclass

from . import InvalidLayout, Singular, TooManyErasures
from .gf256 import Gf256

__all__ = ["RS_K", "RS_M", "RS_N", "ReedSolomon", "Shard"]

RS_K = 4
RS_M = 2
RS_N = RS_K + RS_M

Q_COEFFICIENTS = (1, 2, 4, 8)
"""`[2⁰, 2¹, 2², 2³]` — the Q row. Distinct powers of the generator, which is
what makes Q independent of P."""


@dataclass(slots=True)
class Shard:
    """One shard of an erasure-coded blob.

    `id` is its position in the codeword and is *not* redundant with list
    position: reconstruct receives a sparse list where missing shards are
    `None`, and the id is how a survivor names which generator row it satisfies.
    """

    id: int
    data: bytes


class ReedSolomon:
    """A systematic Reed–Solomon coder over GF(2⁸)."""

    __slots__ = ("gf", "k", "m")

    def __init__(self, k: int = RS_K, m: int = RS_M) -> None:
        if (k, m) != (RS_K, RS_M):
            raise InvalidLayout(
                f"only the lab RS({RS_K},{RS_M}) shape is implemented, got ({k}, {m})"
            )
        self.k = k
        self.m = m
        self.gf = Gf256()

    @classmethod
    def rs_4_2(cls) -> ReedSolomon:
        return cls(RS_K, RS_M)

    @property
    def n(self) -> int:
        return self.k + self.m

    def encode(self, plaintext: bytes) -> list[Shard]:
        """Encode plaintext into `n` shards; the first `k` are the data itself.

        Zero-padding is not recorded anywhere, so `reconstruct` returns the
        padded length. Callers that need the exact original length carry it
        separately — in this store that is what the index row's `size` already
        is, which is why the codec does not invent a second place to keep it.
        """
        if not plaintext:
            raise InvalidLayout("plaintext must be non-empty")

        padded = bytearray(plaintext)
        remainder = len(padded) % self.k
        if remainder:
            padded.extend(b"\x00" * (self.k - remainder))

        shard_len = len(padded) // self.k
        data = [bytes(padded[i * shard_len : (i + 1) * shard_len]) for i in range(self.k)]

        parity_p = bytearray(shard_len)
        parity_q = bytearray(shard_len)
        for column in range(shard_len):
            p_acc = 0
            q_acc = 0
            for index, shard in enumerate(data):
                byte = shard[column]
                p_acc ^= byte
                q_acc ^= self.gf.mul(Q_COEFFICIENTS[index], byte)
            parity_p[column] = p_acc
            parity_q[column] = q_acc

        shards = [Shard(index, chunk) for index, chunk in enumerate(data)]
        shards.append(Shard(self.k, bytes(parity_p)))
        shards.append(Shard(self.k + 1, bytes(parity_q)))
        return shards

    def reconstruct(self, shards: list[Shard | None]) -> bytes:
        """Rebuild the plaintext from a codeword with erasures.

        `shards[i] is None` means shard `i` is lost. At least `k` must survive.
        The output is bit-exact for any input whose length was already a
        multiple of `k`; otherwise it carries encode's zero padding.
        """
        if len(shards) != self.n:
            raise InvalidLayout(f"expected {self.n} shard slots, got {len(shards)}")

        survivors = [(index, shard) for index, shard in enumerate(shards) if shard is not None]
        if len(survivors) < self.k:
            raise TooManyErasures(need=self.k, have=len(survivors))

        # Prefer the lowest ids: with a systematic code they are the data shards
        # themselves, so in the common case `G'` is the identity and the inverse
        # is free.
        chosen = survivors[: self.k]
        shard_len = len(chosen[0][1].data)
        if any(len(shard.data) != shard_len for _, shard in chosen):
            raise InvalidLayout("survivor shards have unequal lengths")

        submatrix = [self.generator_row(index) for index, _ in chosen]
        inverse = self.invert(submatrix)

        recovered = [bytearray(shard_len) for _ in range(self.k)]
        for column in range(shard_len):
            observed = [shard.data[column] for _, shard in chosen]
            for row in range(self.k):
                accumulator = 0
                for col in range(self.k):
                    accumulator ^= self.gf.mul(inverse[row][col], observed[col])
                recovered[row][column] = accumulator

        return b"".join(bytes(shard) for shard in recovered)

    @staticmethod
    def generator_row(shard_id: int) -> list[int]:
        """Row `shard_id` of the systematic P+Q generator matrix `G`.

        Rows 0..3 are the identity — that is what makes the code systematic —
        and rows 4 and 5 are P and Q.
        """
        if 0 <= shard_id < RS_K:
            return [1 if column == shard_id else 0 for column in range(RS_K)]
        if shard_id == RS_K:
            return [1, 1, 1, 1]
        if shard_id == RS_K + 1:
            return list(Q_COEFFICIENTS)
        raise InvalidLayout(f"shard id {shard_id} out of range for RS(4,2)")

    def invert(self, matrix: list[list[int]]) -> list[list[int]]:
        """Invert a `k × k` matrix over GF(2⁸) by Gauss–Jordan.

        Textbook `[A | I] → [I | A⁻¹]`, with every arithmetic operation done in
        the field: `+` is XOR, and dividing a row by its pivot is multiplying by
        the pivot's inverse. Because the field has exact inverses there is no
        rounding and no need for numerical pivoting — the only reason to swap
        rows is a pivot that is literally zero.
        """
        size = self.k
        augmented = [
            list(row) + [1 if i == j else 0 for j in range(size)] for i, row in enumerate(matrix)
        ]

        for column in range(size):
            pivot = next(
                (row for row in range(column, size) if augmented[row][column] != 0),
                None,
            )
            if pivot is None:
                raise Singular(
                    f"zero pivot in column {column} while inverting the survivor submatrix"
                )
            if pivot != column:
                augmented[pivot], augmented[column] = (
                    augmented[column],
                    augmented[pivot],
                )

            pivot_inverse = self.gf.inv(augmented[column][column])
            augmented[column] = [self.gf.mul(cell, pivot_inverse) for cell in augmented[column]]

            for row in range(size):
                if row == column:
                    continue
                factor = augmented[row][column]
                if factor == 0:
                    continue
                pivot_row = augmented[column]
                augmented[row] = [
                    cell ^ self.gf.mul(factor, pivot_cell)
                    for cell, pivot_cell in zip(augmented[row], pivot_row, strict=True)
                ]

        return [row[size:] for row in augmented]
