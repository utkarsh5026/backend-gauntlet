# Durable Timers — A Sleep That Outlives the Process

> **What this teaches:** why "wait 3 days" can never be a `tokio::sleep`, how a delay
> becomes a *fact in the database* swept by a scanner, and why firing it exactly once
> is the hard part. No prior knowledge assumed (docs
> [00](00-event-sourcing-history-log.md) and [01](01-deterministic-replay.md) set the
> stage).
>
> **Prepares you for:** [SPEC](../SPEC.md) **V3 — Durable timers**, built in
> [timers.rs](../src/timers.rs) (`schedule_timer`, `claim_due`, `mark_fired`,
> `fire_due_timers`) on the `timers` table in
> [0001_init.sql](../migrations/0001_init.sql).

---

## The one sentence to hold onto

**A durable timer is not a running countdown — it is a persisted due-time that any
scanner, on any instance, at any point after it passes, can fire; durability comes
from the timer never having lived in a process at all.**

---

## 1. The problem before the solution

The order workflow says "wait 3 days, then ship." Three days is 259,200,000 ms. The
obvious code:

```rust
tokio::time::sleep(Duration::from_millis(259_200_000)).await;  // ← the bug
```

A `tokio::sleep` is an entry in the *runtime's in-memory timer wheel*. It is state
in RAM, and doc 00 already taught us what RAM state is worth here:

| Reality of a 3-day window | What happens to the in-memory sleep |
|---|---|
| The service deploys (probably ~daily) | new process, empty timer wheel — the wait is silently gone; the order never ships |
| The process OOMs / the pod is evicted | same — and nothing even knows a timer existed |
| The engine scales from 3 replicas to 2 | the timers living in the retired replica vanish |
| You run two engine instances | which one holds the sleep? both? now it fires twice |

And the "fix" that keeps the trap alive: *persist timers, but hydrate them into an
in-memory queue at boot and fire from there.* Between hydrate and fire, a second
instance hydrates the same rows — double-fire. Crash after hydrate and pending fires
are lost until the next reboot. The lesson the CONCEPTS card states as the trap:
**the database is the timer wheel; memory is at most a hint.**

So the delay must be stored as data, and *fired* by logic that assumes nothing about
which process is alive. Two separate problems, in order:

1. **Durability of the start** — the timer must survive the process that created it.
2. **Exactly-once firing** — scanners run on every instance, overlap, crash mid-fire,
   and retry; yet `TIMER_FIRED` must land in history once.

---

## 2. Part one: the start — a row, atomic with its event

When the workflow's `StartTimer` command is processed (V4's orchestrator), two
writes describe the same fact:

```text
history_events:  6 │ timer_started │ { timer_id: "wait-3d", … }     ← the record
timers:          (run 7f3a…, "wait-3d") │ fire_at = now()+3d │ pending  ← the mechanism
```

The SPEC's first Done-when says these commit **in the same transaction**. Enumerate
the halves to see why — each partial commit is a distinct lie:

| What committed | The orphan it creates |
|---|---|
| Event only, no row | history says a timer is pending; no scanner will ever fire it — the workflow waits forever, and replay (V2) faithfully reconstructs the wait every time |
| Row only, no event | a scanner will eventually fire a timer that, per history, was never started — `TIMER_FIRED` for a ghost; replay rejects it as malformed (doc 01) |

That is why [`schedule_timer`](../src/timers.rs) must run *inside the caller's
transaction* (the one V4 opens to append `TIMER_STARTED`), never a fresh one of its
own — the scaffold's TODO says exactly this. Once that transaction commits, the
timer is durable in the strongest sense: kill every process in the system and the
row is still there, still due at `fire_at`. Nothing needs to survive *because
nothing was ever held*.

You built the ancestor of this in project 04: a job with a `run_at` column. V3 is
that idea promoted — the due-time now participates in an event-sourced history, so
firing it must be exactly-once *into the log*.

---

## 3. Part two: the fire — at-least-once attempts, exactly-once history

A background loop ([`scan_loop`](../src/timers.rs)) wakes every `interval` and asks:
*which pending timers have `fire_at <= now()`?* Then, per due timer, three things
must happen:

```text
① append TIMER_FIRED to history          (the fact)
② enqueue a workflow task                 (the wake-up — the workflow reacts)
③ mark the timer row 'fired'              (so the next scan skips it)
```

The hostile conditions: the scanner runs on **every** engine instance concurrently;
any scan can **crash between steps**; a restart re-scans everything still pending.
Firing attempts are therefore *at-least-once* by nature — and the design question is
how at-least-once attempts produce exactly-once history.

### Crash-point analysis — why ①②③ are one transaction

Walk each possible partial outcome:

| Committed before crash | Resulting world |
|---|---|
| nothing | timer still `pending`, still due → next scan retries. ✅ safe |
| ① only | history says fired, but no wake-up task — the workflow knows the timer fired and *nobody ever tells it to act*. Stuck forever. ❌ |
| ①+② only | fired and woken, but row still `pending` → next scan fires it AGAIN → duplicate `TIMER_FIRED`. V2's replay rejects the malformed history — the engine now trips over its own log. ❌ |
| ①+②+③ | done. ✅ |

Only "all" and "nothing" are safe states — the textbook signature of *this must be
one transaction*. Crash mid-fire then means: the timer is simply still due, and the
retry is harmless. That's the SPEC's third Done-when, word for word.

### Concurrent scanners — claiming without fighting

