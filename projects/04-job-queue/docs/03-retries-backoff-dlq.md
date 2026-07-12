# V3 — Retries, Backoff + Jitter, and the Dead-Letter Queue

> Teaches how to fail *well*: retry with growing, randomized delays, and give up into
> a parking lot instead of looping forever. No prior knowledge assumed.
>
> Prepares you for **V3** in [`src/retry.rs`](../src/retry.rs)
> (`RetryPolicy::backoff`, `nack`). Concept overview:
> [`00-how-job-queues-work.md`](./00-how-job-queues-work.md) §9.
> This doc goes deeper on the two failure modes and the formula you'll implement.

---

## The one sentence to hold onto

**Retries must *grow*, *scatter*, and *end*: exponential backoff spreads load over
time, jitter decorrelates the herd, and a dead-letter queue stops one broken job from
becoming an infinite outage.**

---

## The problem before the solution

Jobs fail — a downstream API blips, a payload is malformed. The worker's failure path
is already wired: [`process_one`](../src/worker.rs) catches the `Err` and calls
[`nack`](../src/retry.rs). The *policy* inside `nack` is everything. Two distinct
failure modes will hurt you, and they pull in the same direction:

### Failure mode 1 — the poison message

A job whose payload *always* crashes the handler (a malformed record, a bug). Retry it
immediately, forever:

```
12:00:00.000  run job 88 → fail
12:00:00.001  run job 88 → fail
12:00:00.002  run job 88 → fail
...  thousands of times a second, one worker pinned at 100% CPU, going nowhere
```

One bad job now occupies a worker permanently. Multiply by a handful of poison messages
and your whole pool is busy failing.

### Failure mode 2 — the retry stampede (self-inflicted thundering herd)

A downstream API goes down for 10 seconds; 10,000 jobs fail in that window. If every
job retries on a **fixed** 5-second timer:

```
t=0s     downstream dies, 10,000 jobs fail
t=5s     ALL 10,000 retry at once → re-create the exact spike that broke it
t=10s    ALL 10,000 retry again → the API never gets room to recover
```

You've turned a brief outage into a self-sustaining hammer. And here's the subtle
part: **plain exponential backoff doesn't fix this by itself.** If all 10,000 jobs
share the schedule `t+2, t+4, t+8`, they still retry *together* — just at
exponentially spaced instants. They're still synchronized.

---

## The idea: grow, scatter, end

The full retry policy is a small formula with three terms, each fixing one thing:

```
delay = min( cap,  base × 2^(attempt-1) )  ×  random_jitter
        └cap┘      └─── exponential ────┘      └─ decorrelate ─┘
```

| Term | Fixes | Remove it and… |
|---|---|---|
| `base × 2^(attempt-1)` | gives a struggling downstream *growing* breathing room | retries hammer a service that's already down |
| `min(cap, …)` | ceiling so `2^n` doesn't schedule a job absurdly far out | uncapped, ~20 failures → a job waits `2^19` s ≈ **6 days** |
| `× random_jitter` | scatters the herd across time | even exponential clusters — everyone retries at the same instants |

Worked example with the scaffold defaults (`base_delay = 1s`, `max_delay = 300s` from
[`RetryPolicy::default`](../src/retry.rs)) — the *nominal* curve before jitter:

| attempt | `base × 2^(a-1)` | after cap (300s) |
|--------:|------------------|------------------|
| 1 | 1s | 1s |
| 2 | 2s | 2s |
| 3 | 4s | 4s |
| 4 | 8s | 8s |
| 5 | 16s | 16s |
| … | … | … |
| 9 | 256s | 256s |
| 10 | 512s | **300s** (capped) |
| 11 | 1024s | **300s** (capped) |

Then jitter perturbs each value so two jobs that failed together don't retry in lockstep.

### The dead-letter queue: retries must *end*

Backoff spaces retries out, but a poison message would still retry forever, just
slowly. So retries have a budget: after `max_attempts` (scaffold default 5), the job
stops retrying and moves to the **dead-letter queue** — the terminal `dead` state (or a
separate table). It's set aside so it stops wasting workers, but **kept and
inspectable** so you can see *why* it failed and requeue it after a fix.

> A dead job you can't see or requeue is **silent data loss**. "Inspectable +
> requeueable" is a hard requirement, not a nicety.

---

## The decisions V3 asks *you* to make

### 1. Which jitter?

"Add randomness" isn't one thing. The common variants (from AWS's canonical
"Exponential Backoff and Jitter" post):

| Variant | Formula (sketch) | Character |
|---|---|---|
| **Full jitter** | `random(0, min(cap, base·2^n))` | maximum spread; a retry can come almost immediately |
| **Equal jitter** | `half + random(0, half)` | keeps a floor delay, still spreads |
| **Decorrelated** | `random(base, prev·3)` capped | grows based on the previous delay |

Pick one and be able to say why. The scaffold hands you `base_delay` and `max_delay`;
the jitter policy is yours (add `rand` to the workspace deps when you implement it).

### 2. Classify errors — should *every* failure retry?

A malformed-payload error will fail *identically* on every attempt. Retrying it burns
the entire backoff budget (potentially hours) only to land in the DLQ anyway. The
stronger design distinguishes:

| Error class | Right move |
|---|---|
| **Transient** (timeout, 503, connection reset) | retry with backoff — it might succeed |
| **Permanent** (validation error, 400, unknown `kind`) | straight to the DLQ — retrying can't help |

Whether you implement this classification (vs. retrying everything uniformly) is a
design choice to record.

### 3. DLQ representation

Terminal `state='dead'` in the same table, or a separate `dead_letter` table? Either
works; the SPEC only requires that it's terminal, inspectable, and requeueable. Decide
and note it in [`04-design.md`](./04-design.md).

---

## Depth probes (you own V3 when you can answer)

- Why do immediate retries turn *one* poison message into 100% worker occupancy?
- The herd argument: why do fixed intervals *and* bare `2^n` both cluster retries, and
  what exactly does jitter decorrelate?
- Retried jobs may execute *out of order* relative to newer jobs. When does that
  matter, and what would ordered retries cost you?

---

## Where you'll build this

| Piece | Location |
|---|---|
| the backoff curve | [`RetryPolicy::backoff`](../src/retry.rs) `todo!("V3: exponential backoff…")` |
| retry-or-dead-letter decision | [`nack`](../src/retry.rs) `todo!("V3: retry-with-backoff or dead-letter…")` |
| DLQ inspect + requeue surface | your addition to [`src/routes.rs`](../src/routes.rs) |
| backoff property tests (`proptest`) | `tests` module in [`src/retry.rs`](../src/retry.rs) |

**This doc unlocks (V3 "Done when ALL true"):** on failure, bump `attempts`/record
`last_error`/reschedule with backoff when attempts remain; backoff is exponential +
jittered + capped; a poison message lands in the DLQ and stops (no hot loop); the DLQ
is inspectable and requeueable.

**Ready to build?** `/hint 04 V3` for nudges, or `/quest 04 V3` for the guided session
(including the always-failing-job test that asserts it backs off and dead-letters
rather than hot-looping). The `proptest` dev-dependency is already there to check your
curve is monotonic, capped, and actually jittered.
</content>
