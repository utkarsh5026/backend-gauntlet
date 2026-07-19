# Windowed Aggregation & the Percentile Trap — From First Principles

> How a firehose of raw points becomes a handful of queryable rows *as it
> flows*, why you must never average percentiles, and how a stream decides a
> time window is "done". No prior knowledge of stream processing assumed.
>
> This prepares you for **V2** in [SPEC.md](../SPEC.md) — the rollup engine
> you'll build in [rollup.rs](../src/rollup.rs), producing
> [`RollupRow`](../src/model.rs)s from [`Aggregate`](../src/model.rs)s. Card 2
> in [CONCEPTS.md](../CONCEPTS.md) is the checklist this doc unlocks.

---

## 0. The one sentence to hold onto

**Fold the point stream into per-`(series, window)` running summaries in a
single pass — and for percentiles, carry a *mergeable sketch of the
distribution*, never the percentile itself, because percentiles don't
average.**

---

## 1. The problem: the query can't touch the raw points

A dashboard asks: *"p99 latency for service=api, last 6 hours."* The naive
answer is `SELECT ... WHERE ts > now() - 6h` over raw points. Scale it:

| Load | Raw points the query must scan |
| --- | --- |
| 200 series × 1 point/sec × 6 h | 4,320,000 points |
| Real fleet: 1M points/sec | **86.4 billion points per day** |

No store scans billions of rows per dashboard refresh. But notice what the
dashboard actually *renders*: one pixel per minute per series. Pre-aggregate
into 60-second windows and those same 6 hours become:

```
200 series × 360 windows = 72,000 rows      (60× fewer — and it stays 60×
                                             ahead no matter how hot the
                                             points-per-second firehose gets)
```

That's the whole idea of a rollup: **do the `GROUP BY` while the data flows
past, so the query never meets the firehose.** The engine that does it is the
piece you'd normally get from Flink, Materialize, or a TSDB — and it's what
[rollup.rs](../src/rollup.rs) scaffolds.

---

## 2. Tumbling windows: how a point finds its bucket

A **tumbling window** chops time into fixed, non-overlapping slots — this
project's default is 60 s (`WINDOW_SECS=60` in
[.env.example](../.env.example)). A point's slot is its timestamp **snapped
down** to a multiple of the width:

```
window_start = timestamp − (timestamp mod width)
```

Real values (unix seconds, 60 s windows — verified arithmetic):

| Point timestamp | window_start | Why |
| --- | --- | --- |
| 1719600037 | 1719600000 | 37 s into the window |
| 1719600059 | 1719600000 | last second, same window |
| 1719600060 | 1719600060 | first second of the *next* window |
| 1719599999 | 1719599940 | the window before |

Every point in `[start, start + 60s)` shares one bucket key — exactly the
[`WindowKey { series_id, window_start }`](../src/model.rs) the scaffold defines.

```
tumbling (this project):        sliding/hopping (not this project):
|--w1--|--w2--|--w3--|          |----w1----|
                                    |----w2----|       ← overlap: each point
each point lands in                     |----w3----|     lands in SEVERAL
exactly ONE window                                       windows
```

Tumbling is the cheap one — one bucket per point — and it's all a rollup store
needs. Sliding windows ("the last 5 minutes, recomputed every 10 s") cost a
multiple of that; know the distinction, build the tumbling one.

---

## 3. Online aggregation: fold, never hoard

For each open bucket, keep **running** aggregates, updated in one pass as each
point arrives — the fields already laid out in
[`Aggregate`](../src/model.rs):

| Point arrives (value) | count | sum | min | max | last |
| --- | --- | --- | --- | --- | --- |
| 0.91 | 1 | 0.91 | 0.91 | 0.91 | 0.91 |
| 0.12 | 2 | 1.03 | 0.12 | 0.91 | 0.12 |
| 0.55 | 3 | 1.58 | 0.12 | 0.91 | 0.55 |

Constant memory per bucket, no matter how many points flow through it. The
tempting alternative — push every raw value into a `Vec`, compute at flush —
is what the SPEC calls "deferring the work and the memory blow-up": a hot
series at 10k points/sec holds 600k `f64`s per open window, multiplied by
every open `(series, window)`. The memory shape of "defer" is *O(points)*;
the memory shape of "fold" is *O(series × open windows)*. Only the second one
survives contact with a firehose.

