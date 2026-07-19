# Concept Bank — Project 21: Workflow Engine (Temporal-lite)

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted. Rule zero governs everything: **the history is the state.**

---

## 🧠 Card 1 — Event sourcing: store history, not state *(V1 · `src/history.rs`)*

**The problem.** "Charge the card, wait 3 days, ship, email" is a function that outlives every process that runs it — it will be interrupted by crashes, deploys, and evictions many times before it finishes. A struct in RAM dies with the process. A mutable `status = 'shipping'` column tells a fresh worker *where* the workflow is but not *how it got there* — which activities already ran? what did they return? — and "how it got here" is exactly what resuming mid-sentence requires.

**The idea.** Store every fact as an immutable event appended to a per-execution log: `WORKFLOW_STARTED`, `ACTIVITY_SCHEDULED`, `ACTIVITY_COMPLETED(result)`, `TIMER_STARTED`, `TIMER_FIRED`, `WORKFLOW_COMPLETED`. State is always *derived* by folding the log; any status column is a projection that must never disagree with it. Event ids are monotonic and gapless per run; appends are atomic batches (a crash mid-append can't leave half a fact). Distinguish sharply: a **command** is a request to do something; an **event** is the durable record that it happened.

**In the wild:** Temporal/Cadence (this project's namesake — every workflow is an event history), banking ledgers (project 18 is event sourcing with money), Kafka-based event-sourced services, git (commits = events, checkout = the fold).

**You own it when you can explain:**
- [ ] Why "where you are" (status) is insufficient for resumption and "how you got here" (history) is sufficient — with the resumed-mid-workflow example.
- [ ] Append-only discipline: corrections are new events; what editing a posted event would do to every derived view and every replay.
- [ ] Why ids must be gapless-monotonic (ordering *is* meaning in a log) and why a duplicate id is rejected, not overwritten.
- [ ] Atomic batch appends: the half-written-history corruption a crash mid-append would cause otherwise.
- [ ] Command vs event, and why the engine records events *before* their consequences become visible to anyone.
- [ ] Why suffix loading (`events after id k`) exists — the delta a sticky worker (Card 5) consumes.

**Depth probes:**
- Where has this exact idea already appeared in the gauntlet? (Project 18's ledger, project 08's log, Raft's log in project 09 — name what each derives from its log.)
- Histories grow forever for long-lived workflows. What's the mitigation, and what does it echo (Raft snapshots / "continue-as-new")?

**Trap:** keeping a status column that code *writes to directly* "for query convenience". The instant it's written independently of the log, you have two sources of truth — and the drift event sourcing exists to prevent is back.

---

## 🧠 Card 2 — Deterministic replay: the state machine is a pure fold *(V2 · `src/replay.rs`)*

**The problem.** A worker holding a workflow's in-memory state dies. Another worker must reconstruct that state *exactly* — same pending activities, same variables, same next decision — or the workflow continues from somewhere it never was. Reconstruction means re-running the fold; re-running means the fold must produce the identical result on any machine, any time, any batch split. One `now()` call in workflow code and the replay diverges silently.

**The idea.** `replay(&[Event]) -> WorkflowState` is a **pure function**: no clock, no randomness, no IO — those would replay differently than they ran. Effects follow one loop: workflow code emits *commands*; the engine executes them and records *events*; replay consumes the recorded events, so the effectful world is replayed from its recording, deterministically. Purity also arms the engine's tripwire: when a worker's commands contradict what history already recorded (because the workflow *code* changed under a running execution), the engine detects the divergence and rejects — "expected ScheduleActivity(charge), got ScheduleActivity(refund)" — instead of corrupting the execution.

**In the wild:** Temporal's determinism constraints are famous developer folklore (no `time.Now()`, no `rand`, no bare goroutines in workflow code — now you know exactly why); the same replay idea runs event-sourced aggregates and redux-style reducers.

**You own it when you can explain:**
- [ ] Why purity is the *entire* mechanism: each forbidden operation (clock, rand, IO) mapped to the specific divergence it causes on replay.
- [ ] The command/event loop: how workflow code "does" side effects without doing them — and what actually happens on replay (recorded events substitute for the world).
- [ ] Fold-split invariance: why replay(h) must equal replay(h[..k]) then replay-rest — batching is an engine choice that must not change meaning.
- [ ] The non-determinism check as a safety feature: what worker-vs-history divergence detects (a code deploy changed a running workflow's logic) and why the failure must be loud, typed (`FAILED_PRECONDITION`), and pre-commit.
- [ ] How a workflow legitimately gets the time or a random number (as a recorded event/side-effect marker, folded like everything else).
- [ ] Why malformed histories (gaps, references to never-scheduled activities) are rejected rather than folded into plausible-but-wrong state.

**Depth probes:**
- How do real engines version workflow code so deploys don't break running executions (patch/version markers recorded into history)?
- Raft's apply loop (project 09) demands the same determinism. What's the shared theorem — same log + pure fold = same state — and where do the two systems differ (who writes the log)?

**Trap:** code that's *accidentally* deterministic in tests — iterating a HashMap, reading env vars, formatting floats. Replay bugs from incidental nondeterminism are the worst kind: they surface only on the production crash-recovery path, i.e., exactly when you need replay to work.

---

## 🧠 Card 3 — Durable timers: a sleep that outlives the process *(V3 · `src/timers.rs`)*

**The problem.** "Wait 3 days" cannot be `tokio::sleep(3 days)` — the process will deploy, crash, or scale down long before it fires, and the sleep dies with it. The delay must be a *fact in the database*, not a state in a runtime. And firing it has a distributed sting: the scanner that fires timers runs on multiple engine instances, may crash mid-fire, and may run twice — while `TIMER_FIRED` must land in history exactly once.

**The idea.** `StartTimer` persists a due-time row **in the same transaction** as its `TIMER_STARTED` event — the timer can't be lost with the process that created it, because it was never *in* the process. A background scanner sweeps due timers: append `TIMER_FIRED` + schedule the wake-up task + mark fired, atomically — a crash mid-fire leaves the timer still due (retry), never fired-without-wakeup. Concurrent scanners dedupe with the claim pattern (`SKIP LOCKED` — project 04, again). The scan reads only what's due (index on due-time), so a million dormant timers cost nothing per sweep.

**In the wild:** Temporal durable timers (workflows sleeping *years* is a real, advertised feature), Sidekiq/Quartz scheduled jobs, your project 04 `run_at` — this is that idea promoted to a first-class, exactly-once event.

**You own it when you can explain:**
- [ ] Why the timer row and its event must commit atomically — the orphan each half-commit leaves (a timer no history explains / an event no scanner will fire).
- [ ] At-least-once firing forced into exactly-once history: which mechanism dedupes overlapping scanners and restarts.
- [ ] The atomic fire bundle (event + wake-up + mark) and what each crash-point in between would strand without it.
- [ ] The scan-cost argument: why O(due) not O(all timers), and which index makes it so.
- [ ] The scan-interval dial: firing latency vs scan load — and why "a timer fires within interval+ε of due" is the honest contract.

**Depth probes:**
- Timer *cancellation* (workflow takes the other branch first): what races with an in-flight fire, and who wins?
- Compare with a timer wheel in memory (project 14's pacing) — when does the DB-swept design win (durability, horizontal engines) and what does it give up (precision)?

**Trap:** firing timers from an in-memory queue "hydrated from the DB at boot". Between hydrate and fire, another instance does the same — double-fire — and a crash after hydrate loses pending fires until reboot. The database *is* the timer wheel; memory is at most a hint.

---

## 🧠 Card 4 — Dispatch: long-poll + leases + transactional completion *(V4 · `src/dispatch.rs`)*

**The problem.** Work must reach workers with none lost and none duplicated-in-effect — while workers crash holding tasks, complete tasks *after* losing their claim (the zombie-worker race), and poll from many processes at once. Push-based dispatch needs the engine to track worker liveness; naive pull either busy-polls or waits seconds. And completion is a multi-part act (validate, append events, schedule side effects, release the claim) that must not half-happen.

**The idea.** Workers **long-poll**: the call blocks until a task is claimable or times out empty (an empty response, not an error). A claim is a **visibility-timeout lease** (project 04's pattern as the load-bearing wall): crash → lease lapses → task redelivers → the new worker *replays history* (Card 2) and continues. The zombie race is closed by the task token: a completion from a lapsed lease is rejected — the stale worker cannot commit over the new owner. And completion is **one transaction**: determinism check + event append + side-effect scheduling + claim release, all or nothing. The full recipe deserves saying aloud: **at-least-once delivery + deterministic replay + idempotent effects = a crashed worker is a non-event.**

**In the wild:** Temporal's task queues work exactly this way (long-poll, sticky hints, workflow-task completion as a batch of commands); SQS + Lambda is the lease shape; the transactional completion is the outbox idea (project 18) inside the engine.

**You own it when you can explain:**
- [ ] Long-poll vs push vs busy-poll: what each costs the engine and the worker, and why empty-response-on-timeout is the right contract.
- [ ] The zombie-worker race in full: lease lapses, task redelivered and completed by B, then A's late completion arrives — trace why the token check is the *only* thing standing between that and corrupted history.
- [ ] Why completion must be transactional, by enumerating the stranded states of each partial combination (events without side effects, side effects without events, claim released twice).
- [ ] The recipe's three legs and why removing *any one* breaks it (no lease → lost tasks; no determinism → wrong resumption; no idempotency → doubled effects).
- [ ] The visibility-timeout dial, inherited from project 04 but now with replay cost attached to every redelivery.

**Depth probes:**
- Workflow tasks vs activity tasks: why does the engine treat them differently (deterministic decision-making vs arbitrary effectful code), and which one carries retries in its own policy?
- What makes an *activity* idempotent here — and why does the boss fight count side effects with an idempotency-keyed counter rather than trusting logs?

**Trap:** accepting a completion because the payload looks valid. Validity isn't ownership — the token-vs-live-claim check is the difference between "a worker finished its task" and "some process wrote plausible history."

---

## 🧠 Card 5 — Sticky execution: a cache that must never change the answer *(V5 · `src/sticky.rs`)*

**The problem.** Replay is correct and *expensive*: a long-running workflow folds 10,000 events to make decision 10,001 — on every task. Multiply by throughput and the engine spends its life re-deriving state it derived seconds ago. But the obvious fix — cache the folded state — has a landmine: the cache lives in *one specific worker's memory*, and that worker can die, hang, or get deployed away at any moment.

**The idea.** **Sticky execution**: after a worker completes a task, route that execution's next task back to the *same* worker, shipping only the events since it last looked (the suffix from Card 1). Hit: fold a handful of events instead of thousands. The safety design is the lesson: the pin is **liveness-bounded** (a TTL; a silent worker loses it and the execution falls back to the normal queue with a full replay), and the cache is **never a source of truth** — losing all of it costs replays, never correctness. The discipline in one sentence: a sticky hit and a full replay must produce identical state and identical commands, provably.

**In the wild:** Temporal's sticky task queues (workers advertise per-instance queues; the engine routes-with-fallback exactly like this); the general shape — worker-affinity caching with liveness-bounded validity — shows up in session affinity and stateful stream processing (Flink key groups).

**You own it when you can explain:**
- [ ] The replay-cost arithmetic that motivates stickiness (events × folds/sec) and where the win comes from (delta, not full history).
- [ ] Why the pin must expire on worker silence — the stranded-execution failure if tasks kept routing to a dead worker's queue.
- [ ] "Pure optimization" as a testable property: same outcome with the cache hot, cold, or lost — and what test proves it (kill the sticky worker mid-execution; compare results).
- [ ] Why the fallback is *full replay from the log* — the log's completeness is what makes the cache disposable.
- [ ] The metrics that justify the whole card: hit ratio, events-replayed-per-task hit vs miss.

**Depth probes:**
- What invalidates a sticky cache besides worker death? (Code deploy — the folded state came from old code; the non-determinism check from Card 2 is the backstop.)
- Compare with project 20's query cache and project 01's Redis: all three are "derived, disposable, never authoritative" — what's the shared design test for any cache you ever add?

**Trap:** letting correctness *quietly* depend on the cache — e.g., only the sticky worker can complete an execution, or the fallback path is untested. The Reaper boss fight kills workers every 2 seconds precisely to prove the fallback is real.

---

## ⚡ Rapid-fire round

- [ ] The gRPC contract as designed semantics: empty response on poll timeout (not an error), `INVALID_ARGUMENT` for malformed tokens, `FAILED_PRECONDITION` for non-determinism — a worker author can tell "my bug" from "engine's bug" by status code alone.
- [ ] Payloads are opaque bytes with a size cap — the engine schedules them, never interprets or executes them.
- [ ] Task tokens validated against the live claim — a replayed or forged token commits nothing.
- [ ] Graceful shutdown: drain in-flight RPCs, let the timer scan finish its pass — never claimed-and-abandoned by your own deploy.
- [ ] The dashboard that tells the story: task-queue depth, sticky hit ratio, events replayed, timers fired, executions completed vs failed.
- [ ] Why the durable history is never cached — only derived state is, and only where it can be dropped (Card 5's whole point, stated as policy).

## 🔗 Connects to

- This project is the gauntlet's ideas converging: project 04's leases + project 08/09's log-as-truth + project 18's event-sourced ledger + a purity discipline — durable execution is what they add up to.
- Raft (project 09) and replay (Card 2) share the deterministic-fold theorem; snapshots and "continue-as-new" share the history-compaction answer.
- Project 16's control-plane state machine is the *small* version of this; a workflow engine is what it grows into when transitions get arbitrarily rich.
