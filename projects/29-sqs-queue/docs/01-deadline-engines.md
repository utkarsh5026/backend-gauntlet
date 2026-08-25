# Deadline Engines — One Clock for a Million Timers

> Teaches why "check everything on a tick" is the design that quietly caps your scale,
> what structures replace it, and how a timer that fires during a concurrent operation
> stops being a race. No prior knowledge assumed — not of timing wheels, not of
> `asyncio` internals.
>
> Prepares you for **V2** in [`src/sqs_queue/timers.py`](../src/sqs_queue/timers.py)
> (`DeadlineEngine.schedule/cancel/next_due_at/tick/run`). Builds directly on
> [`00-receipt-handles-and-leases.md`](./00-receipt-handles-and-leases.md) — the
> generation counter from that doc is the tool that makes §5 work.

---

## The one sentence to hold onto

**A tick should cost what is *due*, never what is *scheduled* — and a timer that fires
must be able to tell whether the thing it was set for still exists.**

---

## 1. Four features, one problem

Read [`SPEC.md`](../SPEC.md) with an eye for the word "later" and you find four
independent-looking features:

| Feature | The "later" | Owner |
|---|---|---|
| Visibility timeout | an in-flight message becomes available again | V1 |
| `DelaySeconds` | a delayed message becomes available | V6 |
| Retention | an old message is dropped | V6 |
| Dedup window | a dedup id ages out | V5 |

Four different modules, four different meanings — and structurally the same thing every
time: *a deadline, a callback, and a way to cancel or move it.*

Which means you get to make a choice most people make by accident: four loops, or one
engine. The four-loop version is what happens when each feature is built when it is
needed and nobody steps back. Each loop is individually reasonable. Collectively they are
the reason an idle service costs a core.

---

## 2. The naive design, measured

Here is the implementation everyone writes first:

```python
while True:
    now = time.time()
    for message in every_message:          # <-- the problem
        if message.visible_after <= now:
            make_available(message)
    await asyncio.sleep(0.05)
```

It is obviously correct, and its cost is `O(n)` per tick where `n` is **everything
scheduled**, not everything due. So the cost of an *idle* queue grows with how much is
sitting in it.

That's the claim. Here it is measured, on this machine, with plain Python:

```
scan     10,000 timestamps:   0.36 ms   ->  at 20 ticks/sec:  0.7% of one core
scan  1,000,000 timestamps:  37.22 ms   ->  at 20 ticks/sec: 74.4% of one core
```

Read the second row again. **A queue with a million messages and zero traffic burns
three-quarters of a core doing nothing.** No requests are arriving. No consumer is
connected. The service is at 74% CPU because it keeps asking a million messages whether
it is time yet.

And the failure is worse than the number suggests, because it is *invisible in
development*. At 10,000 messages it costs 0.7% and every test passes. The design does not
break; it just has a ceiling nobody wrote down, and you find it in production.

---

## 3. The other naive design, also measured

The second thing people try is to let the runtime do it:

```python
async def visibility_timer(message, seconds):
    await asyncio.sleep(seconds)
    make_available(message)

asyncio.create_task(visibility_timer(m, 30))     # one task per message
```

This *feels* right — it's what `asyncio` is for, and there is no scan anywhere. But you
have traded CPU for memory and scheduler pressure:

```
 10,000 tasks: created in    61 ms, RSS +  14 MB  (~1,454 bytes/task)
100,000 tasks: created in 1,372 ms, RSS + 137 MB  (~1,438 bytes/task)
500,000 tasks: created in 10,227 ms, RSS + 684 MB (~1,434 bytes/task)
```

Half a million in-flight messages — well inside the 120,000-per-queue limit across a
handful of queues — is **684 MB of timer objects and ten seconds of pure task creation**.
The boss fight's target is 400 MB RSS total, so this design loses on the memory criterion
before a single message is stored.

