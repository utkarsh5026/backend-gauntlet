# V2 — Leases, the Visibility Timeout, and the Idempotency Bill

> Teaches why "claiming a job" must be a time-boxed lease, not permanent ownership —
> and the price it charges: at-least-once delivery. No prior knowledge assumed.
>
> Prepares you for **V2** in [`src/lease.rs`](../src/lease.rs) (`reap_expired`,
> `extend_lease`) and the lease stamp inside [`Queue::claim`](../src/queue.rs).
> Concept overview: [`00-how-job-queues-work.md`](./00-how-job-queues-work.md) §8.
> This doc goes deeper on the failure model and the tradeoffs you must decide.

---

## The one sentence to hold onto

**A claim isn't "I own this job" — it's "I'm allowed to try until a deadline"; if the
deadline passes without a success, the job goes back in the pool for someone else.**

---

## The problem before the solution

V1 gave you a correct atomic claim: a worker flips a job to `running` and commits. Now
ask the uncomfortable question: **what if the worker dies while the job is `running`?**

```
worker-2 claims job 42 → state='running'  (committed)
worker-2 starts the work ...
worker-2 gets OOM-killed / loses power / its pod is evicted
```

The row is now stuck:

| The job is… | …because |
|---|---|
| **not `ready`** | the claim already flipped it to `running` |
| **not `done`** | the worker died before acking |
| **not owned by anyone** | worker-2 is gone |
| **invisible forever** | the claim only looks at `ready` rows |

Job 42 is a zombie — silently stuck, never retried, never completed. In a system where
a claim means *permanent* ownership, **every crash leaks a job**. That's unacceptable
for a durable queue.

---

## The idea: a claim is a lease

Borrow the pattern from Amazon SQS, where it's literally called the **visibility
timeout**. When a worker claims a job it stamps three things (columns already in
[the migration](../migrations/0001_init.sql)):

- `locked_by` — which worker holds it (`"worker-2"`)
- `locked_at` — when it was claimed
- `locked_until` — `now() + visibility_timeout` — the **deadline**

The job is invisible to other workers *only until* `locked_until`. Two outcomes:

```
                    claim: locked_until = 12:00:30
                              │
        ┌─────────────────────┴─────────────────────┐
   worker acks before 12:00:30              worker dies, never acks
        │                                            │
   state='done'  ✅                         12:00:30 passes → lease EXPIRED
                                                     │
                                       the REAPER flips it back to 'ready'
                                                     │
                                         another worker claims & retries
```

The **reaper** is the second half of V2. It's a periodic sweep: *"any job that's
`running` but whose `locked_until` is in the past → back to `ready`, clear the lock."*
The loop is already wired for you in [`reap_loop`](../src/lease.rs) (runs every
`REAPER_INTERVAL_SECS`); you write the one-statement sweep in
[`reap_expired`](../src/lease.rs). That sweep is the *entire reason* a crashed worker
doesn't lose its job.

---

## The bill: at-least-once, and why exactly-once is a myth

The lease buys **at-least-once delivery** — every job runs *at least* once, surviving
any crash. But you have to stare directly at the word "least." Trace this:

```
worker finishes the real work  (the email is SENT)  at 12:00:29.8
   ... then the process dies at 12:00:29.9, BEFORE the ack commits ...
      lease expires at 12:00:30 → reaper requeues → another worker runs it
         → the email is SENT AGAIN
```

The work succeeded, but the *acknowledgement* was lost, so the system can't tell the
difference between "done" and "never started" and safely retries. **There is no free
exactly-once.** The ack itself is a message that can be lost, and you cannot make "do
the work" and "record that you did it" a single atomic action across the worker *and*
the outside world (the email provider).

The honest industry answer isn't "prevent the duplicate" — it's **make running the job
twice harmless**, i.e. **idempotent handlers**. Two ways to get there:

| Strategy | How it dedups | Example |
|---|---|---|
| **Natural idempotency** | the operation is inherently repeat-safe | `UPDATE users SET verified=true`, an UPSERT, "set balance to X" |
| **Idempotency key** | record "job 42 already done" in a dedup table; skip on repeat | a `processed_jobs(job_id)` table checked before the side effect |

Every "exactly-once" product you'll ever see (Kafka EOS included) is at-least-once +
dedup underneath. In this project the real work lives in your
[`handle`](../src/worker.rs) function — making *that* idempotent is your job, and the
SPEC requires you to document the strategy in [`04-design.md`](./04-design.md).

---

## The decision V2 asks *you* to make: lease length

`visibility_timeout` (scaffold: `VISIBILITY_TIMEOUT_SECS`, default 30s) is a genuine
dial with pain at both ends:

| Lease too **short** | Lease too **long** |
|---|---|
| a slow-but-*alive* job passes its deadline; the reaper hands it to a second worker; now it runs twice for no reason (and maybe concurrently) | a genuinely *crashed* worker's job sits stuck until the long deadline passes — slow recovery, growing lag |

There's no universally right value — it depends on how long your jobs actually take and
how fast you need crash recovery. A rule of thumb: `visibility_timeout` comfortably
exceeds your p99 job duration, with headroom. Two related sub-decisions:

- **Reaper interval vs. lease length.** The reaper only runs every
  `REAPER_INTERVAL_SECS`, so actual recovery time ≈ lease length + up to one reaper
  interval. Tune them together.
- **Does a reaped job count as a used attempt?** A job reaped after a crash could be
  treated as "attempt failed" (counts against the V3 retry budget) or "never really
  ran" (doesn't). This interacts directly with [retries](./03-retries-backoff-dlq.md) —
  decide deliberately and note it.

### The stretch: heartbeats for long jobs

If a job legitimately runs longer than any sane lease, a fixed timeout can't win —
too short reaps it alive, too long delays every real crash. The fix is a **heartbeat**:
the running handler periodically calls [`extend_lease`](../src/lease.rs) ("still alive,
push my deadline out"), so a slow-but-healthy worker is never reaped out from under
itself, while a *dead* one stops heartbeating and is reclaimed promptly. This is the
`extend_lease` stretch `todo!()`.

---

## Depth probes (you own V2 when you can answer)

- The reaper reclaims a job whose worker is actually still alive, just slow. Now two
  workers run it concurrently. Which of your guarantees survives — and what makes that
  safe? (Answer lives in "idempotent handlers," not in "prevent it.")
- Why does a *non-zero* "leases reaped" metric deserve an alert *threshold* rather than
  zero-tolerance? What are the two very different things a rising reap rate can mean?
- Why can't you make "send the email" and "ack the job" a single atomic transaction?

---

## Where you'll build this

| Piece | Location |
|---|---|
| stamp `locked_by`/`locked_until` on claim | inside [`Queue::claim`](../src/queue.rs) (V1 + V2) |
| the reaper sweep | [`reap_expired`](../src/lease.rs) `todo!("V2: requeue…")` |
| the reaper loop (already wired) | [`reap_loop`](../src/lease.rs) |
| heartbeat / lease extension (stretch) | [`extend_lease`](../src/lease.rs) `todo!()` |
| idempotent work | your [`handle`](../src/worker.rs) |

**This doc unlocks (V2 "Done when ALL true"):** claim stamps the lease and a success
acks to `done`; the reaper returns expired `running` jobs to `ready`; a worker killed
mid-job has its job picked up by another (chaos-tested); the at-least-once/idempotency
reasoning written in the design doc.

**Ready to build?** `/hint 04 V2` for nudges, or `/quest 04 V2` for the guided,
tests-first session (including the chaos test that kills a worker mid-job).
</content>
