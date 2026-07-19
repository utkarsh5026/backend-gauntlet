<!-- status:
state: not-started      # active | paused | blocked | done | not-started
blocked-on: ~           # free text, or ~ for none
-->

# Project 21 — Workflow Engine *(Temporal-lite)*

> A workflow engine sells one promise: **durable execution**. You write a normal-looking
> function — "charge the card, wait 3 days, if not cancelled ship the order, email the
> customer" — and the engine guarantees it runs *to completion, exactly as written, even
> though the process running it will crash, deploy, and restart many times before it
> finishes.* That is a wild promise, and the only way to keep it is to stop storing the
> program's *state* and start storing its *history*: an append-only log of everything
> that happened, from which the current state can be **replayed** on any machine at any
> time. Get that right and a killed worker is a non-event — another one folds the history
> and continues mid-sentence. Get it wrong — cache a mutable balance, read the wall clock
> in workflow code, fire a timer from a `tokio::sleep`, deliver a task exactly-once — and
> the failure isn't a 500, it's a workflow that quietly did the wrong thing, or the same
> thing twice, and you find out in production. This project is where **crash-safety stops
> being a library feature you trust and becomes one you built**.

## What it does (the easy part)
- `StartWorkflow(workflow_type, workflow_id, task_queue, input)` → opens an execution,
  returns its `run_id`.
- A **worker** long-polls `PollWorkflowTask` / `PollActivityTask`, runs the workflow
  function / activities, and reports back with `RespondWorkflowTaskCompleted` (a list of
  commands) / `RespondActivityTaskCompleted`.
- The engine turns commands into history events and their side effects: schedule an
  activity, start a **durable timer**, or complete the workflow.
- `GetWorkflowResult(run_id)` → the terminal result once the workflow finishes.
- Everything is gRPC; the durable truth lives in Postgres.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system must
> do*, never *how*; figuring out the how is the entire point. A box only flips to ✅ when
> its Proof exists. Rule zero: **the history is the state.** There is no mutable
> "current state" you increment — state is always derived by replaying events. Break that
> and every challenge below gets harder.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Event-sourced history log — *the state IS the log*
A durable workflow can't keep its state in a struct in RAM (the process dies) or in a
mutable `status` column (it tells you *where* you are, never *how you got here* — and
"how you got here" is what a fresh worker needs to resume). Instead every fact is an
**immutable event** appended to a per-execution log: `WORKFLOW_STARTED`,
`ACTIVITY_SCHEDULED`, `ACTIVITY_COMPLETED`, `TIMER_STARTED`, `TIMER_FIRED`,
`WORKFLOW_COMPLETED`. Build the log in `src/history.rs`, backed by an append-only
`history_events` table.

**Done when ALL true:**
- [ ] Every execution's history is an **append-only** log — no code path updates or deletes a posted event; a correction is a *new* event, never an edit.
- [ ] Event ids are **monotonic and gapless per run** (1, 2, 3, …); a duplicate or skipped id is *rejected*, not silently written.
- [ ] An append of N events is **atomic**: either the whole ordered batch lands or none of it does — a crash mid-append can never leave a half-written, corrupt history.
- [ ] The full history for a run can be **loaded back in order**, and a **suffix** (events after id `k`) can be loaded on its own — the delta a sticky worker needs (V5).
- [ ] The mutable `status` column, if you keep one, is a **projection** that always agrees with the history — never a second source of truth the log can drift from.

**Proof:** integration tests (real Postgres, gated on `DATABASE_URL`) that: start writes
event 1 = `WORKFLOW_STARTED`; appends assign 2,3,4… and read back in order; a duplicate
event id fails and writes nothing; `load_history_after(k)` returns exactly the tail.
`docs/21-design.md` states why event-sourcing over a mutable state column.

*Concept to internalize:* **event sourcing** — state as a fold over an immutable event
log; why append-only + derived-state is what makes crash recovery even *possible*, and
the difference between a command (a request) and an event (a recorded fact).

### V2. Deterministic replay — *rebuild state from history, identically, every time*
State is not stored; it is **recomputed** by folding history left-to-right. Build the
fold in `src/replay.rs`: a *pure function* `replay(&[Event]) -> WorkflowState`. "Pure" is
the whole game — replay the same events on any worker, at any time, and you must get
byte-identical state. That is what lets a crashed execution resume on a different machine
as if nothing happened. It also forces the rule that defines workflow code: it may not
read the clock, roll a random number, or call the network directly — every such effect
would replay differently. Effects go out as *commands* and come back as *recorded
events*, which replay deterministically. The engine's stake is catching a worker whose
replay **diverged** from what history already records.

