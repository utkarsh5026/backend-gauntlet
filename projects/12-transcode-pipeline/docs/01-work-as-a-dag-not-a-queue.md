# Work as a DAG, Not a Queue — From First Principles

> A ground-up guide to why multi-stage work needs a dependency *graph* instead of
> a flat queue, how a scheduler drains one, and why the graph must live in
> Postgres rows rather than in memory. No prior knowledge of workflow engines
> assumed — but this builds directly on project 04's queue, and names exactly
> what the queue *couldn't* do.
>
> This prepares you for **V2 (the job DAG + scheduler)** in [SPEC.md](../SPEC.md).
> You'll write [`dag::expand`](../src/dag.rs) and [`dag::newly_ready`](../src/dag.rs)
> in [src/dag.rs](../src/dag.rs), and the durable twins in
> [src/job.rs](../src/job.rs) ([`JobStore::submit`](../src/job.rs),
> [`add_tasks`](../src/job.rs), [`promote_ready`](../src/job.rs), …) against the
> schema in [migrations/0001_init.sql](../migrations/0001_init.sql). This doc
> teaches the model; the queries and the graph construction are yours.

---

## 0. The one sentence to hold onto

**Model the job as tasks-with-dependency-edges in durable storage, and reduce all
of scheduling to one pure question asked after every completion: *which pending
tasks now have every dependency done?***

---

## 1. This workload has a shape

A 10-minute source, 6-second chunks, a 3-rung ladder. V1's plan yields 100
chunks, so the job is:

```
1 Split  +  (100 chunks × 3 renditions) Transcodes  +  3 Stitches  =  304 tasks
```

And they are *not* interchangeable queue items — they have edges:

```
                     ┌─▶ transcode(c0, 720p) ─┐
                     ├─▶ transcode(c1, 720p) ─┼─▶ stitch(720p)
                     ├─▶      … ×100          ┘
   split ── fan-out ─┤
                     ├─▶ transcode(c0, 480p) ─┐
                     ├─▶      … ×100          ┼─▶ stitch(480p)
                     └─▶      …               ┘        …
```

Two structural facts drive everything in V2:

- **Fan-out:** no transcode may start before the split finishes (the chunks
  don't exist as a plan yet).
- **Fan-in:** `stitch(720p)` may not start until *all 100* of its transcodes are
  done — not 99, not "most". One missing chunk means a hole in the movie.

## 2. Why a flat queue can't say this

Project 04's queue is great at "here are N independent jobs, workers, go". Try to
express this job with it and you hit a wall exactly at the fan-in:

| Attempt | How it breaks |
| --- | --- |
| Enqueue everything at once | A worker picks up `stitch(720p)` while transcodes are still running — there's no way to say "not yet" in a queue |
| Enqueue the stitch last | "Last enqueued" ≠ "runs after the others *finish*" — 8 workers drain the queue out of order |
| Workers check the filesystem: "are all 100 chunk files there?" | Implicit dependencies. Two workers race the check and both stitch; a half-written chunk file (pre-V3) counts as "there"; nothing in the system can *verify* the rule |
| Keep a counter in memory: "100 done → enqueue stitch" | Coordinator restarts, counter gone. The job is stranded half-finished with no record of what ran — the exact failure the SPEC's durability criterion targets |

The concept card's trap is the third row: implicit edges *work in the demo* and
then corrupt output the first time two events race. The fix is to make the edges
**explicit, durable data**.

## 3. Tasks as rows, edges as rows

Look at [migrations/0001_init.sql](../migrations/0001_init.sql) — the whole model
is three tables:

```
jobs        one row per submitted asset (source, ladder, status)
tasks       the DAG nodes: kind, status, attempts, lease_until
task_deps   the DAG edges: (task_id, depends_on) — task_id waits on depends_on
```

A task's lifecycle is the [`Status`](../src/job.rs) enum, and each transition has
exactly one owner:

```
              scheduler                    worker                 worker
  Pending ───(all deps Done)──▶ Ready ──(claimed+leased)──▶ Running ──▶ Done
                                  ▲                            │
                                  └──── reaper (lease expired) ┘└──▶ Failed
                                        or retry after failure       (attempts
                                                                      exhausted)
```

- `Pending` — exists, but an upstream edge is unsatisfied.
- `Ready` — every dependency `Done`; any worker may claim it.
- `Running` — leased to one worker until `lease_until` (V3's territory).
- `Done` — its artifact exists; downstream edges from it are satisfied.
- `Failed` — retries exhausted; the job is failed too.

Because all of this is rows, **restart-safety falls out for free**: kill the
coordinator mid-job, restart it, and the scheduler's next pass reads the same
states and continues. Nothing to rebuild, nothing remembered only in RAM. That's
the SPEC's durability criterion — not a feature you add, but a consequence of
where the truth lives.

## 4. The readiness rule — one pure question

The entire scheduler reduces to:

> A `Pending` task becomes `Ready` exactly when **all** of its dependencies are
> `Done`.

You'll implement it twice, on purpose:

- [`dag::newly_ready(tasks)`](../src/dag.rs) — pure, in-memory, no I/O. Given a
  slice of tasks, return the ids that just became runnable. Because it's pure,
  you can property-test it exhaustively (the SPEC's
  `stitch_waits_for_all_chunks` test lives here).
- [`JobStore::promote_ready`](../src/job.rs) — the SQL twin, run by the wired
  [`schedule_loop`](../src/dag.rs) every tick, flipping rows `Pending → Ready`.

Trace the fan-in through it. Job with 3 chunks, one rendition:

| Event | split | t0 | t1 | t2 | stitch | Why |
| --- | --- | --- | --- | --- | --- | --- |
| submit | Ready | — | — | — | — | Seed task, no deps ([`submit`](../src/job.rs)) |
| split done, expand | Done | Pending | Pending | Pending | Pending | New tasks land `Pending` |
| scheduler pass | Done | Ready | Ready | Ready | Pending | t*'s only dep (split) is Done; stitch waits on t0,t1,t2 |
| t0, t2 finish | Done | Done | Running | Done | Pending | **Still pending** — t1 outstanding: the fan-in holds |
| t1 finishes; pass | Done | Done | Done | Done | Ready | *Now* all deps Done |

Both directions of the rule matter, and the SPEC tests both:

- **Never early** — a stitch promoted at "2 of 3 done" ships a movie with a hole.
- **Never never** — every task whose deps are satisfied *must* eventually
  promote, or the job deadlocks with everything idle. (Deadlock here = an edge
  that can never satisfy, e.g. an edge pointing at a task that was never
  inserted; starvation = a satisfied task the scheduler never notices. Different
  bugs, same symptom: a DAG that stops draining.)

## 5. Dynamic expansion — the graph builds itself

Here's the wrinkle that makes this more interesting than a textbook DAG: **you
can't build the graph at submit time**, because the number of transcode tasks
depends on the chunk count, and the chunk count is only known after the `Split`
task probes the source — on a *worker*, at run time.

So the graph grows in two steps (see the wired `Split` arm in
[`Worker::execute`](../src/worker.rs)):

```
submit:                     split runs (on a worker):
  jobs row                    probe → plan_chunks (V1)
  + 1 Split task (Ready)      → dag::expand(job, split_id, chunks, ladder)   ← you write
                              → store.add_tasks(tasks)                        ← you write
```

[`dag::expand`](../src/dag.rs) defines the DAG's *shape*: one
`Transcode { chunk, rendition }` per (chunk × rendition), each depending on the
split; one `Stitch { rendition }` per rung, depending on **every** transcode of
that rendition and no others (the SPEC's `expand_wires_fan_in` test pins this).

The design question the concept card flags: **what atomicity does
[`add_tasks`](../src/job.rs) need?** If the coordinator dies after inserting 150
of 300 transcode rows and no stitch rows, what does the recovered job look like —
and can the scheduler tell it apart from a healthy one? Sit with that before you
write the insert; the answer decides whether it's one statement, one transaction,
or something cleverer. (That's the decision — this doc won't make it for you.)

## 6. Job status is a projection, not a column

`GET /jobs/{id}` reports the job's status and per-status counts
([`JobView`](../src/job.rs)). The tempting design is a `status` column on `jobs`
that task handlers update. The concept card calls this a **second truth**: the
moment task states and the job column can disagree (a crash between "mark task
done" and "update job"), one of them is lying and nothing detects it.

The robust design *derives* status from task states at read time:

> `done` iff every task is `Done`; `failed` if any task is `Failed`; otherwise
> running.

One truth (task rows), any number of views. The schema does have a `jobs.status`
column — whether you treat it as authoritative, as a cached projection, or ignore
it in favor of pure derivation is a design decision for `docs/12-design.md`.
Whatever you pick, the SPEC's criterion is observable: counts accurate
throughout, `done` only at all-`Done`, `failed` on any `Failed`.

## 7. Join nodes are where stragglers concentrate

Why does the boss fight ("The Straggler") attack the fan-in? Because a join waits
on the **max** of its inputs, not the mean. 100 transcodes averaging 30 s with
one 10-minute straggler → the stitch starts at 10 minutes. One dead worker
holding one chunk (until V3's reaper reclaims it) stalls the entire rendition
behind a single row.

This is also Amdahl's law wearing work clothes. Split and stitch are serial; only
the transcodes parallelize. If ~95% of the wall-clock is parallelizable work:

| Workers | Max speedup (95% parallel) |
| --- | --- |
| 8 | 5.9× |
| 64 | 15.4× |
| 512 | 19.3× |

Throwing 512 workers at it buys 3× more than 64 did. The serial stages — and the
straggler tail, which acts like an extra serial stage — set the ceiling. The
boss's "≥ 6× at 8 workers" target is calibrated right up against this: you can't
hit it with sloppy scheduling *or* with an unrecovered straggler.

And the depth probe worth pre-thinking: if `stitch(480p)` dead-letters
permanently, what *should* happen to the sibling renditions' tasks? Your status
derivation already implies an answer — check whether it's the one you want.

## 8. Mental model summary

| Concept | The one-liner |
| --- | --- |
| DAG vs queue | A queue orders starts; a DAG gates starts on *completions* — fan-in is the thing a queue can't say |
| Node / edge / state | `tasks` rows / `task_deps` rows / a `Status` column — all durable, restart survives free |
| Readiness rule | Pending + all deps Done → Ready; pure in `newly_ready`, SQL in `promote_ready` |
| Dynamic expansion | The split's completion *builds* the rest of the graph; expansion must not be observable half-done |
| Status as projection | Derive job status from task states — one truth, no drift |
| Join nodes | Waits are max(), not mean() — stragglers and deadlocks both live at the fan-in |

## 9. Where you'll build this

- **Pure half:** [src/dag.rs](../src/dag.rs) — `expand` (the shape) and
  `newly_ready` (the rule); [`deps_all_done`](../src/dag.rs) is wired as the
  spec of "runnable". [`schedule_loop`](../src/dag.rs) is wired and just calls
  your store methods.
- **Durable half:** [src/job.rs](../src/job.rs) — `submit`, `get_job`,
  `job_context`, `add_tasks`, `promote_ready` (V2's rows), against
  [migrations/0001_init.sql](../migrations/0001_init.sql).
- **Unlocks (V2 "Done when ALL true"):** correct fan-out/fan-in shape ·
  runnable-exactly-when-deps-done, no deadlock · DAG survives coordinator
  restart with no re-runs · job status derived from tasks with accurate counts ·
  acyclic and terminating.
- **Feeds:** V3's workers claim what your scheduler promotes; the boss fight's
  recovery story is your readiness rule plus V3's reaper, together.
