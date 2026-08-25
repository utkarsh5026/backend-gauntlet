# How Provisioned Capacity and Hot Partitions Work — From First Principles

> Why a table provisioned for 10,000 writes/sec throttles you at 8% utilisation,
> what a token bucket actually is, and why key design is a **capacity** decision
> rather than a query-performance one.
> No prior knowledge of rate limiting, token buckets, or capacity planning assumed.
>
> Prepares you for **V4** and the 🐉 **Hot Shard** boss fight in [SPEC.md](../SPEC.md).
> Anchored to [throughput.py](../src/dynamodb_core/throughput.py),
> [state.py](../src/dynamodb_core/state.py) and [config.py](../src/dynamodb_core/config.py).

---

## 0. The one sentence to hold onto

**Capacity is not a table-level budget you draw down — it is divided across
partitions, and a single partition key has a ceiling of its own.**

Every confusing thing about DynamoDB throttling follows from that. The dashboard
shows a table-wide average; the throttle happens at a partition. Those two numbers
can disagree wildly, and when they do, raising the table's capacity changes
nothing.

---

## 1. The incident, first

It's Black Friday. Your table is provisioned for 10,000 writes/sec. The dashboard
says **8% utilisation**. Every write is being throttled.

You do the obvious thing and double the provisioned capacity. Utilisation drops to
4%. **Every write is still being throttled.**

```
   ┌──────────────────────────────────────────────────────────────┐
   │  Table: 10,000 WCU/s provisioned                             │
   │                                                              │
   │   partition A   partition B   partition C   partition D      │
   │   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐      │
   │   │ ███████ │   │ ▁       │   │ ▁       │   │ ▁       │      │
   │   │ AT CAP  │   │  idle   │   │  idle   │   │  idle   │      │
   │   └─────────┘   └─────────┘   └─────────┘   └─────────┘      │
   │    1,000/s        ~0/s          ~0/s          ~0/s           │
   │                                                              │
   │  table utilisation: 1,000 / 10,000 = 10%                     │
   │  partition A utilisation: 1,000 / 1,000 = 100%  ← THROTTLING │
   └──────────────────────────────────────────────────────────────┘
```

One product's item — one partition key — is taking every write. The table is fine.
The table has always been fine.

This is the most common DynamoDB production incident there is, and the SPEC's
position is that you only really understand it by building the governor yourself.

### ⚠️ A configuration detail that will hide the whole effect

The hot-shard gap only exists when the **table** ceiling is higher than the
**partition** ceiling. Check what this project actually ships
([config.py](../src/dynamodb_core/config.py), [.env.example](../.env.example),
[tests/conftest.py](../tests/conftest.py)) — the three configurations disagree, on
purpose and by accident:

| Configuration | Table WCU/s | Partition WCU/s | Ratio | Table util. when one key throttles |
| --- | --- | --- | --- | --- |
| `config.py` / `.env.example` defaults | 1,000 | 1,000 | **1 : 1** | **100% — the effect is invisible** |
| `tests/conftest.py` fixture | 100 | 10 | 10 : 1 | 10% ✅ |
| The boss fight's narrative | 10,000 | 1,000 | 10 : 1 | 10% ✅ |

*(All verified.)* With the **shipped defaults the two ceilings are equal**, so a
single hot key exhausts the table budget at the same instant it exhausts its
partition — utilisation reads 100% and there is no paradox to observe. The test
fixture deliberately opens a 10:1 gap, which is why it can exercise the failure at
all.

So: if you run the bench against the defaults and can't reproduce the hot shard,
your limiter is probably fine and your **provisioning** is wrong. Raise the table
capacity (or lower the partition ceiling) so the gap exists, and record the ratio
you benchmarked at in `docs/23-benchmarks.md`. The `< 15%` boss target needs a
ratio of at least ~7:1.

---

## 2. First: what does an operation cost?