**Done when ALL true:**
- [ ] `replay` is a **pure function**: no clock, no IO, no randomness — the same events in always yield the same `WorkflowState` out.
- [ ] Replay is **fold-order-invariant**: replaying a history in one pass equals replaying it split into any two consecutive chunks — batching must not change the result.
- [ ] A **completed** history folds to a terminal state carrying the right result; pending activities/timers are reflected accurately at every prefix.
- [ ] A **malformed** history (a gap, an out-of-order id, an event that references an activity/timer that was never scheduled) is **rejected**, not folded into a wrong-but-plausible state.
- [ ] **Non-determinism is caught:** when a worker returns commands that contradict a recorded event, the engine rejects the task with a clear "expected X, got Y" — it does not silently corrupt the execution.

**Proof:** a property test that for any valid history, `replay(h)` is idempotent and
split-invariant (`prop_replay_is_deterministic`, no DB needed); a unit test that a
start→schedule→complete history folds to the expected terminal state; a test that
`check_determinism` flags a divergent command stream. `docs/21-design.md` lists the
workflow-code determinism rules (no wall clock, no rand, no direct IO) and *why*.

*Concept to internalize:* **deterministic replay** — a workflow as a pure function of its
history; why side effects must be recorded as events; and how the same mechanism that
enables recovery also lets the engine *detect* a workflow whose code changed underneath a
running execution (the "non-determinism error").

### V3. Durable timers — *a sleep that outlives the process*
A workflow that says "wait 3 days, then charge" cannot hold that delay in a
`tokio::sleep` — the process won't live three days, and if it dies the sleep is gone with
it. A durable timer is a **persisted due-time**: `StartTimer` writes a row *in the same
transaction* that appends `TIMER_STARTED` (so the timer can never be lost with the
process that created it), and a background scanner fires it later by appending
`TIMER_FIRED` and waking the workflow. Build it in `src/timers.rs`. Restart the whole
engine mid-wait and the timer still fires — because it was never in memory to begin with.

**Done when ALL true:**
- [ ] A started timer is **durable**: it and its `TIMER_STARTED` event commit atomically, so the timer survives a full engine restart with no in-memory state to lose.
- [ ] A due timer **fires exactly once** into history: the scanner may run repeatedly and overlap a restart, but `TIMER_FIRED` for a given timer lands at most once (idempotent firing).
- [ ] Firing is **atomic with the wake-up**: appending `TIMER_FIRED`, scheduling the follow-up workflow task, and marking the timer fired all happen together — a crash mid-fire leaves the timer still due to retry, never a fired timer with no wake-up.
- [ ] Two engine instances scanning concurrently **never double-fire** the same timer (claim with `SKIP LOCKED` or equivalent).
- [ ] The scan cost is **bounded by what's due**, not by total timers outstanding — an execution with a million dormant timers doesn't make every scan O(million).

**Proof:** a test that a scheduled timer fires after its delay and produces exactly one
`TIMER_FIRED`; a test that firing survives a simulated restart (drop and recreate the
service, timer still fires once); a concurrency test that two scanners produce one fire.
`docs/21-design.md` notes the scan interval ↔ firing-latency tradeoff and the index that
keeps the scan `O(due)`.

*Concept to internalize:* **durable timers** as persisted due-times swept by a scanner
(the "timer wheel" idea, done in a database); why at-least-once firing forces the fire to
be idempotent; and why the write must be transactional with the event that records it.

### V4. gRPC worker dispatch — *long-poll + at-least-once, the crash-safe way*
Workers aren't pushed work; they **long-poll** for it. `PollWorkflowTask` blocks until a
task is claimable or the poll times out. When a worker claims a task it takes a
**visibility-timeout lease**, not ownership: complete it in time and the lease releases;
crash first and the lease lapses, the task becomes claimable again, and another worker
replays and continues. Build the task-queue engine in `src/dispatch.rs`. This module is
also the server-side **orchestrator**: it validates the worker's commands against history
(V2), turns them into events + side effects (an activity task, a durable timer via V3, a
completion), and refreshes the sticky pin (V5).

