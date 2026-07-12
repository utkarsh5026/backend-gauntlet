# V1 — The Atomic Claim: `FOR UPDATE SKIP LOCKED`

> Teaches the single hardest idea in the project — how N workers pull from one
> table without ever grabbing the same row — *before* you build it.
> No prior knowledge assumed.
>
> Prepares you for **V1** in [`src/queue.rs`](../src/queue.rs) (`enqueue`, `claim`,
> `ack`, `get`) and the index TODO in [`migrations/0001_init.sql`](../migrations/0001_init.sql).
> Concept overview: [`00-how-job-queues-work.md`](./00-how-job-queues-work.md) §6–7.
> This doc goes *deeper* on the locking mechanism and the decisions you'll make —
> it does **not** write the claim for you.

---

## The one sentence to hold onto

**Selecting a job and claiming it must be a *single* atomic statement, because any
gap between "I found a ready job" and "I took it" is a window where a second worker
finds and takes the same one.**

---

## The problem before the solution

Recap the race (full trace in the overview doc §6): two workers both `SELECT` the
oldest `ready` job, both get row 42, both `UPDATE` it to `running`, both run it. The
job runs twice. You cannot patch this in application code because the workers may be
in different OS processes on different machines — they share no mutex, only Postgres.

So the fix has to be *inside the database*, and it has to make the read and the claim
inseparable. To get there you need to understand what a row lock actually is.

---

## What `FOR UPDATE` actually does

Postgres runs statements inside **transactions**. A normal `SELECT` reads a snapshot
and holds nothing. `SELECT ... FOR UPDATE` is different: it takes a **row-level lock**
on every row it returns, and holds that lock **until the transaction ends** (commit or
rollback).

```
BEGIN;
SELECT id FROM jobs WHERE state='ready' ORDER BY run_at LIMIT 1 FOR UPDATE;  -- locks row 42
--   ... row 42 is now locked by THIS transaction ...
UPDATE jobs SET state='running' WHERE id=42;
COMMIT;   -- lock released here
```

While your transaction holds that lock, what happens to a **second** worker that runs
the *same* `SELECT ... FOR UPDATE`? By default, it **blocks** — it parks, waiting for
your lock to release. That's the "block" behavior, and it's a disaster for a queue:

```
worker-0: locks 42, works ...
worker-1: SELECT ... FOR UPDATE  → BLOCKS, waiting on 42
worker-2: SELECT ... FOR UPDATE  → BLOCKS, waiting on 42
                                    (the whole pool serializes behind one row)
```

Worse, once worker-0 commits, worker-1 unblocks and re-evaluates — and if 42 is now
`running`, its `WHERE state='ready'` no longer matches, so it got nothing but a stall.
Locking alone makes the pool correct but **serial**. You've traded a correctness bug
for a throughput bug.

### The three lock-wait behaviors

Postgres gives you three choices for "what happens when the row I want is locked":

| Modifier | Behavior when a candidate row is locked | Fit for a queue |
|---|---|---|
| `FOR UPDATE` (default) | **Block** until the lock frees, then re-check | ❌ serializes the pool |
| `FOR UPDATE NOWAIT` | **Error** immediately | ❌ turns contention into failures |
| `FOR UPDATE SKIP LOCKED` | **Skip** the locked row, return the next unlocked one | ✅ contention-free hand-off |

`SKIP LOCKED` is the whole game. It says: *"don't wait, don't error — just pretend the
locked rows aren't there and give me the next available ones."* Now:

```
worker-0: claims 42 (locks + stamps + commits, fast)
worker-1: SELECT ... SKIP LOCKED → 42 is locked, SKIP it, take 43
worker-2: SELECT ... SKIP LOCKED → 42,43 locked, SKIP them, take 44
```

Each worker walks away with a *different* row, nobody blocks, full parallelism. That
is the "atomic claim." It's why "Postgres as a queue" went mainstream — Oban, Solid
Queue, Graphile Worker, good_job all lean on this one modifier.

---

## Worked example: 3 workers, a backlog of 5

Table state (all `ready`, oldest first by `run_at`):

```
id: 10  11  12  13  14      ← ready backlog
```

Three workers each claim a batch of 2 at nearly the same instant:

| Worker | Sees locked | Claims | Table after its commit |
|---|---|---|---|
| worker-A | — | **10, 11** | 10,11 → running |
| worker-B | 10, 11 | **12, 13** | 12,13 → running |
| worker-C | 10–13 | **14** (only one left) | 14 → running |

