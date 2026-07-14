# 04 — Distributed Job Queue: Benchmarks

> Raw numbers only — the *reasoning* lives in [`04-design.md`](./04-design.md).
> This doc is the **Proof** for the V1 index payoff, the V4 poll-vs-NOTIFY latency
> win, the V2 chaos run, and the Definition-of-done throughput/latency load test
> (see [`SPEC.md`](../SPEC.md)). A box flips to ✅ only when its numbers are here.
>
> **Record the setup for every run** so numbers are comparable:
> hardware, `WORKER_CONCURRENCY`, `CLAIM_BATCH`, `DB_MAX_CONNECTIONS`, Postgres
> version/settings, backlog size, commit SHA.

## Environment

| Field | Value |
|---|---|
| Machine / CPU / RAM | WSL2 (Linux 5.15) on x86_64 — dev box, not a tuned bench host |
| Postgres version | `postgres:16-alpine` → PostgreSQL 16.14 (compose) |
| `WORKER_CONCURRENCY` | `____` (n/a for the V1 EXPLAIN microbench below — that's the DoD load test) |
| `CLAIM_BATCH` | `10` (the `LIMIT` used in the V1 EXPLAIN) |
| `DB_MAX_CONNECTIONS` | `____` |
| `VISIBILITY_TIMEOUT_SECS` | `____` |
| Commit SHA | `0cd24cb` |

---

## 🐉 Boss fight — `<boss name / arena from SPEC>`

_(Restate the "boss falls when ALL true" numeric targets from the SPEC, then the
measured result next to each. The boss is defeated only when every row passes.)_

| Target (from SPEC) | Threshold | Measured | ✅ / ❌ |
|---|---|---|---|
| Sustained throughput | ≥ `____` jobs/s | `____` | |
| End-to-end p99 (enqueue → done) | ≤ `____` ms | `____` | |
| No double-run under load | 0 dupes | `____` | |
| … | | | |

---

## V1 — Claim index payoff (with vs. without the partial index)

**Index:** `jobs_claim_idx` — partial composite `(queue, run_at) WHERE state = 'ready'`
(`migrations/0002_claim_index.sql`).

**Method.** Single-statement `EXPLAIN (ANALYZE, BUFFERS)` of the real claim
`UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 10)`, run with vs.
without the index over an **identical seeded backlog**: **20,000 `ready`** rows on
queue `default` (all due) plus **200,000 `done`** rows (the terminal history the
partial index is designed to skip). `ANALYZE jobs` before each run so the planner
has fresh stats; each variant seeded + measured inside a `BEGIN … ROLLBACK` so the
dev DB is untouched. One representative run each (this is the *plan/payoff* proof;
sustained p50/p99 throughput under a worker pool is the Definition-of-done load test
below, not this microbench).

| Config | claim exec time | buffers (claim subtree) | rows scanned | plan |
|---|---:|---:|---:|---|
| **without** partial index | **38.1 ms** | 5,483 | seq-scans all **220,001**, filters out 200,001 | **Seq Scan** on `jobs` + **quicksort** (`Sort Key: run_at`) → LockRows → Limit |
| **with** partial index | **0.32 ms** | 53 (index scan itself: **3**) | reads **10** | **Index Scan** using `jobs_claim_idx`, `Index Cond: (queue = 'default' AND run_at <= now())` — **no Sort node** |

**Payoff: ~120× faster (38.1 ms → 0.32 ms), ~100× fewer buffers (5,483 → 53)** on
this backlog — and the gap *widens* as `done` accumulates, because the seq-scan path
re-scans the entire table history on every claim while the partial index only ever
holds the live `ready` backlog.

`EXPLAIN` output (both):

```
-- without (index dropped): Seq Scan → Sort → LockRows → Limit
->  Limit (actual time=37.655..37.663 rows=10 loops=1)
      ->  LockRows (actual time=37.654..37.660 rows=10 loops=1)
            ->  Sort (actual time=37.636..37.637 rows=10 loops=1)
                  Sort Key: jobs_1.run_at
                  Sort Method: quicksort  Memory: 1706kB
                  ->  Seq Scan on jobs jobs_1 (actual time=4.405..33.027 rows=20000 loops=1)
                        Filter: ((queue = 'default') AND (state = 'ready') AND (run_at <= now()))
                        Rows Removed by Filter: 200001
                        Buffers: shared hit=5433
 Execution Time: 38.128 ms

-- with jobs_claim_idx: Index Scan, no Sort
->  Limit (actual time=0.028..0.035 rows=10 loops=1)
      ->  LockRows (actual time=0.028..0.034 rows=10 loops=1)
            ->  Index Scan using jobs_claim_idx on jobs jobs_1 (actual time=0.025..0.028 rows=10 loops=1)
                  Index Cond: ((queue = 'default') AND (run_at <= now()))
                  Filter: (state = 'ready')
                  Buffers: shared hit=3
 Execution Time: 0.316 ms
```

**Takeaway:** the partial predicate `WHERE state = 'ready'` keeps the index
proportional to the *backlog* (not the *history*), and the `(queue, run_at)` column
order lets one index scan serve the queue equality, the `run_at <= now()` range,
**and** the `ORDER BY run_at` — so `LIMIT 10` stops after 10 rows with no sort. The
seq-scan alternative degrades linearly with total table size; the index does not.

---

## V1/V2 — Concurrency correctness (no double-dispatch)

**Method.** `____` workers over a backlog of `____` distinct jobs; assert each job
runs exactly once (count distinct completions == backlog; zero duplicates).

| Workers | Backlog | Distinct completed | Duplicates | Result |
|--------:|--------:|--------------------|-----------:|--------|
| | | | 0? | |

---

## V2 — Chaos run (kill a worker mid-batch)

**Method.** Start a drain, `SIGKILL` a worker mid-job, keep the rest running.
Assert every job still reaches `done` (via another worker after the lease expires),
and none is stuck `running`.

| Metric | Value |
|---|---|
| Jobs enqueued | `____` |
| Worker killed at | `____` |
| Jobs reaped (requeued) | `____` |
| Jobs eventually `done` | `____` (== enqueued?) |
| Jobs stuck `running` at end | `____` (== 0?) |
| Recovery time (kill → last job done) | `____` |

**Takeaway:** `__________`

---

## V3 — Backoff curve + poison message → DLQ (not a hot loop)

**Method.** Enqueue an always-failing job; record the actual retry timestamps and
confirm it lands in the DLQ after `max_attempts` instead of looping.

| attempt | retried at (Δ from prev) | state after |
|--------:|--------------------------|-------------|
| 1 | | ready |
| 2 | | ready |
| … | | |
| max | | **dead** (DLQ) |

- Retries observed: `____` (== `max_attempts`?)  ·  Ended in a hot loop? **no**
- CPU during the failing period (sanity that it's not spinning): `____`

---

## V4 — Poll vs. LISTEN/NOTIFY pickup latency (idle queue)

**Method.** Idle queue; enqueue one job; measure enqueue → pickup latency, and DB
query load while idle.

| Mode | pickup p50 | pickup p99 | empty SELECTs/sec while idle |
|---|---|---|---|
| Poll only (`POLL_INTERVAL_MS`=`____`) | | | |
| LISTEN/NOTIFY + poll fallback | | | |

Also: a **dropped-notification** test — kill/skip the NOTIFY, confirm the poll
fallback still picks the job up. Result: `____`

**Takeaway:** `__________`

---

## Definition-of-done load test

**Method.** `____` (Rust or `k6` client): enqueue a large backlog, run the worker
pool to drain it; report sustained throughput and end-to-end latency under load.

| Metric | Value |
|---|---|
| Backlog size | `____` |
| Sustained throughput | `____` jobs/s |
| End-to-end latency p50 | `____` ms |
| End-to-end latency p99 | `____` ms |
| Queue depth / oldest-ready-age at steady state | `____` |

**Notes / surprises:** `__________`
</content>