There's a subtler cost too. Every one of those tasks is a scheduler entry. The event loop
walks its timer heap on every iteration, and you have handed it 500,000 entries to
maintain — for messages that will mostly be deleted before their timer ever fires.

| Design | Idle CPU | Memory | Fails on |
|---|---|---|---|
| Scan every tick | grows with `n` | tiny | CPU (74% at 1M, idle) |
| One task per deadline | ~0 | ~1.4 KB × `n` | memory (684 MB at 500K) |
| **One engine** | **grows with `due`** | **~1 entry × `n`** | — that's the target |

---

## 4. The design space: heap vs. wheel

You want two operations cheap — *insert a deadline* and *what's next?* — and a tick that
costs the number due.

**A priority heap** is the honest default. `heapq` is in the standard library, ordering is
exact, and `heap[0]` is the next deadline in constant time. Measured here:

```
n = 1,000,000:  6.1 million pushes/sec   ·   pop ≈ 1.34 µs   ·   log2(1e6) ≈ 20 comparisons
```

**A hierarchical timing wheel** is what Kafka's purgatory, the Linux kernel and most
serious timer subsystems use. Deadlines are bucketed by time slot, so insertion is `O(1)`
— no comparisons at all — at the cost of bounded precision (a slot's width) and a
structure that must be sized for a horizon.

```
   heap                              timing wheel
   ────                              ────────────
   [ 12:00:01 ]                      slot: :01  :02  :03  :04  :05 ...
      /      \                             │    │         │
 [12:00:03] [12:00:07]                    [M1] [M2,M7]   [M4]
      /                               insert = index into a slot: O(1)
 [12:00:09]                           tick   = advance one slot, fire its list
 insert = sift up: O(log n)
```

The comparison the SPEC asks you to *make with numbers*, not pick from a blog post:

| | Heap | Timing wheel |
|---|---|---|
| Insert | `O(log n)` — ~20 comparisons at 1M | `O(1)` |
| Next-due | `O(1)` peek | `O(1)` (advance) |
| Precision | exact | one slot width |
| Cancel | needs a side index, or a tombstone | remove from a slot's list |
| Long horizons (14-day retention) | free | needs hierarchy — hence "hierarchical" |
| Lines of code | ~30 | ~150 |

Note the last row honestly. At the scales in this SPEC a heap may well be enough, and
"I measured both and the heap won at my numbers" is a perfectly good answer — it is the
*measurement* that is graded, not the structure.

---

## 5. The part that isn't the data structure

Here is the trace that decides whether your engine is correct:

```
 t=0    C receives M   -> generation 3, lease until t=30
                          a VISIBILITY deadline is scheduled for t=30
 t=29.999  C: DeleteMessage(H3)     ─┐
 t=30.000  the deadline fires       ─┴─ these are concurrent
```

If both run, you get one of two disasters: the message is deleted *and* made available
(delivered again, forever, to no purpose), or it is made available *and* deleted (a
consumer's successful work vanishes). Locking the whole table around every deadline
"solves" it and makes the tick the slowest thing in the service.

**The tool you already have is the generation counter.** A deadline is scheduled *for a
generation*:

```
schedule(Deadline(due_at=30, kind=VISIBILITY, key="M", generation=3))
```

and when it fires, the handler asks one question: *is M still at generation 3?* If the
delete already ran, M is gone or has moved on — the deadline is about a delivery that
already ended, and it does nothing. If the delete has not run yet, the deadline acts and
bumps the generation, and the delete arriving a microsecond later finds itself
superseded — which is exactly the `SUPERSEDED` outcome from
[doc 00 §5](./00-receipt-handles-and-leases.md).

Either order produces exactly one outcome. That is what "decidable" means here: **you are
not preventing the race, you are making both orderings correct.**

You can see the wiring already in place — `build_state` in
[`main.py`](../src/sqs_queue/main.py) registers
`lambda d, now: inflight.expire_visibility(d.key, d.generation, now)`. The lambda is thin
on purpose: the generation check belongs in the vertical that owns the state, not in the
engine.

---

## 6. Residue — the failure that only shows up on long runs

`ChangeMessageVisibility` moves a deadline. The lazy way to move one is to schedule the
new one and leave the old to fire and be ignored. Harmless once. Now:

```
a consumer extends its lease every 10s for 1 hour  ->  360 obsolete entries
1,000 consumers doing that                         ->  360,000 obsolete entries
```

Each is `O(1)` to skip and none of them is a bug. Together they are memory proportional
to *how long you've been running*, which is the shape of every leak ever written.

The SPEC's criterion: memory after N reschedules is bounded by **live messages**, not by
N. Two defensible ways there — eager removal (costs a lookup index alongside the heap) or
lazy tombstones **with something that bounds them** (a compaction pass, a residue ratio
threshold). Drifting into neither is the failure.

This is why `DeadlineHandler` returns a `bool`: it reports whether the deadline actually
did something. A high proportion of no-op fires is your residue alarm, visible on a
dashboard before it is visible in the RSS graph.

---

## 7. Sleeping correctly

`DeadlineEngine.run` looks like the boring part and contains one trap.

The goal is to sleep until `next_due_at()`, not on a fixed interval — a correct
implementation of everything else with a hardcoded `sleep(0.05)` still wakes 20 times a
second forever, and still fails the idle-CPU criterion.

But: you are asleep until `t=300`, and a message arrives that needs a deadline at `t=1`.

```
 loop:  next due is t=300  ->  await sleep(300)
 t=0.5: schedule(deadline at t=1)      <-- nobody is listening
 t=1:   nothing happens
 ...
 t=300: the loop wakes and fires a deadline that was 299 seconds late
```

That is the same shape as the lost wakeup in
[`02-long-polling.md`](./02-long-polling.md) §4, and it has the same family of fixes: the
sleep must be *interruptible* by a nearer deadline arriving. `TIMER_TICK_SECONDS` in
[`.env.example`](../.env.example) is described as a safety net rather than a polling
interval for exactly this reason — it bounds how long an oversleep can hurt you, and it
is not a substitute for waking properly.

The other bound, `MAX_DEADLINES_PER_TICK`, is a correctness property wearing a
performance parameter's clothes. A tick with a million due deadlines holds the event
loop for its entire duration — every receive, every send, every health check waits on
that function returning. Yield, and finish the rest next time round.

---

## 8. Mental model summary

| Question | Answer |
|---|---|
| How many timer mechanisms should this service have? | One |
| What should a tick cost? | The number of deadlines **due** |
| What's wrong with scanning? | 74% of a core, idle, at 1M messages — and invisible in dev |
| What's wrong with a task per deadline? | ~1.4 KB each; 684 MB at 500K |
| Heap or wheel? | Your call — but measure both, that's the Proof |
| How is the timer/delete race settled? | Schedule for a generation; check it on fire |
| What accumulates if you're careless? | Obsolete entries from rescheduled deadlines |
| How long should the loop sleep? | Until the next deadline — interruptibly |

---

## Where you'll build this

[`src/sqs_queue/timers.py`](../src/sqs_queue/timers.py) — five
`raise NotImplementedError`s: `schedule`, `cancel`, `next_due_at`, `tick`, `run`.
`register` is already done for you, and
[`main.py`](../src/sqs_queue/main.py) already wires visibility and dedup expiry into it.

Done-when criteria this unlocks (V2 in [`SPEC.md`](../SPEC.md)): all four deadline kinds
on one structure, tick cost proportional to due, bounded residue, the concurrent-fire
race, timely expiry under load, and never blocking the loop.

**Build order that works:** heap first, get the visibility expiry correct including the
generation check, *then* measure 10K vs 1M and decide whether a wheel earns its 150 lines.
The measurement is the deliverable either way.

`/hint 29 V2` for a nudge · `/quest 29 V2` for a guided build.