---

## 4. The percentile trap: why p99s don't average

`count/sum/min/max` fold happily. Percentiles do not, and this is the trap
with teeth. Suppose two 1-minute windows roll up into one 2-minute view, and
each stored "its p99":

**Window A:** 1,000 requests, all 10 ms → p99(A) = 10 ms
**Window B:** 10 requests, all 500 ms → p99(B) = 500 ms

Rolled up "the obvious way": `avg(10, 500) = 255 ms`.

The truth: the combined minute has 1,010 requests of which only 10 (0.99%) are
slow — so the true combined p99 is **10 ms**. The average reported **255 ms, a
25× overestimate**. Now keep the *same two window p99s* but change the mix —
90 fast requests in A instead of 1,000:

**A:** 90 × 10 ms (p99 = 10 ms) **B:** 10 × 500 ms (p99 = 500 ms)
→ combined: 100 requests, 10% slow → true p99 = **500 ms**. The same
`avg = 255 ms` is now a **2× underestimate**.

Same pair of inputs, opposite errors. That's the proof that `avg(p99₁, p99₂)`
carries no information: a percentile is a *rank* statistic — "the value below
which 99% of the points fall" — and once computed, it has discarded the
distribution (how many points, how spread out) that any combination would
need. Both numbers *look* plausible on a graph, which is why CONCEPTS.md calls
this the trap where "every graph is fiction and nobody notices."

And this pipeline *must* combine windows: 1 m → 5 m → 1 h rollups (the ladder
sketched as a TODO in [0001_init.sql](../migrations/0001_init.sql)), and
someday "p99 across all 20 hosts". Storing the p99 number per window makes
every one of those combinations meaningless.

---

## 5. The fix: carry a mergeable sketch of the distribution

Since the percentile can't be combined, store something that *can*, and
compute the percentile at the end. That something is a **sketch**: a
fixed-size summary of the distribution supporting two operations:

```
merge(a, b)  -> sketch       // combine two windows' summaries
quantile(q)  -> f64          // ask the combined summary for p50/p95/p99
```

with the contract that `merge(a, b).quantile(q)` ≈ the true quantile of the
union of both windows' points (within a known error bound), and that the
sketch stays **bounded in size** no matter how many points feed it.
Mergeability is the property that makes the whole rollup ladder
mathematically legitimate.

The SPEC offers you two families, and choosing is the V2 design decision:

| | **Bounded histogram** (fixed buckets — the Prometheus/HDR approach) | **t-digest** (adaptive centroids) |
| --- | --- | --- |
| Idea | Pre-chosen bucket edges; each point bumps one counter | Small set of centroids that adapt to the data, densest at the tails |
| Merge | Add counters bucket-by-bucket — trivially exact | Merge centroid sets — more involved |
| Accuracy | Exact *to bucket width*: the error is "which bucket", so you must choose edges that suit your value range up front | Accurate especially at extreme quantiles, without pre-chosen edges |
| Cost | Cheap, simple, fixed size | Cleverer, adaptive size, more code |
| In the wild | Prometheus histograms — 500 instances' histograms combine with plain `sum()` *because* fixed buckets merge exactly; the price paid is bucket-resolution error | Elasticsearch percentiles, many APM backends |

Either satisfies V2. What the SPEC requires is only: *mergeable*,
*constant-ish space per series*, and a **measured** error bound (the tests
sketched in [rollup.rs](../src/rollup.rs) ask you to feed a known distribution
and assert the reported quantile is within your bound — and that two merged
sketches answer for the combined distribution). Which one you build, and how,
is yours — `/hint` for nudges, `/quest` to build it against acceptance tests.

