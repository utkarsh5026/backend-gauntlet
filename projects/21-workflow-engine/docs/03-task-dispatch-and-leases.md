# Task Dispatch — Long-Poll, Leases, and Transactional Completion

> **What this teaches:** how work reaches workers with none lost and none
> duplicated-in-effect — while workers crash holding tasks, finish tasks after losing
> them, and poll from many processes at once. This is where V1–V3 become one engine.
> No prior knowledge assumed (docs [00](00-event-sourcing-history-log.md)–[02](02-durable-timers.md) first).
>
> **Prepares you for:** [SPEC](../SPEC.md) **V4 — gRPC worker dispatch**, built in
> [dispatch.rs](../src/dispatch.rs) over the `task_queue` table in
> [0001_init.sql](../migrations/0001_init.sql), speaking the RPCs in
> [workflow.proto](../proto/workflow.proto). The token type is
> [`TaskToken`](../src/model.rs).

---

## The one sentence to hold onto

**At-least-once delivery + deterministic replay + idempotent effects = a crashed
worker is a non-event — remove any one leg and the promise collapses.**

---

## 1. The problem before the solution

The engine has work (a workflow task: "fold this history and decide what's next"; an
activity task: "run `charge_card` with this input"). Workers — separate processes,
maybe hundreds, on machines the engine doesn't manage — must receive it. Every naive
delivery scheme fails a specific way:

| Scheme | How it breaks |
|---|---|
| **Push** — engine calls the worker | engine must track who's alive, reachable, and idle; a push to a dying worker is a lost task; you've built a health-checking scheduler before writing any workflow logic |
| **Busy-poll** — worker asks every 10 ms | 100 idle workers = 10,000 useless queries/sec hammering Postgres to learn "nothing yet" |
| **Slow poll** — worker asks every 5 s | idle-friendly, but every task waits up to 5 s to even start — p99 dispatch latency is dead on arrival (the boss demands ≤ 50 ms) |
| **Fire-and-forget delivery** — hand out the task, delete the row | worker crashes mid-task → the task is simply gone; the workflow is stuck forever |
| **Exactly-once delivery** — hand it out only once, ever | physically impossible to distinguish "worker crashed" from "worker is slow"; you *will* either lose tasks or duplicate them — the only question is which failure you engineer for |

The last row is the deep one. Between "the worker got the task and died" and "the
worker got the task and is still chewing," the engine sees the same thing: silence.
Any system that promises exactly-once *delivery* is lying somewhere. The honest
design picks **at-least-once delivery** and makes *effects* exactly-once by other
means — which this project has been quietly assembling all along.

---

## 2. Long-poll: the read side

`PollWorkflowTask` blocks. If a task is claimable, it returns immediately; if not,
it holds the connection open up to `long_poll_timeout` and then returns **empty** —
not an error — and the worker polls again ([workflow.proto](../proto/workflow.proto)
encodes "empty token = timed out, poll again").

Why this beats the alternatives, in numbers (100 idle workers):

| | busy-poll @10 ms | long-poll @30 s |
|---|---|---|
| idle queries/sec | 10,000 | ~3.3 |
| latency when work appears | ≤ 10 ms | ~0 — a poll is already parked and waiting |

Long-poll gets *both* ends of the tradeoff: near-zero idle cost **and** near-zero
dispatch latency, because the request is already there when work arrives. The price
is engine-side machinery — parked calls that must wake when work lands (and drain
gracefully on shutdown; the horizontal checklist grades that). Note the contract
detail the SPEC calls out: a timed-out poll is a **normal outcome**, so it's an empty
response, not a gRPC error status — a worker author should never see red in their
logs because the queue was quiet.

---

## 3. The lease: claiming without owning

A worker that polls successfully does **not** own the task. It holds a
**visibility-timeout lease** — the schema in [0001_init.sql](../migrations/0001_init.sql)
implements it with two columns:

```text
task_queue row:  state='pending' | visible_at | locked_by

claimable  ⇔  visible_at <= now()
claim      ⇒  visible_at = now() + visibility_timeout, locked_by = worker identity
complete   ⇒  the row is DELETED (inside the completion transaction, §5)
crash      ⇒  …nothing. visible_at just passes. The task is claimable again.
```

