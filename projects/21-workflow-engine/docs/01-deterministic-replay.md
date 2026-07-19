# Deterministic Replay — State as a Pure Fold

> **What this teaches:** how a crashed workflow resumes on a machine that has never
> seen it — by *recomputing* its state from history with a pure function — and why
> that purity forbids workflow code from reading the clock, rolling dice, or touching
> the network. No prior knowledge assumed (read
> [00-event-sourcing-history-log.md](00-event-sourcing-history-log.md) first).
>
> **Prepares you for:** [SPEC](../SPEC.md) **V2 — Deterministic replay**, built in
> [replay.rs](../src/replay.rs): `replay(&[Event]) -> WorkflowState` and
> `check_determinism`. The state you fold into is
> [`WorkflowState`](../src/model.rs); the events come from V1's log.

---

## The one sentence to hold onto

**A workflow's state is not stored anywhere — it is a pure function of its history,
so any machine that has the history can recompute the state, identically, forever.**

---

## 1. The problem before the solution

V1 gave us the log. Now the actual resume has to happen. Worker A dies at t4 holding
this in-memory picture of run `7f3a…`:

```text
status: Running
pending_activities: {}                 (the charge already completed)
started_timers: { "wait-3d" → fires at t+3d }
next_event_id: 7
```

Worker B — different machine, cold start — must reconstruct **exactly** that picture
from events 1–6, or the workflow continues from a state it was never in. "Exactly"
is not rhetorical. Consider what each kind of near-miss does:

| Reconstruction error | What goes wrong downstream |
|---|---|
| B thinks the charge is still pending | B waits forever, or schedules it *again* — customer charged twice |
| B misses the started timer | the workflow ships immediately instead of waiting 3 days |
| B computes `next_event_id = 6` instead of 7 | B's next append collides with event 6 — rejected as a duplicate (V1), workflow stalls |
| B's state differs in any field at all | B's *decisions* differ, and the history stops making sense |

So reconstruction = re-running the derivation. And re-running only works if the
derivation gives the same answer on any machine, at any time, in any batching. That
property has a name: the fold must be a **pure function**.

```text
replay(events) -> WorkflowState        // same events in ⇒ same state out. Always.
```

No clock. No randomness. No IO. Only the events decide the result.

---

## 2. Why workflow *code* gets rules too

Here is the subtle jump. It's not just the engine's fold that must be pure — the
**workflow function the user writes** is *also* replayed. When worker B picks up the
run, it re-executes the workflow function from the top, feeding it the recorded
history so every past "effect" returns its recorded result. The function must
therefore take the **same path** it took the first time. Any hidden input breaks
that:

| Forbidden in workflow code | The specific divergence it causes on replay |
|---|---|
| `now()` / wall clock | first run at 09:00 took the "before noon" branch; replay at 14:00 takes the other — B walks a path history never recorded |
| `rand()` | first run drew 0.3 and skipped the discount; replay draws 0.9 and schedules an activity history has never heard of |
| direct network / DB call | the world changed between runs — different response, different path; also the side effect just happened *twice* |
| iterating a `HashMap` | iteration order differs per process → activities scheduled in a different order than recorded |
| reading an env var / config | deploy changed it between run and replay |

This is why Temporal's docs famously ban `time.Now()`, `rand`, and bare goroutines
inside workflow code — the folklore rule you may have heard. Now you know the
mechanism: **replay is re-execution, and re-execution must be bit-for-bit
repeatable.**

The last two rows are the nastiest kind: code that is *accidentally* deterministic
in tests (HashMap happened to iterate the same way) and diverges only on the
production crash-recovery path — exactly when replay must work.

### So how does a workflow ever do anything?

Through the command/event loop from doc 00. The workflow never performs an effect;
it *asks* for one:

```text
                        first execution                     replay
                   ──────────────────────────      ─────────────────────────
workflow code:     "schedule charge_card"           "schedule charge_card"
                            │                               │
engine:            records ACTIVITY_SCHEDULED       sees it's already recorded —
                   runs it on an activity worker    does NOT run it again
                   records ACTIVITY_COMPLETED       │
                        {txn_9931}                  │
                            │                       │
workflow code       gets "txn_9931"                 gets "txn_9931"
resumes with:       (from the live result)          (from the RECORDED event)
```

The effectful world is replayed **from its recording**. That is also how a workflow
legitimately gets the time or a random number: the engine (or an activity) produces
it once, records it as an event, and replay reads the recording. `now_ms()` in
[model.rs](../src/model.rs) carries exactly this warning — it's for the *server*
stamping events, never for workflow decisions.

---

## 3. The fold, concretely

`replay` starts from [`WorkflowState::initial()`](../src/model.rs) and applies
events in `event_id` order. Trace the order workflow's six events through the state:

```text
 event                          state after folding it
─────────────────────────────────────────────────────────────────────────────
 (initial)                      Running · next_event_id=1 · no pending anything
 1 workflow_started             Running · next=2
 2 workflow_task_completed      Running · next=3            (bookkeeping only)
 3 activity_scheduled {charge}  Running · next=4 · pending_activities={3: charge}
 4 activity_completed {txn…}    Running · next=5 · pending_activities={}
 5 workflow_task_completed      Running · next=6
 6 timer_started {wait-3d}      Running · next=7 · started_timers={wait-3d: t+3d}
```

Fold events 1–6 on any machine, any day, and you get worker A's exact picture —
including `next_event_id = 7`, which is what lets B's next append slot in without
colliding. Two properties the SPEC demands fall straight out of "it's a fold":