Two engine instances scan at the same moment; both see `wait-3d` due. If both fire
it, ① happens twice. The dedupe has two layers:

- **The claim:** the SPEC names the pattern — `FOR UPDATE SKIP LOCKED`. Instance A's
  `SELECT … FOR UPDATE` locks the due rows it grabbed; instance B's `SKIP LOCKED`
  *skips* rows someone else holds instead of waiting for them. Each due timer is
  handled by exactly one scanner per pass, and scanners never serialize behind each
  other. (Same pattern you used for job claims in project 04 — and V4 will use it
  again for task claims.)
- **The backstop:** `(run_id, timer_id)` is the `timers` table's primary key, and
  the state flip to `'fired'` rides the same transaction as the append — so even a
  scanner with a stale view can't commit a second fire over a completed one.

### Scan cost — O(due), not O(all timers)

An engine hosting a million dormant timers (all due next month) must not pay for
them on every 500 ms scan. The query filters `state = 'pending' AND fire_at <=
now()` — whether that's cheap depends entirely on an index. The migration's
`TODO(V3)` comment leaves the index to you *on purpose*: pick the shape (the hint is
"partial"), and measure before/after. The Done-when is observable: scan cost bounded
by what's due.

### The scan interval — the honest contract

A swept timer does not fire *at* `fire_at`; it fires **within `interval + ε` after**
`fire_at`. That's the dial:

| interval | firing latency (worst) | scan load |
|---|---|---|
| 100 ms | ~100 ms late | 10 queries/sec/instance |
| 1 s | ~1 s late | 1 query/sec/instance |
| 30 s | ~30 s late | negligible |

For workflows sleeping days, seconds of latency are free — which is why the DB-swept
design wins here, while project 14's in-memory pacing wheel (microsecond precision,
zero durability) wins there. `docs/21-design.md` must state the interval you chose
and this tradeoff — the SPEC grades it.

---

## 4. Worked example: the 3-day wait, with a full restart in the middle

```text
 t0        V4 processes StartTimer("wait-3d", 259200000 ms)
           ── one txn: event 6 timer_started + row (7f3a…, wait-3d) pending, fire_at=t0+3d

 t0+1d     rolling deploy — every engine process replaced        (rows unaffected)
 t0+2d     worker pool scaled to zero overnight                  (rows unaffected)

 t0+3d     scan tick on instance B:  claim → due: (7f3a…, wait-3d)
           ── one txn: event 7 timer_fired + enqueue workflow task + row → fired
 t0+3d+ε   scan tick on instance C:  claim → nothing (row is 'fired', B's lock gone)

 t0+3d+2s  a worker polls, folds events 1–7, sees the timer fired → decides "ship"
```

The deploy and the scale-down are invisible because there was never anything to
lose. Instance C's scan is the at-least-once world doing its thing — and finding,
correctly, nothing to do.

---

## 5. The design space you'll navigate (not the answers)

- **Transaction plumbing.** `schedule_timer` must join the *caller's* transaction —
  how a `&mut Transaction` (vs the pool) flows through your APIs is a real Rust
  design decision, and it shapes V4's completion path too.
- **The fire bundle.** `fire_due_timers` composes V1 (append), V4 (enqueue), and V3
  (mark) into one atomic unit per timer — decide the unit: per-timer transactions or
  one for the whole batch, and what each choice does when one timer's fire fails.
- **The index.** The migration hands you the scan query's shape; the index that
  makes it O(due) — and the proof it worked — is yours.
- **Cancellation** (stretch, from the CONCEPTS depth probe): the workflow takes the
  other branch and no longer wants the timer. What if a fire is in flight at that
  moment? Decide who wins and which mechanism (the same claim? the state column?)
  decides it.

**Hard stop.** The three `todo!()`s in [timers.rs](../src/timers.rs) plus
`fire_due_timers` are the build; the queries and index are deliberately left out of
this doc. `/hint` and `/quest` from here.

---

## 6. Mental model summary

| Idea | One-liner |
|---|---|
| Durable timer | A persisted due-time, not a running countdown — durable because it never lived in a process |
| Atomic start | Timer row + `TIMER_STARTED` in one transaction; each half alone is a distinct lie |
| Scanner | Every instance sweeps `fire_at <= now()`; attempts are at-least-once by nature |
| Atomic fire | Event + wake-up + mark, all or nothing — only "all" and "nothing" are safe states |
| `SKIP LOCKED` claim | Concurrent scanners partition the due set instead of fighting or double-firing |
| O(due) scan | A million dormant timers cost nothing per sweep — an index question, measured |
| Interval dial | A timer fires within `interval + ε` of due; latency ↔ scan load, stated in the design doc |

## Where you'll build this

**Module:** [src/timers.rs](../src/timers.rs) — `schedule_timer`, `claim_due`,
`mark_fired`, `fire_due_timers`; table + index TODO in
[0001_init.sql](../migrations/0001_init.sql). (`scan_loop` is already wired; it runs
when `RUN_TIMER_SERVICE=true`.)

**This doc unlocks V3's Done-when criteria:** atomic durable start · idempotent
exactly-once fire · fire atomic with the wake-up · no double-fire across concurrent
scanners · O(due) scan cost. Proof: the fire/restart/concurrency tests and the
scan-interval + index notes in `docs/21-design.md` — see the V3 block in
[SPEC.md](../SPEC.md).

**Next:** timers wake workflows by enqueuing tasks — how tasks reach workers without
being lost or doubled is [03-task-dispatch-and-leases.md](03-task-dispatch-and-leases.md).