The elegance is the last line: crash recovery requires **no detection logic at
all**. Nobody health-checks the worker; the lease simply lapses and the task
resurfaces. Concurrent pollers are partitioned by the same claim pattern as V3's
scanners (`FOR UPDATE SKIP LOCKED` — the SPEC's Done-when names it): two workers
polling the same queue can never receive the same task.

You built this lease in project 04 for jobs. What's new here is what redelivery
*means*: the new worker doesn't restart a half-done job — it **replays history**
(V2) and continues from exactly where the facts end. Notice also what a task row
*is*: a **pointer** into history (`run_id`, `scheduled_event_id`), never a copy of
the work — the history stays the single source of truth, and the worker fetches it
at poll time.

### The visibility-timeout dial

| visibility_timeout | crashed worker's task is retried after | risk |
|---|---|---|
| 5 s | ≤ 5 s | a merely-slow worker (GC pause, big replay) gets its task re-handed to someone else while still running it — two workers on one task |
| 5 min | ≤ 5 min | crash recovery latency: the workflow stalls minutes per death |

The double-run risk is why the *next* two sections exist — the token makes the slow
worker's late completion harmless, and idempotent effects make the overlap harmless.
Your chosen value (and this reasoning) goes in `docs/21-design.md`; the SPEC grades
it. Under The Reaper (a worker killed every ~2 s), this dial *is* your recovery
latency.

---

## 4. The zombie-worker race — and the token that closes it

The nastiest sequence in this project. Walk it slowly:

```text
 t0   worker A claims task T for run 7f3a…       lease until t0+30s
 t1   A stalls (GC pause / network partition)     — not dead, just silent
 t30  lease lapses; task T claimable again
 t31  worker B claims T, replays, completes it    history advances: events 7,8,9
 t45  A wakes up, finishes its (now stale) work,
      and calls RespondWorkflowTaskCompleted      ← the zombie
```

A is not malicious and not buggy — its commands *looked* valid 45 seconds ago. If
the engine accepts them, they land **on top of B's events**: a duplicate advance of
the same task, commands computed against a history that has since moved, potentially
double-scheduled activities. Plausible history, written by a process that no longer
owned the work. (V1's gapless-id collision would catch some of this; the token check
catches *all* of it, before anything is attempted.)

The fix is the [`TaskToken`](../src/model.rs). Every poll response carries one —
`(run_id, kind, scheduled_event_id)` encoded as opaque bytes — and every completion
must present it. On completion the engine checks the token **against the live
claim**: is this task row still claimed, and by this claimant? B's completion passed
that check and *deleted the row*; A's token now names a claim that no longer exists
→ rejected.

The CONCEPTS card states the principle: **validity isn't ownership.** A payload full
of well-formed commands proves nothing; the token-vs-live-claim check is the entire
difference between "a worker finished its task" and "some process wrote plausible
history." (This is also a security boundary — the horizontal checklist's
"unforgeable-enough" item — because a token is only as good as the check that it
matches a *live* claim.)

---

## 5. Transactional completion: the orchestrator's core

`complete_workflow_task` in [dispatch.rs](../src/dispatch.rs) is where every
vertical meets. When a worker reports its commands, the engine must:

```text
① check the token against the live claim                    (§4)
② check_determinism(history, …, commands)                   (V2)
③ per command: append event(s) + create the side effect
     ScheduleActivity → ACTIVITY_SCHEDULED  + activity task row
     StartTimer       → TIMER_STARTED       + timer row      (V3, same txn!)
     CompleteWorkflow → WORKFLOW_COMPLETED  + status projection
④ append WORKFLOW_TASK_COMPLETED, delete the claim row
⑤ refresh the sticky pin                                     (V5)
```

The SPEC's Done-when: ①–④ are **one transaction**. Enumerate the partial commits to
see why — every one strands a distinct corpse:

| Committed | The stranded state |
|---|---|
| events but not side effects | history says `ACTIVITY_SCHEDULED`, but no activity task exists — no worker will ever poll it; the workflow waits forever on an activity that isn't coming |
| side effects but not events | an activity task points at a `scheduled_event_id` history doesn't contain; replay (V2) rejects the run the moment it completes |
| both, but claim row not deleted | the lease lapses later and the *same workflow task* is redelivered — a second worker re-decides a decision already recorded → non-determinism rejection at best, duplicate activities at worst |
| determinism check outside the txn | a divergent worker already half-committed before being caught — the check must gate the commit, not audit it |

This is the outbox lesson from project 18 turned inward: the event and its
consequence must share a commit or one of them is a lie. (⑤, the sticky pin, is
deliberately *outside* the "must be atomic" list — it's process-local memory, and
doc [04](04-sticky-execution.md) explains why losing it is always safe.)