Total claimed: `{10,11,12,13,14}` — five distinct rows, each exactly once, zero
overlap, zero blocking. That's the guarantee the V1 concurrency test must assert.

---

## The decisions V1 asks *you* to make

This is where the doc stops teaching answers and starts naming the choices — the
interesting part is yours.

### 1. Batch size — how many rows per claim?

A worker that claims **one** row, runs it, and round-trips again spends most of its
life on network latency, not work.

```
claim 1:  [~1ms RTT][run 5ms][~1ms RTT][run 5ms]...   → ~28% of time is RTT overhead
claim 10: [~1ms RTT][run 5ms × 10]                    → RTT amortized across 10 jobs
```

But a bigger batch isn't free: a worker holding 10 claimed jobs has leased all 10, so
if it dies, 10 jobs wait for the reaper instead of 1. **The tradeoff:** throughput
(bigger batch) vs. blast radius on crash and fairness (smaller batch). The scaffold
exposes this as `CLAIM_BATCH` / `claim_batch` in [`WorkerConfig`](../src/worker.rs),
default 10. Your job: pick a number and be able to defend it in
[`04-design.md`](./04-design.md).

### 2. The index — keeping the claim cheap at scale

Your claim scans for `ready` rows ordered by `run_at`. On a young table that's fast.
On a table with 5 million rows where **99% are `done`**, a naive scan wades through all
the dead history every time.

The design space:

| Index | What it covers | Cost |
|---|---|---|
| No index | full/again scan of the whole table | melts as history grows |
| Full index on `(queue, run_at)` | every row, including millions of `done` | large, mostly useless entries |
| **Partial** index `... WHERE state='ready'` | *only* the live `ready` rows | tiny, exactly the hot slice |

A partial index only indexes rows matching its predicate, so it stays small and
selective no matter how much `done` history piles up — a strong fit here. The
migration leaves the index out **on purpose**: adding it and proving the difference
with `EXPLAIN (ANALYZE)` (with vs. without) is the V1 benchmark. Decide the exact
columns and predicate yourself.

### 3. Transaction lifetime — claim-and-commit, or hold the lock?

You could hold the transaction (and its `FOR UPDATE` lock) open for the *entire job*.
Don't — a slow job would pin a database connection and block Postgres's autovacuum for
as long as it runs. The pattern is: **stamp the row's state and commit immediately**,
releasing the lock in milliseconds. But that raises the question V2 answers: once
you've committed the row as `running` and let go of the lock, what stops another worker
from grabbing it, and what recovers it if this worker crashes? That's the **lease** —
see [`02-leases-visibility-timeout.md`](./02-leases-visibility-timeout.md).

---

## Depth probes (you own V1 when you can answer)

- Why must the lock live *inside* a transaction — what happens to the locks if the
  worker's connection dies mid-claim?
- With `SKIP LOCKED`, two workers never get the same row — but can a worker ever see
  *stale* data and skip a row it shouldn't? (Think about the snapshot vs. the lock.)
- At what scale does "Postgres as a queue" stop making sense, and what breaks first —
  the claim query, the write throughput, the vacuum, or the connection count?

---

## Where you'll build this

| Piece | Location |
|---|---|
| `enqueue` — the `INSERT` | [`Queue::enqueue`](../src/queue.rs) `todo!("V1: insert…")` |
| `claim` — the atomic dequeue (the heart) | [`Queue::claim`](../src/queue.rs) `todo!("V1: claim…")` |
| `ack` — mark done | [`Queue::ack`](../src/queue.rs) `todo!("V1: mark…")` |
| `get` — read one back | [`Queue::get`](../src/queue.rs) `todo!("V1: fetch…")` |
| the partial index | [`migrations/0001_init.sql`](../migrations/0001_init.sql) TODO(V1) |
| the concurrency test | `tests` module in [`src/queue.rs`](../src/queue.rs) |

**This doc unlocks (V1 "Done when ALL true"):** atomic select+lock in one statement;
two+ workers never claim the same row; batch claim respecting `run_at`/order/queue; the
partial-index payoff shown in the bench.

**Ready to build?** Use `/hint 04 V1` for graduated nudges, or `/quest 04 V1` for a
guided session that writes the failing acceptance tests up front (from the Done-when
criteria) and lets you implement against them. The SQL *shape* is sketched in the
`todo!()` comments — turning it into a correct, tested, indexed claim is the work.
</content>
