# Backend Fundamentals Woven Through This Project

> **What this teaches:** the cross-cutting contracts from the [SPEC](../SPEC.md)'s
> **horizontal checklist** — gRPC status semantics, token trust, payload hygiene,
> graceful shutdown, and the observability story — each grounded in why a *durable
> execution engine* specifically needs it. No prior knowledge assumed; the verticals
> are covered in docs [00](00-event-sourcing-history-log.md)–[04](04-sticky-execution.md).
>
> **Anchored to:** [main.rs](../src/main.rs) (the gRPC adapter + shutdown wiring —
> already implemented), [error.rs](../src/error.rs) (`AppError` → status codes),
> [metrics.rs](../src/metrics.rs) (the graded series), and
> [workflow.proto](../proto/workflow.proto).

---

## The one sentence to hold onto

**An engine that other people's workers talk to is judged by its contracts — status
codes, tokens, bounds, shutdown behavior, and metrics are the API just as much as
the RPCs are.**

---

## 1. gRPC status codes are semantics, not decoration

A worker author debugging at 2 a.m. has exactly one signal from your engine: the
status code. The horizontal checklist demands each failure class map to a *distinct,
actionable* code — because each one tells the author to do something different:

| Situation | Status | What it tells the worker author |
|---|---|---|
| long-poll timed out, no work | **OK + empty response** | normal life; poll again — never an error, never red in the logs |
| empty `task_queue`, non-UUID `run_id`, unparseable token, unknown command type | `INVALID_ARGUMENT` | *your request* is malformed; fix the caller |
| unknown run id | `NOT_FOUND` | you asked about something that doesn't exist |
| replay diverged from recorded history (V2) | `FAILED_PRECONDITION` | *your workflow code* is non-deterministic or changed under a running execution — retrying the same task won't help until the code is fixed |
| Postgres is down | `UNAVAILABLE` | *the engine's* problem; back off and retry |
| a bug in the engine | `INTERNAL` | file a bug against the engine |

Two rows deserve a pause:

- **Empty ≠ error.** A timed-out poll is the single most common response an idle
  system produces. Encode it as a failure and every healthy worker's logs turn to
  noise, retry/backoff logic triggers on non-events, and real errors drown. The
  scaffold already honors this — see `poll_workflow_task` in
  [main.rs](../src/main.rs) returning `PollWorkflowTaskResponse::default()`.
- **Non-determinism is `FAILED_PRECONDITION`, never 500.** The request was
  well-formed; the *replay contract* was violated. The distinction "my code
  diverged" vs "the engine broke" is the difference between a worker author fixing
  their workflow and them filing a useless bug against you. The mapping already
  lives in [error.rs](../src/error.rs) — your V2 code just has to return
  `AppError::NonDeterministic` with a message worth reading (`expected X, got Y`).

The general lesson: **design your error taxonomy as carefully as your success
types.** `AppError`'s variants are the engine's failure vocabulary; the
`From<AppError> for Status` impl is a *published contract*, and the checklist's
proof is RPC tests asserting the codes.

Note also what the adapter does with internal errors: log the details server-side,
hand back a generic message (`"workflow store unavailable"`). An error string that
leaks SQL or schema is a free map of your internals to anyone probing.

---

## 2. Trust boundaries: validate at the door, verify ownership at the vault

Requests cross two distinct trust checks, and conflating them is how engines get
corrupted:

**At the door — input validation (`INVALID_ARGUMENT`, before touching the store).**
The frontend rejects garbage while it's still cheap and harmless: empty
`task_queue`, non-UUID `run_id`, unknown `command_type`, a command missing its
required field. [main.rs](../src/main.rs) already does much of this
(`decode_command` refuses an empty `activity_type`, an unspecified command type, a
malformed token). Why *before* the store? A validation that happens mid-transaction
has already spent a connection, taken locks, maybe written rows it must roll back —
and a validation error that surfaces as a DB error lies about whose fault it is.

**At the vault — ownership verification (the task token vs the live claim).**
A [`TaskToken`](../src/model.rs) is not a secret capability — it's JSON bytes any
process could fabricate (`{run_id, kind, scheduled_event_id}`). The checklist's
phrase is precise: *unforgeable-enough for the model and not trusted blindly*. The
security isn't in the token; it's in the check — a completion is honored only if
the token matches a **live claim** the engine itself recorded at poll time
(doc [03](03-task-dispatch-and-leases.md) §4). A replayed, stale, or hand-crafted
token names a claim that doesn't exist → rejected, nothing written. Validity isn't
ownership.

**Payloads: opaque and bounded.** Workflow inputs and activity results are `bytes`
the engine schedules but *never interprets or executes* — that opacity is a security
posture (nothing to inject into) and an architecture one (the engine stays
workflow-agnostic). The missing half is **size**: payloads land in `history_events`
rows and get shipped to every replaying worker forever. A 200 MB input isn't a
security exploit, it's a durability grenade — it bloats the log, slows every future
replay of that run, and can wedge the poll path. The checklist wants a configured
max size, rejected cleanly (`INVALID_ARGUMENT`) at the door. That check doesn't
exist in the scaffold — it's yours to add, with the oversize-payload test as proof.

**SQL:** every query compile-time-checked (`sqlx::query!`), zero string-concatenated
SQL — house rule, and the reason injection isn't on this engine's threat list.

---

## 3. Graceful shutdown: don't let your own deploy be The Reaper