- **Idempotent:** `replay(h)` twice gives the same answer — there's nothing to be
  different.
- **Split-invariant:** folding 1–6 in one pass ≡ folding 1–3, keeping the state, then
  folding 4–6 on top. Batching is an *engine* choice (sticky workers fold deltas!)
  and must not change meaning. This is the property test the SPEC names
  (`prop_replay_is_deterministic`) — note it needs **no database**; replay is just
  `Vec<Event>` in, state out.

### Malformed histories are rejected, not repaired

What should `replay` do with `[1, 2, 4]` (a gap), or an `activity_completed` naming
a schedule that never happened? The tempting move is to shrug and fold what's there.
The SPEC says reject — because a gap is not a formatting problem, it is **evidence
of corruption** (a lost write, a bug in V1's atomicity), and folding around it
produces a state that is *plausible and wrong*, the worst combination. A loud
[`AppError`](../src/error.rs) at replay time is the last tripwire before a corrupt
state starts making decisions.

---

## 4. The engine's tripwire: `check_determinism`

Purity buys recovery. It also buys something sneakier: **detection of changed
code**.

Scenario: a workflow is mid-flight; history already records
`3 activity_scheduled {charge_card}`. Meanwhile someone deploys new workflow code
where the first step is now `check_fraud`. Worker B picks up the run and replays —
but its (new) code, re-executed from the top, *asks to schedule `check_fraud`* at
the point where history says `charge_card` was scheduled.

The recorded past and the re-executed present disagree. Something is non-deterministic
— maybe a deploy, maybe a `HashMap` — and the engine can *see* it, because the worker
hands back its commands and history already knows what they must be:

```text
history (the record)              worker's replayed commands
──────────────────────            ───────────────────────────
3 activity_scheduled              ScheduleActivity {
  { charge_card }        vs         check_fraud        ← expected charge_card,
                                  }                      got check_fraud → REJECT
```

`check_determinism(history, replayed_through, commands)` in
[replay.rs](../src/replay.rs) is that comparison: fold up to where the worker
claims it replayed, then check the commands against what the *later recorded events*
imply. Three rules of the game:

1. **First-ever task:** nothing is recorded yet, so nothing can contradict —
   commands are accepted and *become* the record.
2. **Mismatch:** fail with `expected X, got Y`, *before* anything commits. That
   message is a workflow author's best debugging clue; the wire status is
   `FAILED_PRECONDITION`, a typed "your code diverged," never a generic 500 (the
   horizontal checklist grades this).
3. **The check runs pre-commit** inside V4's completion transaction — a divergent
   worker must corrupt *nothing*.

Real engines live with this constantly: Temporal has versioning/patch APIs that
record "which code version made this decision" *into history* so deploys don't break
running executions. You don't need to build that — but you should be able to say why
it must be an event.

You've met this theorem before: **Raft (project 09) is the same statement** — same
log + deterministic apply = same state machine on every node. There the log is
written by consensus among peers; here it's written by one engine and replayed by
many workers. The fold's contract is identical.

---

## 5. The design space you'll navigate (not the answers)

- **What does each event type do to the state?** The doc comment in
  [replay.rs](../src/replay.rs) sketches the mapping; the interesting decisions are
  the edge ones — what does a *terminal* state accept afterward? what exactly do the
  task bookkeeping events advance?
- **Where does id-validation live in the fold?** You must reject gaps and
  out-of-order ids — decide whether that's a precondition pass or woven into the
  fold, and what error carries enough context to debug a corrupt run.
- **What, precisely, does a command "match"?** `check_determinism` compares a
  [`Command`](../src/model.rs) against recorded events — decide which fields are
  identity (type? activity_type? input bytes?) and what "the events after
  `replayed_through` imply" means when one command produced several events.
- **How do you generate *valid* random histories** for the property test? The
  generator has to respect the grammar (can't complete an unscheduled activity) —
  designing it will teach you the state machine as well as the fold does.

**Hard stop.** The fold body and the divergence check are the V2 exercise — the two
`todo!()`s in [replay.rs](../src/replay.rs). `/hint` for nudges, `/quest` to build it
against acceptance tests.

---

## 6. Mental model summary

| Idea | One-liner |
|---|---|
| Replay | State = `fold(initial, events)` — recomputed, never fetched |
| Purity | No clock/rand/IO in the fold *or* in workflow code; hidden inputs = divergence |
| Command/event loop | Workflow code asks; engine records; replay reads the recording instead of re-doing |
| Split invariance | Batching is an engine choice; `replay(h)` ≡ replay in chunks — provable by property test |
| Malformed history | Evidence of corruption — reject loudly, never fold into plausible-but-wrong |
| `check_determinism` | History already knows the commands; a contradiction = changed/nondeterministic code, caught pre-commit |
| The shared theorem | Same log + pure fold = same state (Raft's apply loop, redux reducers, this) |

## Where you'll build this

**Module:** [src/replay.rs](../src/replay.rs) — two `todo!()`s: `replay` and
`check_determinism`. Pure functions: test with plain `Vec<Event>`, no Postgres.

**This doc unlocks V2's Done-when criteria:** pure fold · fold-order invariance ·
correct terminal/pending state at every prefix · malformed-history rejection ·
divergence caught with "expected X, got Y". Proof: the property test, the fold unit
tests, the `check_determinism` test, and the determinism-rules section of
`docs/21-design.md` — see the V2 block in [SPEC.md](../SPEC.md).

**Next:** the fold handles "the process died." But "wait 3 days" needs a sleep no
process holds — [02-durable-timers.md](02-durable-timers.md).
