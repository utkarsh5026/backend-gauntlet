# V4 — Wakeups Without Busy-Polling: `LISTEN` / `NOTIFY`

> Teaches how to get millisecond job pickup *and* a silent idle database — by layering
> a best-effort push signal over a durable pull. No prior knowledge assumed.
>
> Prepares you for **V4** in [`src/scheduler.rs`](../src/scheduler.rs)
> (`wait_for_work`, `notify_ready`) and the idle-wait in [`src/worker.rs`](../src/worker.rs).
> Concept overview: [`00-how-job-queues-work.md`](./00-how-job-queues-work.md) §10.
> This doc goes deeper on *why the poll must survive* and where NOTIFY gets lost.

---

## The one sentence to hold onto

**Poll the durable table for correctness, and add a `NOTIFY` ping purely to wake idle
workers fast — the notification is an optimization, never the source of truth.**

---

## The problem before the solution

By V4 the queue *works*. But look at what an **idle** worker does today in
[`worker.rs`](../src/worker.rs): when a claim returns nothing, it sleeps
`poll_interval` and tries again. That fixed sleep forces a dilemma with no good setting:

```
poll every 50ms  → an idle pool fires ~20 empty SELECTs/sec/worker all night
                   (× WORKER_CONCURRENCY = a flood of pointless queries)
poll every 5s    → a job enqueued at 12:00:00.01 isn't picked up until 12:00:05
                   (up to 5s of latency added to every job)
```

| Poll interval | Idle DB load | Pickup latency |
|---|---|---|
| tiny (50ms) | high (wasteful) | low (good) |
| large (5s) | low (good) | high (bad) |

Tuning the knob only **slides the pain** between load and latency. No value removes it.
That's the tradeoff V4 dissolves — not by tuning, but by adding a second mechanism.

---

## The idea: durable pull + ephemeral push

Postgres has a tiny built-in pub/sub: `NOTIFY channel` sends a message,
`LISTEN channel` receives it on any connection that subscribed. Layer it over the poll:

```
PRODUCER                         WORKER (idle)
enqueue job ──INSERT──▶ jobs
     │
     └── NOTIFY jobs_default ─────▶ LISTEN jobs_default wakes INSTANTLY
                                         │
                                    run the normal claim (V1)  → job picked up in ms
```

- On enqueue (and when a delayed/retried job comes due), fire `NOTIFY` on the queue's
  channel — a tiny "hey, there's work" ping. You write this in
  [`notify_ready`](../src/scheduler.rs) (the SPEC also suggests emitting it right from
  `enqueue`).
- An idle worker, instead of sleeping blindly, holds a `LISTEN` connection and waits on
  it. The instant a `NOTIFY` arrives it wakes and runs the *same* V1 claim. You write
  this in [`wait_for_work`](../src/scheduler.rs), built on sqlx's `PgListener`.

Result: **millisecond pickup** on a busy queue *and* **zero empty queries** on an idle
one. Best of both ends of the table above.

---

## The rule that makes it correct: the poll survives

Here's the part people get wrong. `NOTIFY` is **fire-and-forget and not durable**. It
is delivered only to connections listening *at that instant*, and it's gone forever
after. It gets lost in at least three concrete ways:

| How a NOTIFY is lost | Scenario |
|---|---|
| **No one listening yet** | worker is mid-restart / reconnecting when the enqueue fires |
| **Connection dropped** | the `LISTEN` connection blipped between ping and receipt |
| **Nothing to notify** | a job's `run_at` is in the *future* — no enqueue happens when it becomes due |

If correctness *depended* on the notification, any one of these would strand a job
**forever**. So the design rule is absolute:

> **The durable poll stays the source of truth. `NOTIFY` is only a latency
> optimization.**

Concretely, [`wait_for_work`](../src/scheduler.rs) must wait on *either* the
notification *or* a `poll_fallback` timeout — whichever comes first — and re-attempt a
claim regardless. Miss a ping? The slow fallback poll catches the job a moment later.
The notify makes the common case fast; the poll makes *every* case correct. Generalize
the shape and you'll see it everywhere: **durable pull for truth, ephemeral push for
latency** (SQS long-polling, Redis `BLPOP`, condition variables over a locked queue).

---

## Worked example: three paths, one design

| Event | What wakes the worker | Latency |
|---|---|---|
| Job enqueued now, worker listening | the `NOTIFY` | ~milliseconds |
| Job enqueued now, worker was reconnecting (NOTIFY missed) | the next fallback poll | ≤ `poll_fallback` |
| Job enqueued with `delay_secs=60` (future `run_at`) | the fallback poll after 60s (no NOTIFY fires when it comes *due*) | ≤ `poll_fallback` after due |

That third row is the one that proves the poll isn't optional: a delayed job becomes
eligible with *no INSERT event* to hang a `NOTIFY` on, so only the poll (which re-checks
`run_at <= now()`) will ever pick it up. (Handling "NOTIFY when a delayed job comes due"
is a possible refinement — but the poll is the floor that must always work.)

---

## The decisions V4 asks *you* to make

- **Channel naming.** Both `LISTEN` and `NOTIFY` must agree on the channel string
  (e.g. `jobs_<queue>`). Derive it in *one* place so they can't drift apart.
- **Who fires `NOTIFY`.** Enqueue, certainly. Retries coming due? Delayed jobs coming
  due? Decide which events signal, and accept the poll covers the rest.
- **Fallback interval.** Long enough to keep idle load low, short enough that a lost
  notification isn't a long stall. What's your acceptable worst-case pickup latency?
- **Emit NOTIFY from the app INSERT, or an `AFTER INSERT` trigger?** (See the TODO(V4)
  note in [the migration](../migrations/0001_init.sql).) Tradeoffs either way.

---

## Depth probes (you own V4 when you can answer)

- 100 idle workers all `LISTEN`; one job arrives; all 100 wake and race the claim. Is
  that a problem? What bounds the damage? (Hint: it's V1's `SKIP LOCKED` again.)
- Why can tuning the poll interval never eliminate the latency-vs-load tradeoff on its
  own, when adding `NOTIFY` can?
- Compare this with Redis `BLPOP` and SQS long-polling — what does each runtime give or
  lose versus "durable table + best-effort ping"?

---

## Where you'll build this

| Piece | Location |
|---|---|
| the idle wait (LISTEN + poll fallback) | [`wait_for_work`](../src/scheduler.rs) `todo!("V4: LISTEN…")` |
| the wakeup signal | [`notify_ready`](../src/scheduler.rs) `todo!("V4: NOTIFY…")` |
| swap the worker's fixed sleep for `wait_for_work` | idle branch in [`worker::run`](../src/worker.rs) |
| emit NOTIFY on enqueue | [`Queue::enqueue`](../src/queue.rs) (V1 + V4) |

**This doc unlocks (V4 "Done when ALL true"):** enqueue/due-job fires `NOTIFY`, idle
workers `LISTEN` and wake on it; a slow poll fallback remains so a missed `NOTIFY` or
future `run_at` is never stranded; millisecond pickup with near-zero idle load (shown
poll-vs-NOTIFY in the bench); the durable poll stays the source of truth.

**Ready to build?** `/hint 04 V4` for nudges, or `/quest 04 V4` for the guided session
(including the dropped-notification test proving the poll still catches the job). Save
the poll-vs-NOTIFY pickup-latency comparison to
[`04-benchmarks.md`](./04-benchmarks.md).
</content>
