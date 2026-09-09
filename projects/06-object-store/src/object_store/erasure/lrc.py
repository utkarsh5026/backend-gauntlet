"""Local Reconstruction Codes — cheap repair for the common case, one lost shard.

Plain RS repairs a single failure by reading **all `k`** other shards. That is
the dominant cost at scale, because single-disk failure is not the rare case —
it is essentially the *only* case, happening constantly across a large fleet
while double failures are exceptional. Rebuilding one 4 TB disk by reading 17
others saturates the network for hours, and during that window the array is
degraded.

LRC splits the data into `l` **local groups**, gives each a local XOR parity, and
adds `r` global parities on top. A single lost shard then repairs from `≈ k/l`
reads instead of `k`. Azure's published numbers are the canonical example: LRC
cut repair traffic roughly in half at a small storage-overhead increase, which
is a better trade than it sounds because repair bandwidth is the scarce resource.

Lab shape: **(k=4, l=2, r=2)** — 4 data + 2 local + 2 global = 8 shards.
Repairing one data shard reads 2, not 4.

## The catch worth understanding

LRC is not simply "better RS". Adding local parities costs storage, and the code
is no longer maximum-distance-separable: with `k + l + r` shards it does *not*
tolerate every combination of `l + r` losses the way an MDS code of the same
width would. What it buys is that the failure you actually see, constantly, gets
much cheaper. That is a deliberate trade of worst-case tolerance for common-case
cost.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import InvalidLayout, TooManyErasures, Unsupported
from .gf256 import Gf256
from .reed_solomon import Shard

__all__ = ["Lrc", "LrcParams", "RepairStats"]

LAB_K = 4
LAB_L = 2
LAB_R = 2
GROUP_SIZE = LAB_K // LAB_L

GLOBAL_COEFFICIENTS = (
    (1, 2, 4, 8),
    (1, 3, 9, 27),
)
"""Two Vandermonde rows over all `k` data shards. Same GF(2⁸) flavour as RS's Q
row — LRC reorganises *which shards a parity covers*, it does not invent new
arithmetic. Different bases (2 and 3) keep the two rows independent."""


@dataclass(frozen=True, slots=True)
class LrcParams:
    """The shape `(k, l, r)`."""

    k: int
    l: int  # noqa: E741 - the literature's name for the local-group count
    r: int

    @property
    def n(self) -> int:
        return self.k + self.l + self.r

    @property
    def local_repair_fan_in(self) -> int:
        """Ideal single-shard repair reads, `k / l`, for equal groups."""
        return self.k // self.l


LAB_PARAMS = LrcParams(k=LAB_K, l=LAB_L, r=LAB_R)


@dataclass(frozen=True, slots=True)
class RepairStats:
    """How many shards a repair actually touched — the number the SPEC wants.

    Reported rather than assumed, because the whole claim of LRC is a *measured*
    reduction in fan-in and "it should read about two" is not evidence.
    """

    shards_read: int
    used_local: bool


class Lrc:
    """A Local Reconstruction Code coder."""

    __slots__ = ("gf", "params")

    def __init__(self, params: LrcParams = LAB_PARAMS) -> None:
        if (params.k, params.l, params.r) != (LAB_K, LAB_L, LAB_R):
            raise Unsupported(
                f"only lab LRC({LAB_K},{LAB_L},{LAB_R}) is implemented, "
                f"got ({params.k}, {params.l}, {params.r})"
            )
        self.params = params
        self.gf = Gf256()

    @classmethod
    def lab(cls) -> Lrc:
        return cls(LAB_PARAMS)

    def encode(self, plaintext: bytes) -> list[Shard]:
        """Encode into `n = k + l + r` shards.

        Systematic layout: ids `0..k` are data, `k..k+l` are the local XOR
        parities (one per group), and `k+l..n` are the global parities across
        all `k` data shards.
        """
        if not plaintext:
            raise InvalidLayout("plaintext must be non-empty")

        k = self.params.k
        padded = bytearray(plaintext)
        remainder = len(padded) % k
        if remainder:
            padded.extend(b"\x00" * (k - remainder))

        shard_len = len(padded) // k
        data = [bytes(padded[i * shard_len : (i + 1) * shard_len]) for i in range(k)]
        shards = [Shard(index, chunk) for index, chunk in enumerate(data)]

        # Local parities: plain XOR over each group. No field multiply needed —
        # that is exactly why a local repair is cheap in CPU as well as I/O.
        for group in range(self.params.l):
            parity = bytearray(shard_len)
            for member in self.group_data_ids(group):
                for column in range(shard_len):
                    parity[column] ^= data[member][column]
            shards.append(Shard(k + group, bytes(parity)))

        # Global parities: Vandermonde rows over every data shard.
        for offset, coefficients in enumerate(GLOBAL_COEFFICIENTS):
            parity = bytearray(shard_len)
            for column in range(shard_len):
                accumulator = 0
                for index, chunk in enumerate(data):
                    accumulator ^= self.gf.mul(coefficients[index], chunk[column])
                parity[column] = accumulator
            shards.append(Shard(k + self.params.l + offset, bytes(parity)))

        return shards

    def repair_one(self, shards: list[Shard | None], missing: int) -> tuple[Shard, RepairStats]:
        """Repair one missing shard, reporting the read fan-in it cost.

        Tries the local group first: a lost data or local-parity shard is simply
        the XOR of the rest of its group, so the repair is `k/l` reads and no
        multiplication. Falls back to the global parities — `k` reads, the same
        cost plain RS always pays — when the local group is itself incomplete,
        or when the missing shard *is* a global and has no local group.
        """
        n = self.params.n
        if len(shards) != n:
            raise InvalidLayout(f"expected {n} shard slots, got {len(shards)}")
        if not 0 <= missing < n:
            raise InvalidLayout(f"missing id {missing} out of range 0..{n}")

        group = self.local_group_of(missing)
        if group is not None:
            peers = [member for member in group if member != missing]
            present = [shards[member] for member in peers]
            if not any(peer is None for peer in present):
                intact = [_require(peer) for peer in present]
                shard_len = len(intact[0].data)
                data = bytearray(shard_len)
                for peer in intact:
                    for column in range(shard_len):
                        data[column] ^= peer.data[column]
                return (
                    Shard(missing, bytes(data)),
                    RepairStats(shards_read=len(intact), used_local=True),
                )

        return self._repair_via_global(shards, missing)

    def _repair_via_global(
        self, shards: list[Shard | None], missing: int
    ) -> tuple[Shard, RepairStats]:
        """Rebuild through a global parity row — the expensive path."""
        k = self.params.k
        data: list[bytes | None] = [
            shard.data if (shard := shards[index]) is not None else None for index in range(k)
        ]
        holes = [index for index in range(k) if data[index] is None]

        if len(holes) > 1 or (holes and holes[0] != missing):
            raise TooManyErasures(need=k, have=k - len(holes))

        if holes:
            hole = holes[0]
            # From `G = Σ coeff[c]·d[c]`, solve for the one unknown:
            #   d[hole] = (G ⊕ Σ_{c≠hole} coeff[c]·d[c]) / coeff[hole]
            # This is exactly why the coefficients must all be nonzero — a zero
            # coefficient on the missing column makes it unsolvable from that
            # row.
            resolved = self._available_global(shards)
            if resolved is None:
                raise TooManyErasures(need=k, have=k - 1)
            coefficients, parity = resolved

            inverse = self.gf.inv(coefficients[hole])
            rebuilt = bytearray(len(parity))
            for column in range(len(parity)):
                accumulator = parity[column]
                for index in range(k):
                    if index == hole:
                        continue
                    known = data[index]
                    assert known is not None
                    accumulator ^= self.gf.mul(coefficients[index], known[column])
                rebuilt[column] = self.gf.mul(accumulator, inverse)
            data[hole] = bytes(rebuilt)

        return (
            Shard(missing, self._recompute(missing, data)),
            RepairStats(shards_read=k, used_local=False),
        )

    def _available_global(self, shards: list[Shard | None]) -> tuple[tuple[int, ...], bytes] | None:
        """The first surviving global parity as `(coefficients, bytes)`."""
        base = self.params.k + self.params.l
        for offset in range(self.params.r):
            shard = shards[base + offset]
            if shard is not None:
                return GLOBAL_COEFFICIENTS[offset], shard.data
        return None

    def _recompute(self, shard_id: int, data: list[bytes | None]) -> bytes:
        """Recompute any shard from a complete set of data shards."""
        k = self.params.k
        if shard_id < k:
            known = data[shard_id]
            assert known is not None
            return known

        shard_len = next(len(chunk) for chunk in data if chunk is not None)

        if shard_id < k + self.params.l:
            parity = bytearray(shard_len)
            for member in self.group_data_ids(shard_id - k):
                known = data[member]
                assert known is not None
                for column in range(shard_len):
                    parity[column] ^= known[column]
            return bytes(parity)

        coefficients = GLOBAL_COEFFICIENTS[shard_id - k - self.params.l]
        parity = bytearray(shard_len)
        for column in range(shard_len):
            accumulator = 0
            for index, chunk in enumerate(data):
                assert chunk is not None
                accumulator ^= self.gf.mul(coefficients[index], chunk[column])
            parity[column] = accumulator
        return bytes(parity)

    def group_data_ids(self, group: int) -> list[int]:
        """Data shard ids in local group `group`, for equal-sized groups."""
        start = group * GROUP_SIZE
        return list(range(start, start + GROUP_SIZE))

    def local_group_of(self, shard_id: int) -> list[int] | None:
        """The local group covering `shard_id` — its data shards plus its parity.

        `None` for a global parity: globals span every group and therefore
        belong to none, which is why losing one always costs the full `k` reads.
        """
        k = self.params.k
        if shard_id < k:
            group = shard_id // GROUP_SIZE
        elif shard_id < k + self.params.l:
            group = shard_id - k
        else:
            return None
        return [*self.group_data_ids(group), k + group]


def _require(shard: Shard | None) -> Shard:
    if shard is None:  # pragma: no cover - guarded by the caller
        raise InvalidLayout("expected a present shard")
    return shard
