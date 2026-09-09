"""The durability calculator — turning `(k, m, AFR, repair window)` into nines.

Erasure coding does not magically equal eleven nines. Durability is a function
of the code's parameters, how often a disk dies, and how fast a dead one is
replaced — and the last of those is the one people forget. This module turns
those inputs into an annual survival probability and a nines count you can put
in a design doc and defend.

## The model

```text
p       = AFR × (repair_hours / 8760)          one shard dies inside one window
P(loss) = Σ_{j=m+1..n} C(n,j) · p^j · (1−p)^{n−j}   more than m die in one window
annual  = (1 − P(loss)) ^ (8760 / repair_hours)
nines   = −log10(1 − annual)
```

Three things worth noticing:

**The repair window is the dominant term.** It appears twice — once making each
individual failure more likely, once as the number of independent chances per
year. Halving your rebuild time is worth far more than adding a parity shard,
which is exactly the argument LRC makes.

**The full binomial sum, not just the leading term.** The `j = m+1` term
dominates, and truncating there is the usual shortcut — but it understates the
loss probability, and understating loss is the wrong direction to be sloppy in
when the output is a promise about data.

**Independence is assumed and is the model's biggest lie.** Real failures
correlate: same batch, same rack, same power event, same firmware bug. The
number this produces is an upper bound on a fleet that behaves ideally, and the
right way to use it is comparatively — RS(4,2) versus RS(17,3) under the same
assumptions — rather than as an absolute promise.

Reference points from `docs/12-how-erasure-coding-works.md` §7, order of
magnitude rather than bit-exact:

- Backblaze `(17,3)`, AFR ≈ 0.00405, 156 h → about 11 nines
- Lab RS(4,2), same AFR and window → about 9 nines
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import Unsupported

__all__ = ["DurabilityInput", "DurabilityReport", "compute_durability"]

HOURS_PER_YEAR = 8760.0


@dataclass(frozen=True, slots=True)
class DurabilityInput:
    """The four numbers the model needs."""

    k: int
    """Data shards."""
    m: int
    """Parity shards — the code survives any `m` simultaneous losses."""
    annual_failure_rate: float
    """Per-shard AFR, e.g. `0.00405` for about 0.4%."""
    repair_window_hours: float
    """Hours to *detect and rebuild* one lost shard. Detection is part of it,
    and forgetting that is how people talk themselves into optimistic numbers."""

    @property
    def n(self) -> int:
        return self.k + self.m

    @classmethod
    def backblaze_17_3(cls) -> DurabilityInput:
        """Backblaze's published shape."""
        return cls(k=17, m=3, annual_failure_rate=0.00405, repair_window_hours=156.0)

    @classmethod
    def lab_rs_4_2(cls) -> DurabilityInput:
        """The lab code, same AFR and window, for a like-for-like comparison."""
        return cls(k=4, m=2, annual_failure_rate=0.00405, repair_window_hours=156.0)


@dataclass(frozen=True, slots=True)
class DurabilityReport:
    """The result. Put the assumptions next to these numbers, always."""

    n: int
    p_fail_in_window: float
    p_loss_in_window: float
    windows_per_year: float
    annual_durability: float
    nines: float
    """`−log10(1 − durability)` — the count of leading nines."""


def compute_durability(spec: DurabilityInput) -> DurabilityReport:
    """Compute annual durability and nines. Raises `Unsupported` on bad input."""
    if spec.k <= 0:
        raise Unsupported("k must be > 0")
    if spec.repair_window_hours <= 0:
        raise Unsupported("repair_window_hours must be > 0")
    if not 0.0 <= spec.annual_failure_rate <= 1.0:
        raise Unsupported("annual_failure_rate must be in 0..=1")

    n = spec.n
    p = spec.annual_failure_rate * (spec.repair_window_hours / HOURS_PER_YEAR)
    if not 0.0 <= p <= 1.0:
        raise Unsupported(
            f"per-window failure probability {p} escaped 0..=1 (repair window longer than a year?)"
        )

    p_loss = sum(
        math.comb(n, j) * (p**j) * ((1.0 - p) ** (n - j)) for j in range(spec.m + 1, n + 1)
    )

    windows_per_year = HOURS_PER_YEAR / spec.repair_window_hours

    # Work in the *loss* tail rather than computing `1 - durability` directly.
    # Eleven nines means a loss probability around 1e-11; forming the durability
    # as a float near 1.0 and subtracting throws away every significant digit
    # below ~1e-16, so the nines count would be quantised noise. `log1p` and
    # `expm1` keep the small quantity small all the way through.
    log_survive_window = math.log1p(-p_loss)
    annual_loss = -math.expm1(windows_per_year * log_survive_window)
    annual_durability = 1.0 - annual_loss
    nines = -math.log10(annual_loss) if annual_loss > 0 else math.inf

    return DurabilityReport(
        n=n,
        p_fail_in_window=p,
        p_loss_in_window=p_loss,
        windows_per_year=windows_per_year,
        annual_durability=annual_durability,
        nines=nines,
    )