Before you can limit anything you need a unit. DynamoDB's is deliberately
crude — and the crudeness is where the surprises live.
[throughput.py](../src/dynamodb_core/throughput.py) pins the constants:

```python
WRITE_UNIT_BYTES = 1024
READ_UNIT_BYTES = 4096
```

- **1 WCU** = one write of up to **1 KB**, rounded **up**.
- **1 RCU** = one strongly-consistent read of up to **4 KB**, rounded **up**.
- An **eventually-consistent read costs half** an RCU.

Rounding up is the part that bites. Verified:

| Item size | WCU | RCU (strong) | RCU (eventual) |
| --- | --- | --- | --- |
| 1 B | 1 | 1 | 0.5 |
| 1024 B | 1 | 1 | 0.5 |
| **1025 B** | **2** | 1 | 0.5 |
| 2048 B | 2 | 1 | 0.5 |
| 3000 B | 3 | 1 | 0.5 |
| 409600 B (the 400 KiB cap) | 400 | 100 | 50 |

A 1-byte item and a 1024-byte item cost the same. **A 1025-byte item costs
double.** As the scaffold puts it: *"that rounding is why 'just add one more
attribute' occasionally doubles a bill."* An innocuous schema change pushes your
average item over a KB boundary and your write costs jump ~100% overnight.

And the half-price eventual read is not a rounding detail — it's **the entire
economic argument for eventual consistency**, expressed as a number. When you
choose `ConsistentRead=false` you are buying a 50% discount with staleness. The
scaffold asks you to make the number say so.

Note the asymmetry between reads and writes: 4 KB per read unit vs 1 KB per write
unit means writes are 4× more expensive per byte, before you even count the index
amplification from [doc 03](03-how-secondary-indexes-work.md).

---

## 3. Metering the cost is not the same as limiting it

Two separate jobs, and they're graded separately:

1. **Metering** — report what each operation cost. `write_units` / `read_units`,
   surfaced in [`ConsumedCapacity`](../src/dynamodb_core/throughput.py) on every
   response.
2. **Limiting** — refuse operations that exceed the budget. `PartitionLimiter`.

A subtlety from V3 that connects them: *"a failed condition changes nothing while
still costing capacity."* The condition evaluation was real work, so it's metered
and billed even though nothing was written. Metering therefore can't live at the
end of the happy path — it has to survive the failure paths too.

