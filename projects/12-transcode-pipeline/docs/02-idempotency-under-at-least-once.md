# Idempotency Under At-Least-Once — From First Principles

> A ground-up guide to the central bargain of distributed work: you cannot have
> "runs exactly once", but you can have "running it twice changes nothing" — and
> the two disciplines (deterministic output + atomic commit) that buy it. No
> prior distributed-systems background assumed; project 04's lease/claim is
> recapped, not re-taught.
>
> This prepares you for **V3 (parallel transcode workers)** in
> [SPEC.md](../SPEC.md). You'll write
> [`Worker::transcode_chunk`](../src/worker.rs) in
> [src/worker.rs](../src/worker.rs) and the V3 store methods in
> [src/job.rs](../src/job.rs) ([`claim_ready`](../src/job.rs),
> [`complete`](../src/job.rs), [`fail`](../src/job.rs),
> [`reclaim_expired`](../src/job.rs)). This doc teaches why the safety rules
> exist; the ffmpeg invocation and the SQL are yours.

---

## 0. The one sentence to hold onto

**At-least-once delivery plus idempotent execution equals effectively-exactly-once
results — and idempotent execution needs *both* halves: deterministic bytes
(same input → same output) and atomic commit (temp file → rename), because each
half covers the failure the other can't.**

---

## 1. Why every task will sometimes run twice

The lease machinery you built in project 04 comes back verbatim (the SPEC says
so): a worker claims a `Ready` task with `FOR UPDATE SKIP LOCKED`, stamping
`lease_until = now() + lease` ([`claim_ready`](../src/job.rs)); a reaper flips
expired `Running` tasks back to `Ready`
([`reclaim_expired`](../src/job.rs), called by the wired
[`schedule_loop`](../src/dag.rs)).

That machinery *guarantees* recovery: a dead worker's chunk is re-claimable, the
fan-in stitch isn't stranded, the boss's straggler is survivable. But read the
guarantee from the other side: **the system cannot tell "worker died" from
"worker is slow"**. A lease expiring means *either*. So re-delivery is not an
edge case — it's the mechanism. Two scenarios you must survive:

**Scenario A — crash and retry:**

```
worker 1: claim chunk 17 ── encoding… ── 💀 kill -9 (half-written file on disk)
                                              │ lease expires
reaper:                                       └─▶ task back to Ready
worker 2:                                          claim chunk 17 ── encode again
```

Worker 2 starts with worker 1's *partial output already sitting on disk*.

**Scenario B — slow worker, two runners at once:**

```
worker 1: claim chunk 17 ── encoding… (slow box, not dead) ──────── finishes! writes.
                               │ lease expires anyway
worker 2:                      └─ claim chunk 17 ── encoding… ── finishes! writes.
```

Nobody died. Both workers are alive, **running the same task simultaneously**,
and both will try to produce the artifact. The lease bounds *how long* a task
can be stuck; it cannot prevent overlap.

## 2. What goes wrong if the task isn't idempotent

Suppose `transcode_chunk` writes straight to the final path
`…/chunks/17.mp4` and does nothing else clever:

| Naive behavior | Scenario | Corruption |
| --- | --- | --- |
| Write directly to the final path | A | Worker 1's half-file *exists at the final path*. Anything that treats existence as done — memoization, a stitch that starts the moment the last task acks — reads a truncated chunk. The movie has a glitch you can't cheaply detect |
| Two writers, same path | B | Interleaved writes → a file neither worker produced. Exit codes say success twice |
| Append-style or "resume" logic | A | The retry doubles part of the chunk — the SPEC's "exactly once output silently doubles a chunk" |
| Nondeterministic encode | B | Two *complete, valid, different* files race; which bytes win depends on timing. Byte-level determinism tests (`transcode_is_deterministic`) can never pass |

The concept card's trap is the first row, and it's worth restating: the window
where a file exists-but-is-partial is exactly the window where V2's scheduler
may promote the stitch (the task acked, the fan-in satisfied). "We'll overwrite
on retry" doesn't help — the reader downstream already saw the partial.

## 3. The two disciplines, and why you need both

### Discipline 1 — deterministic output

Same chunk in → **same bytes out**, every run, any worker. Then scenario B is
harmless by construction: both workers produce identical artifacts, so it cannot
matter whose write lands.

