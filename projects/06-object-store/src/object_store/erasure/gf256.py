"""GF(2⁸) arithmetic — the number system every shard byte lives in.

Ordinary integer arithmetic is useless here: it overflows past 255, and division
does not come back to whole numbers. Erasure coding needs a field that is
**closed over `0..255`**, has a multiplicative inverse for every nonzero element
(so the reconstruction matrix can be inverted), and where addition is its own
inverse. That field is GF(2⁸).

## The three facts that make it work

**Addition is XOR.** `a + b = a - b = a ^ b`. There is no carry, so a parity
shard can be un-mixed with the same operation that mixed it — which is why plain
XOR parity (RAID-5, and LRC's local groups) needs no field theory at all.

**Multiplication is polynomial multiplication modulo `0x11D`.** Treat a byte as
a degree-7 polynomial over GF(2); multiplying two gives up to degree 14, which
no longer fits in a byte, so the result is reduced modulo the irreducible
polynomial `x⁸ + x⁴ + x³ + x² + 1`. That is the AES and Linux RAID-6 convention,
which matters only for interoperability — any irreducible polynomial of degree 8
gives an isomorphic field.

**Every nonzero element is a power of 2.** `2` is a generator: repeatedly
doubling from 1 walks all 255 nonzero elements before returning to 1. So
multiplication becomes addition of exponents — `log` and `antilog` tables turn a
loop of shifts and conditional XORs into two lookups and an add, which is what
makes encoding fast enough to run on every byte.

The tables are checked against the hand-worked products in
`docs/12-how-erasure-coding-works.md` §3–§4. A wrong table does not raise
anything; it silently corrupts every shard it ever touches, and you find out
during a restore.
"""

from __future__ import annotations

from . import Singular

__all__ = ["Gf256"]


class Gf256:
    """GF(2⁸) with the AES / RAID-6 reduction polynomial `0x11D`.

    Build one and reuse it — the tables are 512 bytes and rebuilding them per
    call would dominate the arithmetic they exist to speed up.
    """

    REDUCTION_POLY = 0x11D
    """`x⁸ + x⁴ + x³ + x² + 1`."""

    REDUCTION_BYTE = REDUCTION_POLY & 0xFF
    """The low byte, XOR'd in by `xtime` after an overflow."""

    HIGH_BIT = 0x80
    MUL_ORDER = 255
    """The multiplicative group's order — exponents are taken modulo this."""

    __slots__ = ("antilog", "log")

    def __init__(self) -> None:
        log = [0] * 256
        antilog = [0] * 256
        value = 1
        for exponent in range(self.MUL_ORDER):
            log[value] = exponent
            antilog[exponent] = value
            value = self.xtime(value)
        # `2**255 == 2**0 == 1`, so index 255 mirrors index 0. Having it means
        # `mul` can add two exponents up to 254 each and index without a modulo
        # in the common case.
        antilog[self.MUL_ORDER] = antilog[0]
        self.log = log
        self.antilog = antilog

    @staticmethod
    def xtime(a: int) -> int:
        """Multiply by 2: shift left, and reduce if the high bit overflowed.

        The primitive everything else is built from, and the one operation worth
        being able to do on paper — it is how the tables above get filled and how
        the RAID-6 Q row is hand-checked.
        """
        shifted = (a << 1) & 0xFF
        return shifted ^ Gf256.REDUCTION_BYTE if a & Gf256.HIGH_BIT else shifted

    @staticmethod
    def add(a: int, b: int) -> int:
        """Addition, which is also subtraction, which is XOR."""
        return a ^ b

    def mul(self, a: int, b: int) -> int:
        """Multiply via the log tables."""
        if a == 0 or b == 0:
            # Not a table lookup: `log[0]` is undefined (no exponent gives 0),
            # so zero has to be special-cased rather than encoded.
            return 0
        return self.antilog[(self.log[a] + self.log[b]) % self.MUL_ORDER]

    def inv(self, a: int) -> int:
        """The multiplicative inverse of a nonzero element."""
        if a == 0:
            raise Singular("multiplicative inverse of 0 is undefined")
        return self.antilog[self.MUL_ORDER - self.log[a]]

    def div(self, a: int, b: int) -> int:
        """`a / b = a · b⁻¹`."""
        return self.mul(a, self.inv(b))