The scaffold points at exactly where it lands: the
`TODO(V2)` inside [`Aggregate`](../src/model.rs) ("You cannot store the
percentile itself… Store the *distribution*"), surfacing as `p50`/`p99` in
[`RollupRow`](../src/model.rs) when a window closes.

---

## 6. Watermarks: when is a window *done*?

Points arrive out of order — networks retry, agents buffer, clocks skew. So
"the 18:40 window" can still receive points at 18:41:05. When may you flush
it?

The standard answer is a **watermark**: pick a grace period `G` and declare
*"no points older than `now − G` are coming."* A window `[start, start+60s)`
closes when `now ≥ start + 60s + G` — precisely the rule
[`flush_ready()`](../src/rollup.rs) asks you to implement, with `G` =
`GRACE_SECS=10` from [.env.example](../.env.example). A point that arrives
*after* its window flushed is **late**, and you need a policy:

| Grace too eager (small G) | Grace too lazy (large G) |
| --- | --- |
| Late points miss their window → undercounted history | Live graphs lag: a window isn't visible until `start + 60s + G` |
| | Open-window map holds more state → memory grows (this map *is* your RAM footprint — [`open_windows()`](../src/rollup.rs) is the gauge) |

For the late points themselves: **drop and count** (a `points_late` counter —
cheap, honest, the common default) or **re-open the window** (accurate, but
now a "closed" window can change after being written and pushed — everything
downstream must cope). The SPEC leaves the policy to you; what it does not
allow is the silent third option, letting stale windows sit in the map
forever — that's the unbounded-memory failure the doc-comment on
[`Rollup.open`](../src/rollup.rs) warns about.

One honest scaffold note: `flush_ready(now)` compares windows against
*wall-clock* time — a processing-time watermark. Real stream processors
(Flink, Beam) track watermarks in *event time* (the max timestamp seen, minus
grace), which behaves better when replaying a backlog: a replay at full speed
doesn't prematurely close old windows. The scaffold's shape is the simpler
one; noticing where it bends during a backlog replay is part of owning the
concept.

---

## 7. The design space V2 leaves to you

1. **The sketch** — histogram vs t-digest, and (if histogram) the bucket-edge
   scheme; then *measure* its error, don't assume it.
2. **Late-point policy** — drop-and-count vs re-open, and what that means for
   downstream consumers.
3. **The grace value** — trade data completeness against live-graph lag and
   map size (you have the env knobs to experiment).
4. **Merging** — how a 1m sketch state rolls into 5m/1h (the
   [migration's](../migrations/0001_init.sql) multi-resolution TODO is the
   storage side of this decision).

---

## 8. Mental-model summary

| Concept | One-liner |
| --- | --- |
| Rollup | Do the `GROUP BY` as data flows, so queries read thousands of rows, not billions of points |
| Tumbling window | Fixed non-overlapping slots; `window_start = ts − (ts mod width)`; one bucket per point |
| Online aggregation | Fold into count/sum/min/max/last; memory is O(series × open windows), never O(points) |
| The percentile trap | avg(p99s) gave 255 ms for a true p99 of 10 ms *and* of 500 ms — a rank statistic discards the distribution |
| Mergeable sketch | Fixed-size distribution summary with `merge` + `quantile`; makes 1m→5m→1h legitimate |
| Histogram vs t-digest | Exact-merge + bucket-width error vs adaptive + tail-accurate + more complex |
| Watermark | "Nothing older than now − grace is coming" → flush; late points get a *policy*, not silence |

## 9. Where you'll build this

- [`Rollup::ingest()`](../src/rollup.rs) — snap to window, upsert the running
  aggregate, feed the sketch (`todo!()`).
- [`Rollup::flush_ready()`](../src/rollup.rs) — the watermark flush
  (`todo!()`).
- [`Rollup::drain_all()`](../src/rollup.rs) — graceful-shutdown drain
  (`todo!()`).
- The sketch field itself — the `TODO(V2)` in
  [`Aggregate`](../src/model.rs).
- The tests sketched in [rollup.rs](../src/rollup.rs): window boundaries,
  watermark timing, late-point policy, and the sketch's measured error +
  mergeability.

You own it (Card 2 of [CONCEPTS.md](../CONCEPTS.md)) when you can explain:
tumbling vs sliding and the snap; why raw value lists are forbidden; the
two-window counterexample from §4 unprompted; the mergeability requirement
and the histogram/t-digest tradeoff; and what each direction of the watermark
knob costs.
