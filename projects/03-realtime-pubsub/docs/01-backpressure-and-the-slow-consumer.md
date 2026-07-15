# Backpressure & the Slow Consumer — From First Principles

> A ground-up guide to the single hardest fact about real-time systems: **some
> consumers are slower than the producer, always, at scale** — and if you don't
> design *for* that, one slow phone takes down your server. We derive bounded
> queues, head-of-line blocking, and the overflow-policy decision from scratch.
> No prior knowledge of channels, backpressure, or flow control assumed.
>
> Prepares you for **V2** (`src/backpressure.rs`). Anchored to real code:
> [src/backpressure.rs](../src/backpressure.rs), [src/hub.rs](../src/hub.rs),
> [src/routes.rs](../src/routes.rs), [src/main.rs](../src/main.rs).

---

## 0. The one sentence to hold onto

**A bounded queue is where "the producer is faster than the consumer" stops being an
accident that crashes you and becomes a *decision* you made on purpose.**

The whole vertical is: give every connection a bounded outbound mailbox, and decide —
explicitly — what happens when it fills.

---

## 1. The physics: why a slow consumer is inevitable

A WebSocket message doesn't teleport. To reach Bob, bytes go: your server → the OS
send buffer → the network → Bob's OS receive buffer → Bob's app calling `recv()`.
That chain drains only as fast as its **slowest link**, and for Bob that's usually
his network and how often his app reads.

Now the mismatch. Say "room1" is busy — 1,000 messages/second are published. Bob is
on hotel wifi and his client reads 10 messages/second. Every second, 990 messages
that were *produced* for Bob cannot be *delivered* to Bob.

**Those 990 messages have to go somewhere.** That "somewhere" is a buffer, and the
entire vertical is the question: *what are the rules of that buffer?* There are only
three possible answers, and you must pick:

1. Let the buffer grow without limit → it fills RAM → **the server OOM-kills**. Bob's
   bad wifi crashes the whole node for everyone. Unacceptable.