Charging on `Scan` is the other one. A `Scan` is billed on what it **scanned**, not
what it returned — which is what makes the `scanned_count` field from
[doc 00](00-how-partition-keys-decide-placement.md#5-scanned_count-making-the-cost-visible)
a billing input rather than a debugging nicety.

---

## 4. The token bucket

The limiter is a **token bucket**, and it's worth deriving rather than accepting.

A bucket holds tokens. Tokens refill at a fixed rate. An operation spends tokens
equal to its cost. No tokens, no operation.

```
   rate = 1000 tokens/sec        bucket capacity = rate * burst_seconds
   ┌────────────────────────┐
   │ ●●●●●●●●●●●●●●         │  <── refills continuously at `rate`
   │                        │
   └────────────────────────┘
              │
              │  a write costing 2 WCU takes 2 tokens
              ▼
        enough tokens?  ──yes──> proceed
                        ──no───> ProvisionedThroughputExceeded (429, retryable)
```

Why a bucket rather than a simple counter reset every second?

| Approach | Problem |
| --- | --- |
| "Max N per fixed 1-second window" | Boundary abuse: N at 0.999s and N at 1.001s = 2N in 2ms. And a window that resets makes an evenly-paced client indistinguishable from a bursty one. |
| "Max N per sliding window" | Correct, but you must remember every request's timestamp within the window — memory proportional to traffic. |
| **Token bucket** | Constant state (a count and a timestamp), smooth rate limiting, and **burst is a first-class, tunable property** rather than an accident. |

That last point is the reason it wins here. Bursting isn't a bug you tolerate —
real traffic is bursty, and the bucket lets you say exactly *how* bursty is
acceptable via the bucket's depth.

### Burst, in this project's numbers

[config.py](../src/dynamodb_core/config.py) sets `burst_seconds = 300.0`. So the
bucket depth is `rate × 300`:

| Bucket | Rate | Depth (burst bank) | Meaning |
| --- | --- | --- | --- |
| Per-partition write | 1,000 WCU/s | 300,000 WCU | 5 minutes of unused capacity, bankable |
| Table write @ 10k | 10,000 WCU/s | 3,000,000 WCU | same, table-wide |

*(Verified.)* An idle partition banks capacity; a spike spends it. Your criterion:

> **Burst capacity** lets a short spike through; a sustained one does not.

Which is exactly the bucket's behaviour and needs no special-casing — a spike drains
a full bucket and succeeds; sustained overload drains it and then hits the refill
rate, which is the real ceiling. If you find yourself writing an `if is_burst:`
branch, you've probably reimplemented what the bucket already does.

### Two implementation notes the scaffold insists on

```python
        #   self._tokens: float
        #   self._updated_at: float   # time.monotonic(), NEVER time.time() —
        #                             # a clock adjustment must not mint or
        #                             # destroy capacity
```

**`time.monotonic()`, never `time.time()`.** Wall-clock time can jump — NTP
correction, DST, a VM resuming from suspend. A backwards jump computes negative
elapsed time (destroying capacity, or worse, going negative); a forwards jump mints
capacity from nothing. Monotonic time only ever moves forward.

**Refill lazily, on access.** `tokens += elapsed × rate`, clamped to the depth,
computed when someone asks — not by a background ticker.

> no timer to schedule, and it stays exact regardless of how irregularly requests
> arrive.

With one bucket per partition key and millions of keys, a per-bucket timer would be
millions of timers. Lazy refill is O(1) per request and O(0) when idle.

### Refuse, don't queue

```python
        # Note the asymmetry DynamoDB actually has: a request that exceeds the
        # bucket is REFUSED, not queued — throttling is backpressure the caller
        # must retry, not a delay you absorb on their behalf.
```

This is a real fork in the road and most rate limiters get it wrong. If you *queue*
an over-budget request:

- the queue grows without bound under sustained overload → memory exhaustion
- the caller sees latency instead of an error, so its timeout fires, so it retries,
  so now there are two copies queued
- you've converted a clear, actionable signal into an unclear, unactionable one

Refusing pushes the decision to the caller, who is the only one who knows whether
to retry, back off, shed the request, or fail the user. That's what backpressure
*is*. It's also why [`ProvisionedThroughputExceeded`](../src/dynamodb_core/errors.py)
sets `retryable = True` and the handler adds a `retry-after` header — the error is
actionable by design, and the contrast with `ConditionalCheckFailed`
(`retryable = False`) from [doc 02](02-how-conditional-writes-work.md) is the point.

---

## 5. Two buckets, and why the hot partition appears

Here is the mechanism that produces the incident in §1. The scaffold spells it out:

> One of these per (table, partition) pair, plus one for the table as a whole. A
> request must pass **both**: the table budget stops you overspending overall, the
> partition ceiling stops one key monopolising it.

```
        incoming write (2 WCU)
               │
               ▼
     ┌─────────────────────┐
     │ table bucket        │  10,000/s   ──> 9,998 tokens left, plenty
     └─────────────────────┘
               │ passed
               ▼
     ┌─────────────────────┐
     │ partition bucket    │   1,000/s   ──> EMPTY
     │ for hash("prod#42") │
     └─────────────────────┘
               │ refused
               ▼
     ProvisionedThroughputExceeded — while the table sits at 10%
```

And the definition of the hot shard, straight from the scaffold:

> A request rejected by the partition bucket while the table bucket is nearly full
> *is* the hot-shard failure, and your metrics should make that visible rather than
> just erroring.

That sentence contains the observability requirement. An operator staring at a
table-level utilisation graph **cannot see this**. Which is why the horizontal
checklist asks for:

> The **key distribution across partitions is observable** (per-partition item count
> and request rate) — you can *see* a hot partition forming before it throttles you.

Per-partition request rate is the metric that turns a 3am mystery into a glance.

---

## 6. Why key design *is* capacity design

Run the same load through different key designs and the ceiling changes without
anything else changing. *(Figures below use the **boss-fight** provisioning —
table 10,000 WCU/s, partition 1,000 WCU/s — not the shipped 1:1 defaults; see the
warning in §1.)*

| Key design | Distinct partition keys | Ceiling |
| --- | --- | --- |
| `pk = "GLOBAL"` | 1 | **1,000/s** — the table's 10,000 is unreachable |
| `pk = status` (5 values) | 5 | 5,000/s, unevenly split — `SHIPPED` gets most of it |
| `pk = customer_id` | ~millions | Table-limited, if traffic is spread |
| `pk = customer_id`, one whale customer | many, but skewed | The whale's partition caps at 1,000/s while others idle |

The last row is the honest one: a *good* key design still throttles under skew,
because real traffic is Zipfian. Verified, for 1,000 keys:

| Zipf exponent | Hottest key's share | Top 10 keys' share |
| --- | --- | --- |
| s = 1.0 | 13.4% | 39.1% |
| s = 1.2 | 23.1% | 56.9% |

At s=1.2, one key out of a thousand takes 23% of your traffic. This is why the boss
fight's arena includes a **Zipfian** workload alongside uniform and single-key: the
uniform case is the one that never happens.

Your V4 criterion asks you to prove the fix works:

> Spreading the identical load across many partition keys **removes** the
> throttling — measured, not asserted.

### The boss fight arithmetic, precisely

The boss wants: *"Re-spreading that identical load across ≥ 100 partition keys
raises accepted throughput by ≥ 20×."* Verified:

| Table capacity | 1 hot key | 100 keys | Ratio |
| --- | --- | --- | --- |
| 10,000/s | 1,000/s | 10,000/s | **10×** ← table budget becomes the new ceiling |
| 20,000/s | 1,000/s | 20,000/s | **20×** ✅ |
| 25,000/s | 1,000/s | 25,000/s | 25× |

So the ≥20× target requires the **table** provisioned at ≥ 20,000/s — which lines
up exactly with the boss's first criterion (≥20,000 writes/sec sustained "with
capacity provisioned to match"). At a 10,000 table you'd top out at 10× and fail
the criterion for a reason that has nothing to do with your code. **Provision the
arena to match the target**, and record the provisioning in
`docs/23-benchmarks.md` alongside the numbers.

Note the shape of the result: spreading the load doesn't remove the ceiling, it
*moves* it — from the partition bucket to the table bucket. That's the correct
outcome. The table budget is the one you can actually buy your way out of.

---

## 7. The design space (your decisions)

`docs/23-design.md` must record **"the capacity model, bucket granularity and burst
policy."** The open questions:

**Bucket lifecycle.** From the TODO in [state.py](../src/dynamodb_core/state.py):

> the per-PARTITION limiters live here too — one bucket per partition key, created
> on first touch and (importantly) evicted when idle, or a table with millions of
> keys leaks a bucket per key. A bounded LRU of buckets is the usual answer; note
> that **evicting a bucket forgives its debt**, which is a tradeoff worth recording.

That parenthetical is the interesting bit. An evicted bucket comes back full, so a
client that goes quiet just long enough to be evicted gets a fresh burst bank.
Whether that's acceptable depends on your eviction threshold versus your burst
window — and it's a genuine tension between memory safety and limiting accuracy.
There's no free answer; there's a documented one.

**Granularity.** One bucket per partition key is the obvious reading. But a table
with 10 million keys and a bounded LRU means most requests hit a cold bucket. Is
the bucket per *key*, or per *partition* (a group of keys)? Real DynamoDB does the
latter, which is why its hot-key behaviour is subtler than "one key, one ceiling."

**Adaptive capacity.** The concept-to-internalise line asks *"what adaptive capacity
can and cannot rescue."* Real DynamoDB will lend a hot partition unused capacity
from elsewhere in the table. It helps with *moderate*, *sustained* imbalance. It
cannot help with a single key exceeding a single partition's physical ceiling —
there's no amount of borrowed budget that makes one key live in two places. Worth
knowing before you conclude the platform should have saved you.

**Where I stop:** the refill arithmetic, the two-bucket check ordering, the eviction
policy and the metric surface are yours. `/hint` and `/quest` as usual.

---

## 8. Mental-model summary

| The instinct | The correction |
| --- | --- |
| "Utilisation is 8%, so I have headroom." | Table-wide average. One partition can be at 100% while the average is 8% — verified at exactly 10% with this project's defaults. |
| "Throttled? Raise the provisioned capacity." | Does nothing for a hot key. The partition ceiling doesn't move when the table's does. |
| "A 1-byte write is cheaper than a 1 KB write." | Identical — 1 WCU each. But 1025 bytes costs **2**. Rounding up is where bills surprise you. |
| "Eventual consistency is about latency." | It's priced: **half** an RCU. That discount is the whole economic argument. |
| "Rate limiting = count requests per second." | Fixed windows allow 2N at the boundary. A token bucket gives smooth limiting *and* makes burst an explicit dial. |
| "`time.time()` is fine for elapsed time." | A clock jump mints or destroys capacity. `time.monotonic()`, always. |
| "Refill with a background timer." | Millions of keys = millions of timers. Refill lazily on access: O(1) per request, exact regardless of arrival pattern. |
| "Throttled requests should be queued." | Refuse. Queuing grows unboundedly, converts a clear signal into latency, and multiplies work via client timeouts. |
| "Good key design means unique keys." | It means *evenly hit* keys. Real traffic is Zipfian — at s=1.2, one key in a thousand takes 23%. |
| "Spreading the load removes the ceiling." | It **moves** it — partition bucket → table bucket. That's the one you can buy more of. |

---

## 9. Where you'll build this

**Module:** [throughput.py](../src/dynamodb_core/throughput.py):

| `todo` | What it owes you |
| --- | --- |
| `write_units` | 1 WCU per 1 KB, rounded up. |
| `read_units` | 1 RCU per 4 KB rounded up; **half** when eventually consistent. |
| `PartitionLimiter.__init__` | Token-bucket state: `_tokens`, `_updated_at` on `time.monotonic()`. |
| `try_consume` | Lazy refill, clamp to `rate × burst_seconds`, then spend or **refuse**. |
| `available` | Token count after a lazy refill — backs the utilisation metric. |

Plus the per-partition bucket registry in
[`TableContext`](../src/dynamodb_core/state.py), with an eviction policy.

**Done-when criteria this doc unlocks** (from [SPEC.md](../SPEC.md) V4): consumed
capacity on every operation matching a documented cost model with the
strong/eventual split; a distinct **retryable** throttle error; a single-key
workload throttling while table utilisation stays low; burst letting a spike
through but not a sustained load; re-spreading removing the throttling; and a GSI
throttling on its **own** capacity without stopping base-table reads.

**Proof:** *"a bench scenario for each of uniform / skewed / single-key workloads
with throttle counts and table utilisation side by side."* Side by side is the
whole point — the two numbers are only interesting together.

**The boss fight** (🐉 The Hot Shard) lives here too. Its six criteria are in
[SPEC.md](../SPEC.md); the ones this doc arms you for are the hot-key reproduction
at `< 15%` table utilisation, the ≥20× re-spread (mind §6's provisioning), and the
write-amplification prediction within ±20% from
[doc 03](03-how-secondary-indexes-work.md#2-what-it-costs-write-amplification).

**Next:** [05-how-change-streams-work.md](05-how-change-streams-work.md) — the last
vertical, and the seam that makes everything you've built integrable.