### Workflow tasks vs activity tasks

Same queue table, same lease, different *contract* — worth keeping sharp:

| | workflow task | activity task |
|---|---|---|
| the worker runs | the workflow function, by replaying (must be pure, V2) | arbitrary effectful code — HTTP calls, charges, emails |
| returns | **commands** (decisions) | a **result** (or failure) |
| redelivery is safe because | replay is deterministic — same decision recomputed | **only if the activity is idempotent** — the engine can't make `charge_card` safe to re-run; the activity author must (idempotency keys) |
| completion path | `complete_workflow_task` (①–⑤ above) | `complete_activity_task`: append `ACTIVITY_COMPLETED` + delete task + enqueue a workflow task, one txn |

That third row is the recipe's third leg, and it's the one the *boss fight* audits
directly: every activity increments a durable, idempotency-keyed counter precisely
so double-execution is countable rather than trusted from logs.

### The recipe, assembled

Now say the whole sentence with the parts pointing at modules:

- **at-least-once delivery** (this doc — leases lapse, tasks resurface)
- **+ deterministic replay** ([replay.rs](../src/replay.rs) — the new worker
  recomputes the exact state)
- **+ idempotent effects** (activity idempotency keys; V3's exactly-once fire;
  V1's duplicate-id rejection)
- **= a crashed worker is a non-event.**

Remove a leg: no lease → crashed workers strand tasks. No determinism → the new
worker resumes into a state that never existed. No idempotency → every redelivery
risks a double charge. All three or nothing.

---

## 6. The design space you'll navigate (not the answers)

- **How does a long-poll actually wait?** Blocking a gRPC call for 30 s while a
  claim might appear at any moment — polling internally? notification
  (`LISTEN/NOTIFY`? a `tokio::sync` primitive per queue?)? The latency/complexity
  tradeoff is yours, and the boss's p99 ≤ 50 ms will judge it.
- **What exactly is "the live claim check"?** Which columns prove the presenter
  still holds the task, and how does it compose with the delete inside one
  transaction without racing another claimer?
- **The claim query's index.** The migration leaves the
  `(task_queue, kind, visible_at)` index as a V4 lesson — shape it, then prove the
  before/after under load.
- **Ordering inside completion.** ①–④ must be one transaction, but their *internal*
  order still matters (when do you read history for the determinism check? what
  locks first?). Deadlock-free composition with V3's scanner touching the same rows
  is part of the exercise.
- **Retries for failed activities** (stretch): `fail_activity_task` wakes the
  workflow — whether retry policy lives in the engine or the workflow is a real
  architecture fork; Temporal chose engine-side per-activity policies.

**Hard stop.** The seven `todo!()`s in [dispatch.rs](../src/dispatch.rs) are the
build — the largest vertical because it's the glue. `/hint` for nudges, `/quest` to
run it against acceptance tests.

---

## 7. Mental model summary

| Idea | One-liner |
|---|---|
| Long-poll | Park the request until work exists; empty response on timeout is a normal outcome, not an error |
| Lease (visibility timeout) | A claim that expires by itself — crash recovery with zero detection logic |
| At-least-once | The honest guarantee; "exactly-once delivery" is indistinguishable from lying |
| Task = pointer | The row names (run, event); the history remains the only truth |
| Zombie race | Late completion after a lapsed lease; validity isn't ownership — the token-vs-live-claim check closes it |
| Transactional completion | Token check + determinism + events + side effects + claim delete: one commit, or a stranded corpse |
| The recipe | at-least-once + deterministic replay + idempotent effects = crashes are non-events |

## Where you'll build this

**Module:** [src/dispatch.rs](../src/dispatch.rs) — `start_workflow`,
`poll_workflow_task`, `complete_workflow_task`, `poll_activity_task`,
`complete_activity_task`, `fail_activity_task`, `get_result`; plus the `task_queue`
index TODO in [0001_init.sql](../migrations/0001_init.sql).

**This doc unlocks V4's Done-when criteria:** true long-poll · exclusive claims ·
at-least-once redelivery after a lapsed lease · stale-lease completion rejected ·
fully transactional completion. Proof: the two-worker, crash-redelivery, and
stale-token tests, and the visibility-timeout rationale in `docs/21-design.md` — see
the V4 block in [SPEC.md](../SPEC.md).

**Next:** correctness is done after V4 — V5 makes it *fast* without being allowed to
change a single answer: [04-sticky-execution.md](04-sticky-execution.md).