**Done when ALL true:**
- [ ] `PollWorkflowTask` / `PollActivityTask` **long-poll**: they block up to a timeout for work and return an empty response (not an error) when there is none.
- [ ] Two workers polling the same queue **never receive the same task** — a claim is exclusive (e.g. `FOR UPDATE SKIP LOCKED`).
- [ ] Delivery is **at-least-once**: a task claimed but not completed within its visibility timeout becomes **claimable again**, so a crashed worker's task is retried, not lost.
- [ ] A **late** completion is rejected: a worker whose lease already lapsed (its task was reassigned) cannot commit its stale result on top of the new owner's — the task token / claim is checked.
- [ ] Completing a workflow task is **transactional**: validating determinism, appending the resulting events, scheduling side effects, and deleting the claim either all happen or none do — no event without its side effect, no side effect without its event.

**Proof:** a two-worker test that a single task is delivered once; a crash test that an
uncompleted task is redelivered after the visibility timeout and the workflow still
completes exactly once; a test that a stale-lease completion is refused. `docs/21-design.md`
names the visibility-timeout value and the crash-latency ↔ double-run tradeoff it sets.

*Concept to internalize:* **task-queue dispatch** — long-poll matching, the
visibility-timeout lease, and why **at-least-once delivery + deterministic replay +
idempotent effects** is the exact recipe that turns "a worker crashed" into a non-event.

### V5. Sticky workflow-state cache — *skip the replay you don't need*
Replay (V2) is correct but not free: rebuilding a long-running workflow's state means
folding its *entire* history on every task — 10,000 events just to make the 10,001st
decision. The fix is **sticky execution**: after a worker runs a task, route that
execution's next task *back to the same worker*, which kept the folded state in memory;
it then only needs the events since it last ran. Build the routing table in
`src/sticky.rs`. The lesson is where the cache lives — in one *specific* worker's memory —
so it is valid only while that worker is alive. If the sticky worker goes silent (The
Reaper), the pin expires and the execution falls back to the normal queue with a **full
replay**. Correctness never depends on the cache; it only makes the common case cheap.

**Done when ALL true:**
- [ ] After a worker completes a task, that execution's **next** workflow task is routed back to the **same** worker (a sticky hit), which receives only the events since it last ran — not the whole history.
- [ ] A sticky worker that **goes silent** past the stickiness TTL loses the pin, and the execution is picked up from the **normal queue with a full replay** — no task is stranded on a dead worker.
- [ ] The cache is a **pure optimization**: a sticky hit and a full replay produce the **same** resulting state and the **same** commands — correctness is identical with the cache on or off.
- [ ] The cache is **process-local** and never a source of truth — losing all of it (restart) costs replays, never correctness.
- [ ] Under steady load the **hit ratio is high** and measurable, and a hit replays **dramatically fewer** historical events than a miss.

**Proof:** a test that consecutive tasks for one execution stick to one worker and carry
only the delta; a test that killing the sticky worker reroutes to a full replay with an
identical outcome; a bench line showing the hit ratio and the events-replayed reduction
(cache on vs off). `docs/21-design.md` states the TTL and why it's tied to worker liveness.

*Concept to internalize:* **sticky execution / worker-affinity caching** — trading a
replay for an in-memory hit, why the cache must be liveness-bounded, and the discipline of
an optimization that is *never* allowed to change the answer.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] **gRPC contract is deliberate:** poll RPCs return an *empty* response (not an error status) on a timed-out long-poll, and a malformed task token is `INVALID_ARGUMENT`, not `INTERNAL`. *(Proof: RPC tests asserting the codes.)*
- [ ] **A non-determinism error is a distinct, actionable status** (`FAILED_PRECONDITION`), never a generic 500 — a worker author can tell "my code diverged" from "the engine broke". *(Proof: V2 test asserting the status.)*
- [ ] **Graceful shutdown** drains the in-flight gRPC calls *and* lets the timer scan loop finish its current pass on SIGTERM — no task claimed-and-abandoned by our own shutdown.

### Caching
- [ ] Sticky workflow-state cache implemented (V5), liveness-bounded, and proven to not change outcomes.
- [ ] `docs/21-design.md` states **why the durable state is never cached** — only the *derived* state is, and only in a worker that can be told to drop it. Caching the history itself would reintroduce the drift event-sourcing exists to prevent.

### Security
- [ ] **Input validation at the frontend:** an empty `task_queue`, a non-UUID `run_id`, an unknown `command_type`, or a command missing its required field is rejected with `INVALID_ARGUMENT` *before* touching the store. *(Proof: validation tests.)*
- [ ] **Task tokens are unforgeable-enough for the model and not trusted blindly:** a token is validated against the live claim, so a replayed or hand-crafted token can't commit a result for a task the sender doesn't hold. *(Proof: stale-token test.)*
- [ ] **Payloads are opaque and size-bounded:** workflow/activity inputs and results are treated as bytes the engine never executes, with a configured max size rejected cleanly. *(Proof: oversize-payload test.)*
- [ ] **No SQL injection:** every query is `sqlx` compile-time-checked (`query!`) — zero string-concatenated SQL.

