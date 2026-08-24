"""V4 — Provisioned throughput & hot partitions.

The most common DynamoDB incident there is: the table is provisioned for 10,000
writes/sec, the dashboard says 8% utilisation, and every write is throttled. The
reason is that capacity is **not a table-level budget**. It is divided across
partitions, and a single partition has a hard ceiling of its own. Pour all your
traffic through one partition key and you hit that ceiling while the table as a
whole sits idle.

That is why key design *is* a capacity decision, and why "just raise the
provisioned capacity" does not fix a hot key. Building the governor is the only
way this really lands.

Scaffold state: the cost model and the limiter are modelled; the metering and the
token-bucket arithmetic raise.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "CapacityKind",
    "ConsumedCapacity",
    "PartitionLimiter",
    "read_units",
    "write_units",
]

# DynamoDB's real cost model, and the numbers the SPEC grades against.
WRITE_UNIT_BYTES = 1024
READ_UNIT_BYTES = 4096


class CapacityKind(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(slots=True)
class ConsumedCapacity:
    """What one operation cost, reported back on every response.

    Split by target because a write to a table with three GSIs costs the base
    table one write and each index one more — surfacing that is how write
    amplification stops being invisible.
    """

    table: float = 0.0
    indexes: dict[str, float] | None = None

    @property
    def total(self) -> float:
        return self.table + sum((self.indexes or {}).values())


def write_units(item_bytes: int) -> float:
    """WCU consumed writing an item of this size."""
    # TODO(V4): 1 WCU per 1 KB, rounded UP — a 1-byte item and a 1024-byte item
    # cost the same, and a 1025-byte item costs two. That rounding is why "just
    # add one more attribute" occasionally doubles a bill.
    raise NotImplementedError("V4: write capacity units for an item of this size")


def read_units(item_bytes: int, *, consistent: bool) -> float:
    """RCU consumed reading an item of this size."""
    # TODO(V4): 1 RCU per 4 KB rounded up for a strongly-consistent read; an
    # eventually-consistent read costs HALF that. The discount is the whole
    # economic argument for eventual consistency — make the number say so.
    raise NotImplementedError("V4: read capacity units, discounted when eventually consistent")


class PartitionLimiter:
    """A token bucket per partition — the thing that makes a hot key throttle.

    One of these per (table, partition) pair, plus one for the table as a whole.
    A request must pass **both**: the table budget stops you overspending overall,
    the partition ceiling stops one key monopolising it. A request rejected by the
    partition bucket while the table bucket is nearly full *is* the hot-shard
    failure, and your metrics should make that visible rather than just erroring.
    """

    def __init__(self, capacity_per_second: float, burst_seconds: float) -> None:
        if capacity_per_second <= 0:
            raise ValueError("capacity must be > 0")
        self._rate = capacity_per_second
        self._burst = burst_seconds
        # TODO(V4): token-bucket state. You need the current token count and the
        # timestamp it was last updated:
        #
        #   self._tokens: float
        #   self._updated_at: float   # time.monotonic(), NEVER time.time() —
        #                             # a clock adjustment must not mint or
        #                             # destroy capacity
        #
        # Refill lazily on access (tokens += elapsed * rate, clamped to the burst
        # bank) rather than with a background ticker: no timer to schedule, and it
        # stays exact regardless of how irregularly requests arrive.

    @property
    def rate(self) -> float:
        return self._rate

    def try_consume(self, units: float) -> bool:
        """Spend `units` if they are available. Returns False when throttled."""
        # TODO(V4): refill by elapsed time, clamp to the burst bank
        # (rate * burst_seconds), then spend or refuse. Note the asymmetry
        # DynamoDB actually has: a request that exceeds the bucket is REFUSED, not
        # queued — throttling is backpressure the caller must retry, not a delay
        # you absorb on their behalf.
        raise NotImplementedError("V4: refill by elapsed time, then spend or throttle")

    def available(self) -> float:
        """Tokens available right now — for the capacity-utilisation metric."""
        raise NotImplementedError("V4: current token count after a lazy refill")
