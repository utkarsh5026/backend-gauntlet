# Sticky Execution — A Cache That Must Never Change the Answer

> **What this teaches:** why full replay on every task gets expensive, how routing an
> execution back to the worker that already holds its folded state fixes that, and
> the discipline that keeps a cache from quietly becoming a source of truth. No prior
> knowledge assumed (docs [00](00-event-sourcing-history-log.md)–[03](03-task-dispatch-and-leases.md) first).
>
> **Prepares you for:** [SPEC](../SPEC.md) **V5 — Sticky workflow-state cache**,
> built in [sticky.rs](../src/sticky.rs) (`StickyCache`: `lookup`, `pin`, `evict`)
> and woven into [dispatch.rs](../src/dispatch.rs)'s poll/complete paths via
> [`load_history_after`](../src/history.rs).

---

## The one sentence to hold onto

**A sticky hit and a full replay must produce identical state and identical commands
— the cache is allowed to change how fast the answer arrives, never what it is.**

---

## 1. The problem before the solution

Replay (V2) is correct, and correctness has a bill. Every workflow task means: load
the run's **entire** history, fold **every** event, make one decision. The cost
compounds two ways.

**Per execution, it's quadratic.** A workflow that makes 100 decisions, its history
growing ~5 events per decision, folds 5 events for decision 1, 10 for decision 2, …
500 for decision 100. Total: **25,250 event-folds to make 100 decisions** — versus
500 if each task only folded what was new. (Long-running workflows are the norm
here: the whole point of the engine is functions that live for days.)

**Per engine, it multiplies by throughput.** At the boss fight's 500 tasks/sec
against histories averaging 1,000 events, full replay means **500,000 event-folds
per second** — plus loading all those rows from Postgres per task. The engine spends
its life re-deriving state it derived seconds ago, and the history table becomes the
hottest read path in the system.

The obvious fix: *cache the folded `WorkflowState`.* And here is the landmine — the
fold result lives in the memory of **one specific worker process**, because that's
who did the folding. A cache that lives in one mortal process raises exactly the
questions this vertical exists to answer:

| Naive cache move | How it dies |
|---|---|
| worker keeps state; engine keeps routing the run to it, forever | worker crashes (The Reaper kills one every ~2 s) → every task for that run routes to a corpse; the execution is stranded |
| put the folded state in Postgres/Redis instead | now state is *stored* again — the mutable-state column from doc 00 sneaks back in through the cache door, and it can drift from the log |
| worker trusts its cached state unconditionally | the cache was folded by *old code* before a deploy → stale logic makes the next decision (V2's determinism check is the backstop, but only if the design lets it fire) |

---

## 2. The idea: route the work to the memory

Invert the usual caching direction. Don't move the state to the work — **move the
work to where the state already is.** After worker `w1` completes a task for run
`7f3a…`, the engine remembers: *`7f3a…`'s folded state, through event 8,000, lives
in `w1`* — a **pin**. When the run's next task appears, the engine routes it back to
`w1` and ships only the **events since the pin** (V1's suffix load — this is what
`load_history_after` was built for):

```text
                     sticky HIT                          sticky MISS (fallback)
             ─────────────────────────           ──────────────────────────────
engine sends  events 8,001–8,006 (6 rows)         events 1–8,006 (all of them)
              sticky_cache_hit = true             sticky_cache_hit = false
worker does   fold 6 events onto cached state     fold 8,006 events from initial()
result        same state, same commands           same state, same commands  ← the law
```

The engine-side piece is small and deliberately humble: a routing table.
[`StickyCache`](../src/sticky.rs) maps `RunId → StickyPin { worker_identity,
last_event_id, expires_at }`, guarded by a plain `Mutex<HashMap>` — **process-local,
in memory, not in Postgres.** That location is a design statement, not laziness:
losing the whole table on an engine restart costs full replays, never correctness.
The moment the table lives in the database it starts smelling like truth, and
someone will eventually treat it as such.

Temporal works exactly this way (workers advertise per-instance sticky queues; the
server routes-with-fallback); the wire flag already exists in
[workflow.proto](../proto/workflow.proto) as `sticky_cache_hit`, and
[`WorkflowTask`](../src/model.rs) carries it to your worker.

### What a hit is worth

Verified arithmetic at the boss-fight shape (500 tasks/sec, 1,000-event average
histories, ~5-event deltas):

| | events folded per task | engine-wide folds/sec |
|---|---|---|
| no sticky | 1,000 | 500,000 |
| 80% hit ratio | 0.8×5 + 0.2×1,000 = **204** | **102,000** (~4.9× less) |
| per-hit vs per-miss | 5 vs 1,000 | a hit folds **200×** fewer events |

And the DB read shrinks the same way: a hit loads 5 rows instead of 1,000. The boss
demands hit ratio ≥ 80% and ≥ 5× fewer events replayed on a hit — both directly
observable from the metrics the horizontal checklist requires (hit/miss and
events-replayed as structured fields per dispatched task).

---

## 3. The safety design: liveness-bounded, never load-bearing

The pin answers "where is the cached state?" The TTL answers the question that
actually matters: **"is that memory still reachable?"**

### Why the pin must expire

The Reaper kills `w1` while it holds the pin for `7f3a…`. Without expiry, the
engine keeps routing the run's tasks toward a worker that will never poll again —
one dead process silently strands every execution pinned to it. So the pin carries
`expires_at = last activity + TTL`: a worker that goes silent past the TTL *loses*
the pin, and the run falls back to the normal queue, where **any** worker picks it
up with a full replay. The stranding failure converts into a bounded delay plus one
expensive fold.

The TTL is a liveness bet, and `docs/21-design.md` must state your value and why:

| TTL | pinned-to-a-corpse window (worst) | cost of the bet |
|---|---|---|
| short (~seconds) | small | healthy-but-briefly-idle workers lose pins → misses → replays |
| long (~minutes) | a dead worker strands runs for minutes | high hit ratio while alive |

Tie it to what "alive" observably means in your engine (a worker that long-polls
every ≤30 s is provably alive at that cadence — the TTL should be reasoned from the
signal you actually have).

### The law: pure optimization, provable

The SPEC's central Done-when: a sticky hit and a full replay produce **the same
state and the same commands**. This isn't a slogan — it's a *testable property*,
and V2 already did the heavy lifting: replay is split-invariant
(doc [01](01-deterministic-replay.md)), so `fold(cached_state, delta)` ≡
`fold(initial, full_history)` *by construction*… provided the cached state really is
the fold of exactly events `1..=last_event_id`, and the delta is exactly
`last_event_id+1..`. The pin's `last_event_id` is the seam — off by one event and
the two paths diverge.

The proof the SPEC asks for is brutal and honest: **kill the sticky worker
mid-execution and compare outcomes.** Fallback must produce an identical result.
The Reaper kills a worker every ~2 s precisely to prove the fallback path is real
and not a decorative branch nobody ever exercised.

### What else invalidates the cache

Worker death is only the loud case. A code deploy is the quiet one: `w1`'s cached
state was folded by *old* workflow code. The pin can't see that — but V2's
determinism check can: divergent commands get `FAILED_PRECONDITION`, the execution
falls back to a full replay under new code. The layers compose — the cache makes
things fast, the TTL makes it live, the determinism check makes it *safe to be
wrong*.

This is the third time the gauntlet has taught the same cache law — project 01's
Redis in front of Postgres, project 20's query cache, now this. The shared test for
any cache you ever add: **derived, disposable, never authoritative.** If losing the
cache can change an answer (not just a latency), it isn't a cache — it's an
unreplicated database you forgot you were operating.

---

## 4. Worked example: one run, one reap, zero drama

```text
 t0   run 7f3a… task #1 → normal queue → w1 claims (MISS: full history, 12 events)
      w1 folds 12, decides, completes → engine pins {7f3a… → w1, last_event_id=15, ttl 30s}
 t1   activity completes → task #2 → pin live → routed to w1
      w1 gets events 16–18 only (HIT) · folds 3 onto cached state · completes
      → pin refreshed {last_event_id=19}
 t2   💀 The Reaper kills w1
 t3   timer fires → task #3 → pin for 7f3a… not yet expired… but w1 never polls
 t3+30s  pin expires → task #3 visible on the normal queue
 t4   w2 claims (MISS: full history, 22 events) · folds from initial() · same
      decision task #3 would have produced on w1 · completes → new pin {7f3a… → w2}
```

Compare t1 and t4: different workers, different fold sizes (3 vs 22), **identical
commands**. That comparison — not the hit ratio — is the vertical's real deliverable.
The hit ratio is just the reward.

---

## 5. The design space you'll navigate (not the answers)

- **Where does "sticky routing" actually happen?** The scaffold's shape (see
  `poll_workflow_task`'s TODO in [dispatch.rs](../src/dispatch.rs)) checks the pin
  at *poll* time — think through what that means for which worker's poll can claim a
  pinned run's task, and what happens in the window where the pin is live but `w1`
  hasn't polled yet.
- **Expiry mechanics.** Lazy eviction on `lookup` vs a background sweep — for a
  `Mutex<HashMap>`, which one is worth its complexity? What does `lookup` do when it
  finds a corpse?
- **When exactly to `pin`, refresh, and `evict`.** Completion pins; what about a
  terminal workflow? A non-determinism rejection? The lease lapsing in V4?
  Enumerate the lifecycle transitions and decide each one deliberately.
- **Measuring it.** The Done-when demands the hit ratio be *measurable* — the
  metrics scaffolding ([metrics.rs](../src/metrics.rs), `REPLAYS_TOTAL` with a
  sticky label) is there; deciding what counts as a hit/miss at the instrumentation
  point is yours.

**Hard stop.** The three `todo!()`s in [sticky.rs](../src/sticky.rs) are small; the
real build is threading hit/miss through `poll_workflow_task` and
`complete_workflow_task` without breaking V4's transactionality. `/hint` and
`/quest` from here.

---

## 6. Mental model summary

| Idea | One-liner |
|---|---|
| Sticky execution | Route the work to the worker that already holds the folded state; ship only the delta |
| The pin | `run → (worker, last_event_id, expires_at)` — a routing hint, never a fact about the workflow |
| Liveness-bounded | A pin is a bet that the worker is alive; the TTL bounds how wrong the bet can be |
| Fallback | Normal queue + full replay — always available because the log is complete (doc 00) |
| Pure optimization | Hit ≡ miss in state and commands, provable via V2's split-invariance; only latency may differ |
| Process-local cache | Losing it costs replays, never correctness — the instant that's false, it's not a cache |
| The cache law | Derived, disposable, never authoritative (Redis in 01, query cache in 20, sticky here) |

## Where you'll build this

**Module:** [src/sticky.rs](../src/sticky.rs) — `lookup`, `pin`, `evict` (pure
in-memory, no DB needed for its tests) — plus the sticky-aware branches of
[dispatch.rs](../src/dispatch.rs)'s `poll_workflow_task` / `complete_workflow_task`,
fed by [`load_history_after`](../src/history.rs).

**This doc unlocks V5's Done-when criteria:** next task routes to the same worker
with only the delta · silent worker loses the pin and falls back to full replay ·
hit ≡ miss in outcome · process-local, never truth · high measurable hit ratio with
dramatically fewer events replayed. Proof: the stickiness test, the
kill-the-sticky-worker test, the hit-ratio bench line, and the TTL rationale in
`docs/21-design.md` — see the V5 block in [SPEC.md](../SPEC.md).

**Next:** the cross-cutting contracts that make the engine operable — status codes,
tokens, payload bounds, shutdown, and the metrics story:
[05-fundamentals-woven-through.md](05-fundamentals-woven-through.md).