### Observability
- [ ] `tracing` span per RPC (a workflow-task span should carry `run_id` and `event_id` so a log line ties back to the exact history position it advanced).
- [ ] Each dispatched task logs **run_id, task kind, sticky hit/miss, and events replayed** as structured fields.
- [ ] Counter/gauge metrics at `/metrics`: **workflow tasks & activity tasks dispatched, replays (sticky hit ratio), timers fired, executions completed|failed, and task-queue depth.** *(Proof: a metrics-render test asserting the recorded series.)*

---

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load + chaos test lives in `bench/`, the
   numbers in `docs/21-benchmarks.md`.
3. `docs/21-design.md` records the decisions the SPEC grades: **event-sourcing over a
   mutable state column (V1), the determinism rules (V2), the timer scan-interval
   tradeoff (V3), the visibility-timeout value (V4), and the sticky TTL (V5)**.
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p workflow-engine` are
   green; no `todo!()` remains on a checked path.

## 🐉 Boss fight — The Reaper

> Somewhere a process is dying every couple of seconds. A worker OOMs mid-task; the engine
> gets a rolling deploy; a pod is evicted while it holds a workflow's cached state. None of
> them got to finish. The Reaper doesn't care — it just keeps killing. Your engine's one
> job is to make every one of those deaths a *non-event*: the workflow that was halfway
> through resumes on another worker, from history, and finishes **exactly once** — no lost
> step, no activity run twice, no timer that never fired. The Reaper wins the instant a
> single workflow is left stuck, a single side effect happens zero or two times, or a
> replay comes back different.

**Arena:** `bench/` load + chaos test against a **release build** (`cargo run --release`)
with Postgres up. Start a flood of small workflows (each: start → 1 activity → a short
durable timer → complete) across a **pool of worker processes**, while a reaper **kills a
random worker every ~2s** for the duration. Each activity increments a durable,
idempotency-keyed side-effect counter so double-execution is *countable*. Snapshot: total
workflows started, total completed, and the side-effect counter, before and after.

**The boss falls when ALL true:**
- [ ] ≥ **500 workflows/sec** completed end-to-end, sustained for 60s on the mixed workload
  (no chaos) — the throughput floor.
- [ ] Under the reaper (a worker killed every ~2s), **100% of started workflows still reach
  a terminal state** — zero stuck executions, proven from history, not vibes.
- [ ] **Every activity effect happens exactly once:** the side-effect counter equals the
  number of activity schedules — zero losses, zero duplicates — across the whole run.
- [ ] **Zero non-deterministic-completion errors** over the run: every resumed execution
  replays cleanly (`FAILED_PRECONDITION` count == 0).
- [ ] **Sticky pays off without changing answers:** steady-state hit ratio ≥ **80%**, and a
  hit replays ≥ **5×** fewer historical events than a miss — with identical outcomes.
- [ ] **p99 task-dispatch latency ≤ 50ms** during the no-chaos run.

**Proof:** methodology + before/after numbers (the exactly-once side-effect count front and
center) in `docs/21-benchmarks.md` (hardware noted, reaper cadence and commands
reproducible via `bench/`).

## Suggested order of attack
1. Get the boring path working: `StartWorkflow` writes a `WORKFLOW_STARTED` event straight
   to Postgres and enqueues one workflow task; a worker polls it, sends `CompleteWorkflow`,
   and `GetWorkflowResult` reads the result. No activities, no timers, no sticky.
2. Make history append-only and load/replay it into state (V1 + V2) — prove replay is pure.
3. Add activities: schedule → activity task → complete → wake the workflow, all through
   history (V4's happy path).
4. Add durable timers and their scan loop (V3).
5. Harden dispatch: visibility-timeout leases, at-least-once redelivery, stale-token
   rejection (V4) — now a killed worker is recoverable.
6. Add the sticky cache and prove it never changes an outcome (V5).
7. Benchmark, unleash The Reaper, document, tune.

## Run the dependencies
```bash
docker compose up -d        # postgres
cp .env.example .env        # then fill in values
sqlx migrate run            # apply migrations (install: cargo install sqlx-cli)
cargo run -p workflow-engine
```
