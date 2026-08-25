# Long Polling — Ten Thousand Consumers Waiting, and Nothing Burning

> Teaches why polling forces every consumer into a bad trade, what "parking" a request
> actually means, and the two races that every park/notify implementation has to answer.
> No prior knowledge assumed — not of condition variables, not of `asyncio` primitives.
>
> Prepares you for **V3** in [`src/sqs_queue/polling.py`](../src/sqs_queue/polling.py)
> (`WaitSet.wait_for_messages`, `.notify`, `.release_all`, `.waiter_count`). The wake
> call lives on the send path in [`routes.py`](../src/sqs_queue/routes.py).

---

## The one sentence to hold onto

**Long polling doesn't remove the cost of an idle queue — it moves it from the consumer
to you, and then it's your job to make it nearly free.**

---

## 1. The trade polling forces

A consumer wants a message the instant one exists. It has one tool: ask.

```
loop:
    messages = ReceiveMessage(queue)
    if not messages:
        sleep(interval)
```

Now pick `interval`. Every choice is bad, and the arithmetic makes it concrete — here for
10,000 consumers, which is one modest fleet:

| Interval | Requests/sec at the broker | Worst-case latency added |
|---|---|---|
| 100 ms | **100,000** — essentially all empty | 100 ms |
| 1 s | **10,000** — essentially all empty | 1 s |
| 5 s | **2,000** — essentially all empty | 5 s |

There is no good row. Small intervals mean your service spends its entire capacity
answering "no". Large intervals mean every message waits, including the urgent ones, on a
queue that had work available the whole time.

The tell that the *design* is wrong rather than the tuning: the consumer is being asked
to guess how often something it can't observe is going to happen.

**Long polling deletes the question.** The receive says "wait up to 20 seconds", and the
broker answers the moment a message exists:

```
10,000 consumers long-polling at 20s -> 500 requests/sec, and near-zero latency
```

From 100,000 requests/sec to 500, with *better* latency than the 100 ms row. That is not
a tuning win, it is a different shape.

---

## 2. What "waiting" has to mean

The number above is only real if waiting is free. The version that is not free:

```python
deadline = now + wait_time
while now < deadline:                # busy-wait
    messages = queue.take(max)
    if messages:
        return messages
```

That consumes a core per waiter. You moved the polling loop from the client into your own
process and called it long polling.

What you want is for a waiting request to consume **nothing but memory**: no CPU, no
scheduler attention, no timer per iteration. In `asyncio` terms, the coroutine must be
suspended on something that only the arrival of a message (or its deadline) resumes. The
request's connection stays open; the coroutine is not on the ready queue at all.

This is the same idea as blocking on a condition variable in a threaded server, and it
appears under many names in systems you already use:

| System | The call | The wait |
|---|---|---|
| SQS | `ReceiveMessage(WaitTimeSeconds=20)` | up to 20s |
| Redis | `BLPOP key 0` | until a push |
| Postgres | `LISTEN channel` | until a `NOTIFY` (project 04's V4) |
| Kafka | `fetch.min.bytes` + `fetch.max.wait.ms` | until enough bytes or the wait elapses |

The boss fight's idle scenario measures exactly this: **10,000 parked waiters, under 3%
CPU, for five minutes.** Anything in your implementation that touches every waiter on a
timer — rather than only when something happens — shows up as a flat few percent that
never goes away.

---

## 3. The thundering herd, in its purest form

One message arrives. You have 10,000 waiters parked. How many do you wake?

The easy implementation wakes all of them, because that is what a broadcast primitive
does and it is obviously *correct*: whoever gets there first wins, everyone else finds
nothing and parks again.

Count the work:

```
1 message arrives
  -> 10,000 coroutines resume
  -> 10,000 queue checks
  ->      1 succeeds
  ->  9,999 park again
```

**`O(waiters)` work per message, to deliver one message.** And it peaks at precisely the
worst moment: after a quiet period, when the maximum number of consumers are parked and
traffic is just resuming. Your queue's busiest CPU moment is the one where it had almost
nothing to do.

The SPEC's criterion is that one message wakes a **bounded** number of waiters — the boss
fight says ≤ 2. That is why `WaitSet.notify` in
[`polling.py`](../src/sqs_queue/polling.py) takes an `available` count and *returns how
many it woke*: the input tells you how many wakes could possibly be useful, and the
return value is the number the bench measures.

Which raises a question you have to answer deliberately: once you're waking *some*
waiters, **which** ones? Oldest first is fair and costs bookkeeping. Newest first has
better cache behaviour and can starve the waiter that has been there longest. "Whatever
the primitive happens to do" is not a decision — and it will be the answer to a support
ticket one day.

---

## 4. The lost wakeup

This is the race, and it is the reason condition variables have the API they do.

A receive must do two things: check whether messages are available, and park if not.
Between those two steps is a window:

```
   consumer                                    producer
   ────────                                    ────────
   check queue -> empty
        │
        │  <──────────── SendMessage arrives here
        │                notify() runs: zero waiters registered, wakes nobody
        ▼
   park (registers as a waiter)
        │
        │  ... sleeps for the full 20 seconds ...
        ▼
   returns empty
```

The message was there the entire time. The consumer returns empty after 20 seconds. No
error, no log line, no dropped message — just a latency spike that appears under load and
disappears the moment you attach a debugger, because the timing changes.

The general fix is the same everywhere: **the check and the park must not have a gap that
a notify can fall into.** How you achieve that in `asyncio` is your design decision —
registering before checking, holding something across both, or re-checking after
registering are all real approaches with different costs. What you cannot do is hope.

> The SPEC asks for a **deliberately interleaved** test, and that phrasing is load
> bearing. A test that sends and receives concurrently and asserts it worked will pass
> against a broken implementation roughly nine times out of ten. You need a test that
> forces the send to land *inside* the window.

Note that this is structurally identical to the oversleeping deadline engine in
[`01-deadline-engines.md`](./01-deadline-engines.md) §7. Same bug, different clothes: a
sleeper that missed the news.

---

## 5. Leaving cleanly

`wait_for_messages` is a coroutine that gets **cancelled** — routinely, not
exceptionally. A client hits Ctrl-C. A load balancer times out. The server shuts down.
All of those raise `CancelledError` inside a parked waiter.

If cancellation leaves the waiter registered in the wait set, then:

```
10,000 aborted long polls
  -> 10,000 orphaned waiter entries
  -> notify() now walks 10,000 dead entries to find a live one
  -> and the memory never comes back
```

The SPEC's criterion is measurable and blunt: park N, abort all N,
`WaitSet.waiter_count()` returns to baseline. That method exists in
[`polling.py`](../src/sqs_queue/polling.py) *for the test* as much as for the gauge.

Shutdown deserves its own thought. `release_all` is documented to return every waiter an
**empty response** rather than dropping the connection, and the difference is entirely
about the client's experience:

| What the client sees | How it reacts |
|---|---|
| Empty receive | A normal, expected answer it already handles. Loops. |
| Connection reset | An error. Logged, alerted on, someone gets paged. |

Same restart, two very different nights. This is why the shutdown ordering in
[`main.py`](../src/sqs_queue/main.py) releases waiters *before* stopping the deadline
loop, and deliberately leaves in-flight leases alone — those expire on their own, which
is the whole point of V1.

---

## 6. Returning early, and the batch that isn't a promise

A consumer asks for 10 messages with a 20-second wait. One message arrives at t=0.2.

Return it. Don't sit for 19.8 more seconds hoping for nine friends.

`MaxNumberOfMessages` is a **ceiling, not a target** — batching is an optimization the
broker offers when it can, not a quantity the consumer is owed. Getting this backwards
turns a low-traffic queue into a 20-second-latency queue, and the symptom (everything is
slow when it's quiet, fast when it's busy) reads as nonsense until you find it.

Worth putting on the dashboard, per the observability checklist: a **histogram of
messages-per-receive**. If it's pinned at 1 under heavy load, your batching isn't
working, and you're paying a round trip per message.

---

## 7. Mental model summary

| Question | Answer |
|---|---|
| What does long polling save? | 100,000 req/s → 500 req/s at 10K consumers, *and* lower latency |
| What does a parked waiter cost? | Memory only — no CPU, no scheduler slot |
| How many waiters wake per message? | Bounded (boss fight: ≤ 2), never all of them |
| Which waiters? | Your decision — fair vs. cache-friendly. Write it down |
| What is the lost wakeup? | A message arriving between "checked, empty" and "parked" |
| How do you test for it? | Deliberate interleaving — the natural test passes on broken code |
| What must cancellation restore? | Waiter count, timers, slots — all to baseline |
| What does shutdown owe a waiter? | An empty response, not a reset |
| Is `MaxNumberOfMessages` a promise? | No. A ceiling |

---

## Where you'll build this

[`src/sqs_queue/polling.py`](../src/sqs_queue/polling.py) — four
`raise NotImplementedError`s: `waiter_count`, `wait_for_messages`, `notify`,
`release_all`. The call sites are already marked: `_receive_message` and `_send_message`
in [`routes.py`](../src/sqs_queue/routes.py), and `release_all` in the lifespan teardown
in [`main.py`](../src/sqs_queue/main.py).

Done-when criteria this unlocks (V3 in [`SPEC.md`](../SPEC.md)): wait semantics at 0 and
>0, parked-not-spinning at 10K waiters, bounded wake fanout, no lost wakeup,
early return, `MaxNumberOfMessages` honoured, clean cleanup on disconnect, and the 20s cap.

**Measure the idle CPU at 10,000 waiters before you tune anything.** The first number is
usually a surprise, and it tells you immediately whether you built a park or a spin.

`/hint 29 V3` · `/quest 29 V3`
