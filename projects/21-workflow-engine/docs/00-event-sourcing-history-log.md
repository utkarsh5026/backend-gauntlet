# Event Sourcing — Store History, Not State

> **What this teaches:** why a durable workflow engine stores an append-only log of
> *what happened* instead of a mutable *where we are*, and the invariants that log
> must hold. No prior knowledge assumed.
>
> **Prepares you for:** [SPEC](../SPEC.md) **V1 — Event-sourced history log**, built in
> [history.rs](../src/history.rs) on top of the `history_events` table in
> [0001_init.sql](../migrations/0001_init.sql). The event vocabulary lives in
> [model.rs](../src/model.rs) (`Event`, `EventType`).

---

## The one sentence to hold onto

**State tells you where you are; history tells you how you got here — and only "how
you got here" lets a stranger finish your work.**

---

## 1. The problem before the solution

Here is the promise this whole project makes. You write a workflow:

```text
charge the card
wait 3 days
if not cancelled: ship the order
email the customer
```

That function takes *days* to finish. The process running it will be deployed over,
OOM-killed, and evicted many times before the email goes out. Yet the engine
guarantees it runs **to completion, exactly as written**. So ask the only question
that matters: *when the process dies halfway through, what does the next process need
to know to continue mid-sentence?*

### Attempt 1: keep the state in RAM

A struct: `{ step: Shipping, charge_result: Ok(txn_9931), cancelled: false }`.

Dies with the process. Nothing to say beyond that.

### Attempt 2: a mutable `status` column

```text
workflow_executions
┌────────────┬────────────┐
│ run_id     │ status     │
│ 7f3a…      │ 'shipping' │
└────────────┴────────────┘
```

This survives the crash — and it is *still not enough*. A fresh worker reads
`'shipping'` and immediately hits questions the column cannot answer:

| Question the resuming worker has | Can `status='shipping'` answer it? |
|---|---|
| Did the charge succeed? What was the transaction id? | ❌ gone |
| Did the 3-day timer already fire, or are we mid-wait? | ❌ gone |
| Was `ship_order` already *started* by the worker that died? | ❌ gone — run it again and you may ship twice |
| What input did this workflow start with? | ❌ gone |
| Was the status even written correctly, or did the crash land between the charge and the `UPDATE`? | ❌ unknowable |

The killer is the third row. `status` tells you the workflow's *position*, but
resuming safely needs the workflow's *evidence*: which effects already happened and
what they returned. A position without evidence forces the resuming worker to guess —
and every guess is either a lost step or a repeated one.

### Attempt 3 (the trap): more columns

Add `charge_txn_id`, `timer_fired_at`, `shipped` … Now every workflow type needs its
own schema, every new step is a migration, and — worse — each column is written by a
separate `UPDATE`, so a crash between any two updates leaves a state that *never
actually existed*. You have N sources of truth and no way to tell which ones are lies.

---

## 2. The idea: record facts, derive state

Flip the storage model. Don't store the current state at all. Store every **fact**
as an **immutable event**, appended to a per-execution log, in order:

```text
history_events for run 7f3a… (one execution)

 event_id │ event_type          │ attributes
──────────┼─────────────────────┼──────────────────────────────
        1 │ workflow_started    │ { input: <order json> }
        2 │ workflow_task_…     │ (bookkeeping)
        3 │ activity_scheduled  │ { activity_type: "charge_card", input: … }
        4 │ activity_completed  │ { result: "txn_9931" }
        5 │ timer_started       │ { timer_id: "wait-3d", fire_at: … }
        ─── crash happens here — process gone ───
```

A brand-new worker on a different machine loads rows 1–5 and knows *everything*:
the charge ran exactly once and returned `txn_9931`; a timer is pending; the next
thing to do is wait for `TIMER_FIRED`. No guessing, because the log doesn't record
where the workflow *is* — it records everything that ever *happened to it*. State is
**derived**, whenever needed, by folding the log left to right (that fold is V2's
job — [01-deterministic-replay.md](01-deterministic-replay.md)).