What breaks determinism in practice (the card's "what would break" list):

- **Wall-clock metadata** — encoders love stamping creation times and
  writing-library tags into the container. Two runs, two timestamps, two hashes.
- **Multithreaded rate control** — some encoder paths make thread-count-dependent
  decisions, so the same input encodes differently on an 8-core and a 4-core box.
- **Anything random** — seeds, unordered maps feeding option strings, temp names
  leaking into metadata.

The `todo!()`'s comment says it plainly: fixed encoder settings, no
wall-clock/random metadata. Which ffmpeg flags achieve that is part of the
vertical — the *test* is already specified (`transcode_is_deterministic`:
transcode a chunk twice, compare bytes).

### Discipline 2 — atomic commit (write temp, then rename)

Project 06's discipline, applied to artifacts: write the encode to a temp path,
and only when it's complete, `rename` it to the final path. POSIX `rename(2)`
within a filesystem is atomic — an observer sees the old state or the new state,
never a half-state. Consequences:

- A crash mid-encode leaves garbage at a *temp* path, never at
  `…/chunks/17.mp4`. Partial files are **invisible** to everything downstream.
- "The final path exists" becomes a **truthful completion marker** — which is
  the entire basis for the next section.

### Why one without the other fails

| Have | Missing | The hole |
| --- | --- | --- |
| Determinism only | Atomic commit | Scenario A still exposes a half-written file at the final path — identical bytes don't help if only half of them arrived |
| Atomic commit only | Determinism | Scenario B has two *complete but different* files racing the rename; byte-identity tests fail; the "content-stable artifacts" the SPEC's caching checklist demands is gone |

Two failure modes, two disciplines, no overlap. That's why the SPEC's criterion
names both in one breath.

## 4. Memoization falls out for free

Once "exists at final path" is a sound marker, the horizontal checklist's cache
item costs one `if`: a task whose output already exists skips the encode
entirely. This is what makes at-least-once *cheap*, not just safe:

- Worker died *after* rename but *before* [`store.complete`](../src/job.rs)
  acked? The retry hits the marker and completes in milliseconds.
- The boss fight's "no re-transcode waste" criterion — recovery re-runs **only**
  the dead worker's in-flight chunk — is proven by exactly this plus the
  attempt/cache-hit counters you'll add.

Note the direction of the logic: memoization is *sound because of* atomic
commit. Add the skip-check without the rename discipline and you've built the
corruption machine from section 2, with a cache in front of it.

This is Bazel's model, verbatim: hermetic (deterministic) actions + a
content-addressed cache = "never run the same work twice", safely. Spark task
retries and Temporal activities (project 21) assume the same contract.

## 5. Backpressure: the pool size is the throttle

The other half of V3 is *parallelism done safely at the machine level*. After
the split expands, hundreds of transcodes go `Ready` at once. Each ffmpeg
process happily uses several cores and hundreds of MB. Spawn one per ready task
and a 16-core box is running 300 encoders:

- every encode slower than serial (cache thrash, scheduler churn),
- memory exhaustion → the OOM killer starts executing your workers,
- which expires leases, which re-queues tasks, which spawns *more* ffmpeg — a
  feedback loop the SPEC calls fork-bombing.

The fix is structural, and you already have it: **workers only claim one task at
a time**. N workers ⇒ at most N concurrent encodes, no matter how deep the ready
queue gets. The pool size *is* the backpressure valve — sized to the box, not to
the backlog. The wired loop in [`Worker::run`](../src/worker.rs) already
enforces claim-run-settle-repeat; your job is to not defeat it (e.g. by spawning
unawaited encodes).

This also frames the bench you'll run: wall-clock vs worker count
(`docs/12-benchmarks.md`). Expect the curve to bend — the boss demands ≥ 6× at 8
workers (75% efficiency), and the missing 2× is the depth probe: serial split +
stitch (Amdahl), claim contention on the DB, straggler tails, and disk/IO
bandwidth shared by every worker.

## 6. Retries, backoff, dead-letter

Not every failure is a crash. A corrupt source region fails the encode
*deterministically* — retrying forever burns the pool on a chunk that will never
succeed. So [`fail`](../src/job.rs) settles a failed attempt by policy:

```
attempts < max_attempts  →  back to Ready (with backoff — don't hammer)
attempts ≥ max_attempts  →  Failed        (dead-letter; the job fails cleanly)
```

`max_attempts` lives in [`PipelineConfig`](../src/job.rs); `attempts` is bumped
at claim time. The design decisions left to you: where backoff lives (the lease?
a delay column? the scheduler?), and what "cleanly" means for the failed job's
sibling tasks — your V2 status projection already constrains the answer.

## 7. Mental model summary

| Concept | The one-liner |
| --- | --- |
| At-least-once | Leases can't distinguish dead from slow → re-runs are the mechanism, not an edge case |
| The two duplicate scenarios | Crash-then-retry (partial file waiting) and slow-plus-lease-expiry (two live runners) |
| Determinism | Same chunk → same bytes on any worker → concurrent runs can't disagree |
| Atomic commit | temp → `rename`: partial files never visible; existence = truthful done-marker |
| Both, not either | Determinism covers the race; atomicity covers the crash — no overlap |
| Memoization | Free consequence of the marker; what makes recovery re-run *only* the lost chunk |
| Backpressure | N workers = N encodes, however deep the backlog — the pool size is the throttle |
| Retry vs dead-letter | Bounded attempts + backoff; a permanently-bad chunk fails the job, not the pool |

## 8. Where you'll build this

- **Module:** [src/worker.rs](../src/worker.rs) — the `todo!()` in
  [`transcode_chunk`](../src/worker.rs) (cut one chunk's `[start, end)`, encode
  one rung, deterministically, temp→rename into
  [`chunk_dir`](../src/job.rs)`/<index>.mp4`), plus
  [src/job.rs](../src/job.rs)'s `claim_ready` / `complete` / `fail` /
  `reclaim_expired`.
- **Unlocks (V3 "Done when ALL true"):** exactly one chunk per task · ~N-way
  parallelism measured at N workers · byte-identical re-runs, atomic commit · a
  killed worker loses nothing (`killed_worker_is_recovered`) · bounded retries
  then dead-letter.
- **Feeds:** V4 stitches the artifacts your commit discipline guarantees are
  whole; the 🐉 boss fight is this doc end-to-end — speedup, kill-recovery, and
  no-waste all at once.