The whole project is about surviving *unplanned* death. Ironically, the most
frequent killer is your own rolling deploy — and unlike a crash, a deploy sends
SIGTERM first, which means you get to choose what dying looks like. The checklist
requires two drains:

1. **In-flight gRPC calls finish.** The tonic server stops accepting new
   connections but lets in-flight calls complete — including parked long-polls. A
   completion cut off mid-transaction is safe (the transaction aborts, the lease
   lapses, V4 redelivers) but *wasteful*: you just converted a clean finish into a
   visibility-timeout stall for that workflow.
2. **The timer scan loop finishes its current pass.** A pass killed between claim
   and commit leaves its work to the next scan (V3's atomic fire makes that safe) —
   but a loop that exits *cleanly at a pass boundary* leaves nothing in doubt.

Look at how [main.rs](../src/main.rs) already wires this — it's a pattern worth
stealing for every service you ever write:

```text
ctrl_c  ──►  watch::channel(false→true)  ──┬──►  tonic  serve_with_shutdown(…)
                                           ├──►  axum   with_graceful_shutdown(…)   (metrics sidecar)
                                           └──►  timers::scan_loop  tokio::select! → break
```

One broadcast (`tokio::sync::watch`), every long-lived task selects on it, `main`
joins them all before exiting. The scaffold gives you the wiring; the checklist's
box flips when the *behavior* is demonstrated — no task claimed-and-abandoned by
your own shutdown.

The deep point: at-least-once machinery (V3/V4) makes ungraceful death *correct*,
and graceful shutdown makes planned death *cheap*. You need both — correctness for
The Reaper, cheapness for the ten deploys you'll do this week.

---

## 4. Observability: instrument the promise, not just the process

Every project in the gauntlet has metrics. What's specific here is *what* must be
observable: this engine's product is a guarantee ("exactly once, to completion"),
and a guarantee you can't measure is a guess. The graded series
([metrics.rs](../src/metrics.rs) — constants already defined, call sites are your
job) each answer a question the boss fight will ask out loud:

| Series | The question it answers |
|---|---|
| `workflow_workflow_tasks_total` / `workflow_activity_tasks_total` | is work flowing? at what rate? |
| `workflow_replays_total{sticky=hit\|miss}` | is the sticky cache earning its keep? (V5's ≥ 80% target is `hit/(hit+miss)` on this series) |
| `workflow_timers_fired_total` | are durable timers actually firing? |
| `workflow_executions_completed_total{outcome}` | is the *promise* being kept — started == terminal, under chaos? |
| `workflow_task_queue_depth` (gauge) | are workers keeping up? this is the backpressure signal — rising depth under The Reaper means recovery isn't. |

Tracing gets the same treatment: a span per RPC carrying `run_id` and `event_id`,
so one log line ties back to the *exact history position* it advanced. In an
event-sourced system this is a superpower unique to the design — because state is a
log position, "where was the system when this happened" is a first-class, queryable
fact. Each dispatched task logs `run_id`, task kind, sticky hit/miss, and events
replayed as structured fields — which together are exactly the dataset you'll need
to debug the boss fight's numbers.

Mechanics worth knowing: gRPC has no natural place for a scrape endpoint, so a tiny
axum sidecar serves `/metrics` + `/healthz` on a second port (9090) beside tonic
(7233) — already wired. The `metrics` facade writes to a process-global recorder;
until `install()` runs, the macros are no-ops, which is why unit tests need no
metrics setup.

---

## 5. The caching policy, stated as policy

One checklist line is pure principle and worth engraving: **the durable history is
never cached — only derived state is, and only in a process that can be told to
drop it.** Cache the log itself (in Redis, in a worker, anywhere) and you've created
a second copy of the truth that can drift — the exact disease event sourcing exists
to cure. The folded `WorkflowState` in a sticky worker is safe to cache *because*
it's derived: throw it away and the log regenerates it, byte-identical (V2).
`docs/21-design.md` must say this in your own words; doc
[04](04-sticky-execution.md) §3 has the full argument.

---

## 6. Mental model summary

| Contract | One-liner |
|---|---|
| Status codes | A failure taxonomy is API: empty ≠ error, `FAILED_PRECONDITION` = "your replay diverged", 500 = "our bug" |
| Validation at the door | Reject malformed input with `INVALID_ARGUMENT` before a transaction spends anything |
| Token check | Validity isn't ownership — a completion is honored only against the live claim |
| Opaque bounded payloads | Bytes the engine never executes, capped before they become permanent log weight |
| Graceful shutdown | One watch channel, every loop selects on it — planned death should cost nothing |
| Metrics | Instrument the *promise*: completions vs starts, hit ratio, queue depth, timers fired |
| Cache policy | Never the history; only derived state, only where it can be dropped |

## Where you'll build this

No single module — these thread through all five verticals as you build them:
validation and status codes ride [error.rs](../src/error.rs) + the adapter in
[main.rs](../src/main.rs) (partly done — the payload size cap is yours to add);
the token check lands inside V4's completion transaction; metric call sites go
where [dispatch.rs](../src/dispatch.rs) and [timers.rs](../src/timers.rs) TODOs
name them; shutdown wiring exists and needs its behavior proven.

**This doc unlocks the horizontal checklist** in [SPEC.md](../SPEC.md): the
Protocols, Security, and Observability boxes (the Caching boxes are graded with V5).
Proof per box: RPC status tests, validation tests, the stale-token and
oversize-payload tests, and the metrics-render test.