2. Make the producer *wait* for Bob to catch up → **everyone** now moves at Bob's
   speed (we'll see why — head-of-line blocking). Usually unacceptable.
3. Bound the buffer and, when it's full, **shed load** — drop or disconnect — on
   purpose, and *count what you shed*. This is backpressure done right.

This isn't a Bob problem. At 100k connections, *somebody* is always Bob. The scaffold
says it plainly: slow consumers are "a first-class failure mode you design *for*, not
against." It **will** happen; design assuming it.

> **In the wild:** Slack and Discord gateways disconnect laggy clients (policy #3,
> "disconnect"). Financial market-data feeds keep only the latest price (a 4th policy,
> "conflate"). MQTT QoS 0 drops. Kafka exposes consumer *lag* as a first-class metric.
> Everyone who moves messages at scale has faced exactly this and made exactly this
> choice.

---

## 2. What the scaffold already gives you

Open [src/backpressure.rs](../src/backpressure.rs). The substrate is wired; the
*decision* is the `todo!()`. Two halves of one bounded channel:

```rust
pub struct Mailbox { tx: mpsc::Sender<ServerMessage>, policy: OverflowPolicy }
pub type Outbox = mpsc::Receiver<ServerMessage>;

pub fn mailbox(capacity: usize, policy: OverflowPolicy) -> (Mailbox, Outbox) {
    let (tx, rx) = mpsc::channel(capacity.max(1));   // ← BOUNDED. capacity is the bound.
    (Mailbox { tx, policy }, rx)
}
```

- The **`Mailbox`** (sender half) is `Clone`, and the hub clones it into *every topic
  this connection subscribes to* (that's the `Mailbox` value stored in
  [src/hub.rs](../src/hub.rs)'s map). All those clones share **one** underlying
  channel — one queue per *connection*, not per subscription.
- The **`Outbox`** (receiver half) is owned by that connection's writer task in
  [src/routes.rs](../src/routes.rs): `while let Some(msg) = outbox.recv().await { …
  ws_tx.send(…) }`. It pulls from the queue and writes to the socket as fast as TCP
  allows — *that* `recv().await` is the consumer, and it's slow exactly when the
  socket is slow.

The queue is created with `mpsc::channel(capacity)` — **already bounded**. `capacity`
comes from `OUTBOX_CAPACITY` (default **64**, see [src/main.rs](../src/main.rs) and
[.env.example](../.env.example)). Small on purpose: a slow consumer must not let the
server accumulate unbounded memory. The bound is the point; a big buffer just delays
the reckoning (§6).

Your job is one method:

```rust
pub fn deliver(&self, msg: ServerMessage) -> DeliverOutcome { todo!("V2: …") }
```

`deliver` is what the hub calls per subscriber. It must enqueue `msg` **without ever
blocking the publisher**, and when the queue is full, apply `self.policy` and report
back one of:

```rust
pub enum DeliverOutcome { Delivered, Dropped, Disconnect }
```

---

## 3. Failure mode: head-of-line blocking (the trap)

Here's the "obvious" implementation, and why it's a trap:

```rust
// DO NOT DO THIS in deliver():
self.tx.send(msg).await   // awaits until there's room in the queue
```

`send().await` is *polite* backpressure: if Bob's queue is full, it waits for Bob to
drain before returning. Sounds correct! Now trace it against
[src/hub.rs](../src/hub.rs)'s publish loop:

```
publish("room1", msg):        // (after the snapshot, delivering outside the lock)
    deliver_to(Bob)   → tx.send().await  → Bob's queue FULL → *** WAITS ***
    deliver_to(Carol) → not reached yet, we're still awaiting Bob
    deliver_to(Dan)   → not reached yet
```

The publisher is a single task walking the subscriber list. Await Bob's full queue,
and **Carol and Dan don't get the message until Bob catches up.** Bob reads at 10
msg/s, so now Carol and Dan — on fiber, perfectly healthy — *also* receive at 10
msg/s. One slow client dragged the entire room down to his speed. That's
**head-of-line blocking**: the slowest consumer sets the pace for everyone behind
him in line.

```
   Fan-out with an AWAITING mailbox (WRONG):

   publisher ──▶ [Bob's full queue] 🐢  ← publisher stuck here
                       ✗ Carol never reached
                       ✗ Dan never reached
                 everyone moves at Bob's speed
```

The scaffold is emphatic about this:

> *NEVER call the awaiting `self.tx.send(msg).await` here: that reintroduces
> head-of-line blocking and lets one slow client stall every publisher.*

The escape is `try_send`, which **never waits** — it returns immediately with either
`Ok` (there was room) or `Err(Full)` (no room). On `Full`, *you* decide, and the
publisher moves on to Carol instantly:

```
   Fan-out with try_send (RIGHT):

   publisher ──▶ [Bob's full queue]  → Full! apply policy, keep going ──▶ Carol ✅ ──▶ Dan ✅
                 Bob's slowness costs Bob a message, nobody else waits
```

This is the same lesson as V1's "never hold the lock across a send", one layer down:
**never let one consumer's slowness propagate into a shared path.** V1 kept the *map*
free; V2 keeps the *publisher* free.

---

## 4. The policy decision — the actual vertical

Once you've decided "the queue is full and I will NOT wait", there's a real product
choice about *which* message loses. The scaffold enumerates it as an enum parsed from
`OVERFLOW_POLICY`:

```rust
pub enum OverflowPolicy { DropNewest, DropOldest, Disconnect }
```

| Policy | On a full queue… | What the slow client experiences | Right for… |
|--------|------------------|----------------------------------|------------|
| **DropNewest** | refuse the incoming msg, keep the backlog | sees an *old* continuous prefix, misses the latest | a log/chat where the *start* of a burst matters and you'll reconcile later |
| **DropOldest** | evict the oldest queued msg, enqueue the new one | sees the *latest*, misses the middle | live feeds where **freshness beats completeness** — scores, telemetry, "current state" |
| **Disconnect** | tear the connection down | gets kicked, must reconnect (and refetch state) | gateways that refuse to babysit stragglers — Slack/Discord do this |

There is **no globally correct answer** — that's the whole point, and why it's a
runtime switch (`OVERFLOW_POLICY`, default `drop_oldest` in
[.env.example](../.env.example)) rather than hard-coded. Make the choice *visible* and
*motivated*:

- A **live auction / price ticker**: `DropOldest`. A bid from 20 seconds ago is
  worthless; the newest price is everything. Show the latest, skip the stale.
- A **collaborative text editor**: neither drop is safe — losing an edit corrupts the
  document. Here you might `Disconnect` (force a full resync) rather than silently
  drop, because *silent loss is a data-integrity bug*, not a UX blemish.
- A **chat backlog**: `DropNewest` plus a "you missed N messages, click to load" —
  keep the conversation contiguous and let the client backfill.

> **Depth probe (worth chewing on):** a *4th* policy is **conflation** — keep only the
> latest value *per key* (e.g. per stock symbol). For a market-data feed that beats
> all three, because the consumer only ever wants "the current value", never the
> history. Notice conflation needs a different structure than a plain FIFO — which is
> your first hint about §5.

---

## 5. Why `DropOldest` is the interesting one (a structural hint, not a solution)

`try_send` gives you `DropNewest` and `Disconnect` almost for free: on `Err(Full)`,
either drop the message on the floor or report `Disconnect`. Done.

`DropOldest` is different, and the scaffold flags exactly why:

> *a plain mpsc can't pop from the send side — you may need a different structure
> (e.g. an `Arc<Mutex<VecDeque>>` + a `Notify`, or a ring buffer). This is the
> interesting decision.*

Sit with *why*. A `tokio::mpsc` is a one-directional pipe: the **sender** can push,
the **receiver** can pull. To "drop the oldest", you'd need to remove from the
*front* of the queue — the receiver's end — but you're standing at the *sender's*
end holding a `tx`. The channel's shape simply doesn't let the sender reach the front.

So `DropOldest` forces an honest architectural question: is `mpsc` even the right
substrate for the policy you want to ship? That's the design decision — not "write
this code", but "does my policy fit my primitive, and if not, what primitive fits?"
This doc deliberately stops here: picking and building that structure is the vertical.
For graduated nudges, that's what `/hint` is for.

---

## 6. The trap: "just make the buffer bigger"

The tempting non-fix: bump `OUTBOX_CAPACITY` from 64 to 100,000 and "the drops go
away." Watch what that actually buys you:

```
 capacity 64:      queue fills in ~64 msgs → you drop → Bob sees FRESH data, minus gaps
 capacity 100000:  queue fills in ~100k msgs → Bob's queue holds 100 SECONDS of backlog
                    → Bob is now watching messages from 100s ago (STALE)
                    → 100k conns × 100k msgs each = your RAM is gone anyway (OOM, later)
```

A bigger buffer converts a **fast, visible, bounded** failure (a drop counter ticking
up, which you can graph and alert on) into a **slow, invisible, unbounded** one
(memory creep + every slow client served *stale* data). You didn't remove the
problem; you hid it and made delivery worse. The bound is a feature. Keep it small and
make the drops observable — which is the last piece.

---

## 7. Observable loss: the drop counter is part of the design

The scaffold's last line on this:

> *Messages shed by the policy are **counted** (a metric) — the loss is observable,
> never silent.*

This is not observability garnish; it's what separates a *policy* from a *bug*. A
`DeliverOutcome::Dropped` you count and expose as a metric is a deliberate, graphable,
alertable decision: "we shed 4,000 msgs/s to slow clients during the spike, here's the
graph." A drop you *don't* count is indistinguishable from a lost-message bug — and
you'll spend a debugging night proving it wasn't one. Every `Dropped` (and every
`Disconnect`) increments a counter. That counter is, per the SPEC, *"the whole point of
V2"* — it's the number your bench chart is built around.

---

## 8. Mental model — looks-like vs actually-is

| It looks like… | It actually is… |
|----------------|-----------------|
| "Just send the message to each subscriber." | Try to enqueue without blocking; a full queue is a decision point, not an error. |
| "`send().await` is proper backpressure." | Head-of-line blocking — it drags every subscriber down to the slowest one. |
| "A bigger buffer fixes the drops." | It trades a fast visible failure for slow memory growth + stale delivery. |
| "Dropping messages is a bug." | Dropping *on a declared, counted policy* is correct load-shedding. Silent drops are the bug. |
| "One policy is best." | Chat, live scores, and editors each want a different one — hence the runtime switch. |
| "`DropOldest` is just `DropNewest` backwards." | It needs a structure a plain `mpsc` can't give you — that's the real V2 design call. |

---

## 9. Where you'll build this

One `todo!()`, but it's the crux of the vertical:

- [`Mailbox::deliver(&self, msg) -> DeliverOutcome`](../src/backpressure.rs) —
  non-blocking enqueue via `try_send`; on `Full`, branch on `self.policy`
  (DropNewest / DropOldest / Disconnect); on `Closed`, the reader is gone →
  `Disconnect`. Return the outcome so the hub can count drops and reap wedged
  connections.

Everything around it is wired: [src/main.rs](../src/main.rs) reads
`OUTBOX_CAPACITY`/`OVERFLOW_POLICY` and builds the mailbox per connection in
[src/routes.rs](../src/routes.rs); [src/hub.rs](../src/hub.rs)'s `publish` calls
`deliver` and inspects the outcome. If you choose `DropOldest`, you'll also reshape
the substrate (§5).

**This doc unlocks these V2 "Done when ALL true" boxes:**

- [ ] Each connection has a **bounded** outbound mailbox — no unbounded queue on the
  publish path. *(§2, §6)*
- [ ] An explicit overflow **policy switch** (drop-newest / drop-oldest /
  disconnect) exists and is honored. *(§4)*
- [ ] **Invariant under a stalled reader:** server memory stays bounded **and**
  delivery to other subscribers is unaffected. *(§3 — the `try_send` payoff)*
- [ ] Messages shed by the policy are **counted** — loss is observable, never silent.
  *(§7)*

**The proof** (from the SPEC): a test that deliberately stalls one reader and asserts
the **drop counter climbs** while **memory and other-subscriber delivery stay flat.**
That flat line for the healthy subscribers, next to the climbing drop counter for the
stalled one, is the entire V2 payoff — and it's the money shot of the project's bench.

---

*Previous: [the hub that calls `deliver`](00-the-fan-out-hub-and-lock-discipline.md)
(V1). Next: [who's actually in the room?](02-presence-as-soft-state.md) — presence,
and why detecting *absence* is harder than everything here (V3).*