This pattern is **event sourcing**. You already use a famous event-sourced system
daily: **git**. Commits are immutable events; `checkout` is the fold that derives a
working tree from them; nobody edits a published commit — a fix is a *new* commit.
Bank ledgers work the same way: your balance is not a cell someone overwrites, it is
the sum of every posted transaction. Temporal — the system this project rebuilds in
miniature — stores every workflow as exactly this kind of event history.

### Command vs event — the distinction that keeps the log honest

Two words that sound similar and must never blur:

| | Command | Event |
|---|---|---|
| What it is | a **request**: "schedule this activity" | a **recorded fact**: "the activity was scheduled" |
| Tense | imperative, future | past, immutable |
| Can it be rejected? | yes — validation, non-determinism | no — it already happened |
| Where it lives here | [`Command`](../src/model.rs) from the worker's `RespondWorkflowTaskCompleted` | [`Event`](../src/model.rs) rows in `history_events` |

The engine's rhythm is always: receive commands → validate → **record events** →
only then let the consequences become visible (the activity task, the timer). An
event is a promise to every future reader; you don't post it until it is true, and
once posted you never take it back.

---

## 3. The invariants the log must hold (and why each one)

The SPEC's Done-when criteria for V1 are exactly these invariants. Each one exists
because a specific corruption happens without it.

### 3.1 Append-only — corrections are new events

If any code path can `UPDATE` or `DELETE` a posted event, then two replays of the
"same" history can disagree — the exact drift this whole design exists to prevent.
An activity failed and then succeeded on retry? That is *two* events
(`activity_failed`, then a new schedule and `activity_completed`), not an edit. The
schema comment in [0001_init.sql](../migrations/0001_init.sql) says it flatly:
*"a correction is a new event, never an edit."*

### 3.2 Monotonic and gapless ids — ordering IS meaning

`event_id` is 1, 2, 3, … per run. In a log, position is semantics: "the charge
completed *after* it was scheduled" is encoded purely by 4 > 3. A **gap** means an
event was lost — folding around it silently produces a state that never existed. A
**duplicate** means two writers both thought they were appending event N — i.e. two
workers both believe they own this execution's next step. That collision must
*fail loudly* (the `(run_id, event_id)` primary key makes Postgres the enforcer),
because rejecting the second writer is precisely how the engine discovers a stale
worker trying to advance a workflow it no longer owns (V4 builds on this).

### 3.3 Atomic batch appends — no half-written facts

One workflow task often produces several events at once (task-completed +
activity-scheduled + timer-started). If a crash can land two of the three, the log
now *asserts* a moment that never happened — an activity scheduled with no record of
the decision that scheduled it. The whole ordered batch commits or none of it does.
This is what database transactions are for; the interesting part (your part) is
making sure every multi-event write path actually goes through one.

### 3.4 Suffix loads — `events after id k`

`load_history_after(run, k)` looks like a convenience; it is actually V5's entire
fuel supply. A sticky worker that already folded events 1–8,000 only needs
8,001-onward to catch up. The log's ordering guarantee is what makes "just the tail"
a meaningful thing to hand out.

### 3.5 Any status column is a projection, never truth

The `workflow_executions.status` column exists so a dashboard can `WHERE status =
'running'` without folding every history. That is fine **as long as it is only ever
written as a consequence of appending the corresponding event, in the same
transaction**. The moment any code writes it independently "for convenience," you
have two sources of truth again, and they *will* drift. This is the trap named in
[CONCEPTS.md](../CONCEPTS.md) Card 1 — it is the most common way real systems
quietly un-event-source themselves.

---

## 4. Worked example: one order, crash included

Trace the order workflow through the log, with a crash and a resume:

```text
 t   what happens in the world                    what lands in history_events
────────────────────────────────────────────────────────────────────────────────
 t0  StartWorkflow(order-51)                      1 workflow_started {input}
 t1  worker A polls, decides "charge first"       2 workflow_task_completed
                                                  3 activity_scheduled {charge_card}
 t2  activity worker runs the charge              4 activity_completed {txn_9931}
 t3  worker A polls again, decides "now wait"     5 workflow_task_completed
                                                  6 timer_started {wait-3d}
 t4  💀 worker A is OOM-killed                    (nothing — the log doesn't care)
 …   3 days pass; the engine restarts twice       (nothing — the timer is durable, V3)
 t5  timer fires                                  7 timer_fired {wait-3d}
 t6  worker B (never seen this run) polls,        8 workflow_task_completed
     loads events 1–7, folds them, and            9 activity_scheduled {ship_order}
     continues mid-sentence
 …
 tN  workflow returns                             13 workflow_completed {result}
```

Two things to sit with:

- **The crash at t4 produced no event.** A death leaves no mark because the log only
  records facts, and "worker A died" changes no fact about the order. Recovery is not
  a special code path that repairs state — it's the *normal* path (load, fold,
  continue) run by someone else.
- **Worker B never asked "what step was it on?"** It derived the answer, including
  the charge's transaction id, from evidence. That's the difference between
  `status='waiting'` and events 1–7.

---

## 5. The design space you'll navigate (not the answers)

V1 leaves you real decisions. Here is what each one trades — deciding is your part:

- **Who assigns event ids?** The caller passes fully-formed [`Event`](../src/model.rs)s
  with ids (from the replayed state's `next_event_id`) — so what, exactly, catches two
  concurrent writers? Think about what the primary key gives you for free and what it
  doesn't (gaps).
- **What makes the batch atomic?** "One transaction" is easy to say; the design work
  is deciding what else *joins* that transaction (the status projection? the first
  task enqueue in `start_execution`?) so no observer can see one without the other.
- **How does a rejected append surface?** A unique-key violation is Postgres talking;
  [`AppError`](../src/error.rs) is your API talking. Someone upstream (V4) needs to
  tell "this run was advanced by someone else" apart from "the database hiccuped."
- **Histories grow forever.** A workflow alive for a year has an enormous log. Real
  engines compact ("continue-as-new" in Temporal — start a fresh run whose
  `workflow_started` input is the old run's summary; Raft calls the same move a
  snapshot). Out of scope to build, in scope to understand: what makes compaction
  safe is that the fold is deterministic, so a prefix can be replaced by its result.

**Hard stop.** The append/load queries and the transaction shape are the V1 exercise
— the `TODO(V1)` blocks in [history.rs](../src/history.rs) mark exactly where. If
you're stuck between designs, `/hint` gives graduated nudges and `/quest` runs the
guided build; this doc's job ends at making the decisions visible.

---

## 6. Mental model summary

| Idea | One-liner |
|---|---|
| Event sourcing | Store what happened; derive where you are |
| Event | An immutable past-tense fact; never edited, only appended |
| Command | A request that *may become* events after validation |
| Gapless monotonic ids | Ordering is meaning; a duplicate id = two writers fighting — fail loudly |
| Atomic batch append | A crash can never leave a half-written moment in the log |
| Projection | A derived convenience (`status` column) that must only move when the log does |
| Suffix load | The delta feed that makes sticky execution (V5) possible |

## Where you'll build this

**Module:** [src/history.rs](../src/history.rs) — four `todo!()`s:
`start_execution`, `append_events`, `load_history`, `load_history_after`, over the
`history_events` + `workflow_executions` tables in
[0001_init.sql](../migrations/0001_init.sql).

**This doc unlocks V1's Done-when criteria:** append-only discipline · gapless
monotonic ids with loud duplicate rejection · atomic batch appends · full-history and
suffix loads in order · status as an agreeing projection. Proof: integration tests
against real Postgres (gated on `DATABASE_URL`) plus the why-event-sourcing section
of `docs/21-design.md` — see the V1 block in [SPEC.md](../SPEC.md).

**Next:** the log is only half the trick. Deriving state from it — identically, on
any machine, every time — is [01-deterministic-replay.md](01-deterministic-replay.md).
