# Multi-node Fan-out & the Echo Loop — From First Principles

> A ground-up guide to what breaks the instant you run *two* servers instead of
> one: a message published on node A must reach a subscriber whose socket lives on
> node B — which A's in-memory map has never heard of. We derive the **hub/bus
> split**, why a naive bridge creates an **infinite echo loop**, and how a single
> `NODE_ID` breaks it. No prior knowledge of Redis, load balancers, or distributed
> messaging assumed.
>
> Prepares you for **V4** (`src/cluster.rs`). Anchored to real code:
> [src/cluster.rs](../src/cluster.rs), [src/hub.rs](../src/hub.rs),
> [src/main.rs](../src/main.rs), [src/routes.rs](../src/routes.rs),
> [docker-compose.yml](../docker-compose.yml).

---

## 0. The one sentence to hold onto

**Split the system in two: a *local hub* that owns the actual sockets, and a
*cross-node bus* that carries messages between nodes — and stamp every bus message
with the node that sent it, so a node can recognize and drop its own echoes.**

Miss that stamp and a 2-node cluster becomes an infinite message loop.

---

## 1. Why one node was a lie (a comforting one)

V1–V3 all assumed a single process. That process holds *every* socket in one
in-memory `Hub`, so "deliver to all subscribers of room1" is just a loop over a local
map. Beautiful — and it stops working the moment you need a second server.

You need a second server for the usual reasons: one box can hold only so many open
sockets (tens of thousands before file descriptors, memory, and CPU bite), and you
want to survive a node dying. So you put N nodes behind a load balancer:

```
                         ┌─────────────┐
        Alice ──────────▶│    load     │──────▶ node A   (Alice's socket lives here)
        Bob   ──────────▶│  balancer   │──────▶ node B   (Bob's socket lives here)
                         └─────────────┘
        Both are "in room1". But room1 on A = {Alice}, room1 on B = {Bob}.
        Neither node's map knows the other's clients exist.
```

Now Alice publishes to room1:

```
   Alice → node A → A.hub.publish("room1") → delivers to {Alice} ✅
                                           → Bob? A has never heard of Bob. ✗
```

**Bob never gets the message.** Pub/sub silently, invisibly broke — no error, no
crash, just... Bob sitting in the same room hearing nothing. And "just use sticky
sessions so a user always hits the same node" doesn't save you: stickiness pins a
*user* to a node, but Alice and Bob are *different users* who can still land on
different nodes while sharing a room. The room is inherently split across processes.

---

## 2. The fix, derived: separate "who holds the socket" from "how nodes talk"

The local hub is *good at* one thing: it owns real sockets and does the final write to
each client. What it *can't* do is see another process's sockets. So don't make it.
Instead, give the nodes a shared channel to shout into — a **bus** — and let each node
keep doing local delivery from its own hub.

Two distinct components, two distinct jobs:

| Component | Owns | Job | In this project |
|-----------|------|-----|-----------------|
| **Local hub** | the actual WebSocket sockets on *this* process | final delivery to local clients | [src/hub.rs](../src/hub.rs) (V1) — unchanged |
| **Cross-node bus** | nothing durable — it's a pipe | carry each publish to *every other node* | Redis pub/sub, via [src/cluster.rs](../src/cluster.rs) (V4) |

The flow, from the module doc comment in [src/cluster.rs](../src/cluster.rs):

```
   client→A  ──publish──▶  A.hub (local sockets)               ← Alice gets it locally
                           └──▶ Redis channel ──▶ B.run() ──▶ B.hub (local sockets)  ← Bob gets it via the bus
```

Every local publish does **two** things now: deliver locally (as before) *and* put the
message on the bus so other nodes can deliver it to *their* locals. You can see both
already wired in `dispatch` in [src/routes.rs](../src/routes.rs):

```rust
state.hub.publish(&topic, msg);                 // (1) local subscribers — V1
if let Some(cluster) = &state.cluster {
    cluster.publish(&topic, &payload).await;    // (2) onto the bus — V4
}
```

> **Redis here is a *bus*, not a *store*.** It carries messages and forgets them —
> nothing is persisted, and that's *correct* for live chat/presence. This is a
> deliberate design stance, not a shortcut (more in §6). It's the same shape as
> Phoenix PubSub's Redis adapter, socket.io-redis, and Ably/Pusher internals — and it
> returns almost verbatim in project 16's cross-pod chat.

---

## 3. The trap that defines the vertical: the echo loop

Here's the naive receive side, and it's a beautiful disaster. Each node runs a
background task that subscribes to the bus and re-publishes whatever arrives:

```
   on bus message m for topic t:
       publish(t, m)      // ← re-broadcasts locally AND back onto the bus
```

Trace one message with two nodes:

```
  t0  Alice publishes to A → A delivers locally → A puts m on the bus
  t1  B's bridge receives m from the bus → B calls publish(m)
        → B delivers to Bob locally ✅ ... but publish ALSO puts m back on the bus 🔁
  t2  A's bridge receives m from the bus → A calls publish(m)
        → A delivers to Alice AGAIN (duplicate!) ... and puts m on the bus 🔁
  t3  B receives it again → delivers to Bob AGAIN → back on the bus 🔁
  t4  A receives it again → ... 🔁🔁🔁  forever, growing, both nodes pegged at 100% CPU
```

**One publish becomes an infinite storm.** Alice and Bob each receive the message an
unbounded number of times, and both nodes melt. This is *the* classic distributed
pub/sub bug, and the scaffold is built entirely around preventing it. Two rules, both
in the [src/cluster.rs](../src/cluster.rs) module doc:

> 1. The receive side injects into the **local hub only** — it must NOT re-publish to
>    Redis, or every message loops forever.
> 2. Each message is stamped with this node's id, so a node recognises and drops its
>    own messages coming back around.

---

## 4. Breaking the loop with `NODE_ID`

The two rules together snap the loop. Look at what actually travels on the bus — the
scaffold defines it:

```rust
pub struct BusEnvelope {
    pub origin: String,        // ← NODE_ID of the node that first published this
    pub topic: String,
    pub payload: serde_json::Value,
}
```

Every node has a stable `NODE_ID` (from [.env.example](../.env.example) /
[src/main.rs](../src/main.rs) — `node-a`, `node-b`, …). The discipline:

- **On publish** ([`ClusterBridge::publish`](../src/cluster.rs)): wrap the payload in
  a `BusEnvelope { origin: self.node_id, … }` and put it on the bus. You're signing
  the message: *"A sent this."*
- **On receive** ([`ClusterBridge::run`](../src/cluster.rs)): decode the envelope and
  do two things the scaffold spells out:
  - **Drop your own echo:** `if envelope.origin == self.node_id { skip }` — you
    already delivered this locally when you first published it; the copy coming back
    around the bus is a duplicate.
  - **Inject locally only:** deliver to *this* node's hub
    (`self.hub.publish(&topic, ServerMessage::Message { … })`) and **do not** call
    `self.publish(...)` again. That's rule #1 — never re-emit to the bus.

Re-trace with the stamp in place:

```
  t0  Alice→A: A delivers locally; A puts m{origin:A} on the bus
  t1  B receives m{origin:A}: origin≠B → inject into B.hub only → Bob gets it ✅
        B does NOT re-publish to the bus. Chain stops on B's side.
  t2  A receives m{origin:A}: origin==A → DROP (my own echo). No re-delivery to Alice.
        Chain stops on A's side.
  ── done. Exactly one delivery each. No loop. ──
```

The `origin` check is doing double duty: it breaks the echo *and* it de-dups (a node
never re-delivers a message it originated). Two rules, one field.

---

## 5. Lazy subscription: don't drink from the firehose

A working bridge still has a scaling flaw. The simple receive side does `PSUBSCRIBE
pubsub:*` — subscribe to **every** topic on the bus. On a big cluster that means node
A receives *every message for every room in the entire system*, decodes each envelope,
and then... throws almost all of them away because it has no local subscribers for
those rooms. That's enormous wasted network + CPU.

The scalable version, per the SPEC:

> Subscribe to a Redis channel **lazily** — only for topics this node actually has
> subscribers for — and unsubscribe when the last local subscriber leaves.

So the bus subscription set tracks the hub's local interest: `SUBSCRIBE pubsub:room1`
when room1 gets its *first* local subscriber, `UNSUBSCRIBE pubsub:room1` when its
*last* local subscriber leaves. A node then only hears traffic for rooms it's actually
serving. The scaffold notes `PSUBSCRIBE pubsub:*` is *"the simple start"* and lazy
per-topic is *"the scalable version"* — a legitimate staging: get correctness first
(§4), then earn scale.

```
   PSUBSCRIBE pubsub:*  (simple)          Lazy per-topic (scalable)
   ───────────────────────────           ─────────────────────────────
   node A hears: EVERY room's traffic     node A hears: only rooms with a local sub
   decodes millions of envelopes/s        decodes only what it can use
   discards ~all of them                  ~zero waste
```

---

## 6. The delivery guarantee you actually ship (and why it's fine)

Bridging over Redis pub/sub gives you a specific, *weak* guarantee, and understanding
it is the senior-engineer part:

- **At-most-once, best-effort.** Redis pub/sub is fire-and-forget: if a node isn't
  subscribed at the instant a message is published, it misses it. No replay, no acks.
- **No ordering across nodes.** Two messages from different origins can interleave
  differently on different nodes.

Now the trap, straight from `CONCEPTS.md`:

> **Trap:** thinking the bus needs to be reliable. For live chat/presence, a lossy
> fire-and-forget bus is the *correct* cost/benefit.

Why is losing messages *acceptable* here? Because the product is **live**. If Redis
blips for 10 seconds, users on a single node keep talking to each other fine (local
hub still works); users split across nodes miss the cross-node messages for those 10
seconds — the same as a brief network glitch, which chat clients already tolerate and
recover from on reconnect. Paying for a durable, ordered, replayable bus (Kafka, NATS
JetStream — see project 05) would buy correctness the product doesn't need, at real
latency and complexity cost. **Knowing when best-effort is enough is the judgment
being taught.** (Depth probe: what would a durable bus change, and for which features
would it suddenly be worth it — payments? audit logs?)

---

## 7. Mental model — looks-like vs actually-is

| It looks like… | It actually is… |
|----------------|-----------------|
| "Add a second node and scale for free." | Pub/sub silently breaks — cross-node subscribers get nothing until you add a bus. |
| "Sticky sessions fix multi-node." | They pin a user to a node; two users in one room still split across nodes. |
| "The bridge just re-publishes bus messages." | That's an infinite echo loop — inject locally only, never back onto the bus. |
| "Redis is where the messages live." | Redis is a *bus*, not a store — fire-and-forget, nothing durable, and that's correct. |
| "Subscribe to everything, filter later." | On a big cluster that's a firehose of useless traffic — subscribe lazily per live topic. |
| "A real system needs a reliable bus." | For live chat, best-effort is the right cost/benefit; durability would be over-engineering. |

---

## 8. Where you'll build this

Two `todo!()`s in [src/cluster.rs](../src/cluster.rs):

- [`ClusterBridge::publish(topic, payload)`](../src/cluster.rs) — wrap in a
  `BusEnvelope { origin: self.node_id, … }`, JSON-encode, publish to `pubsub:{topic}`.
  Fire-and-forget: log and swallow bus errors so a Redis hiccup degrades to
  single-node delivery, never fails the client's publish. (Use a cached async
  connection, not one per publish, on this hot path.)
- [`ClusterBridge::run(self)`](../src/cluster.rs) — the receive loop: subscribe,
  decode each `BusEnvelope`, **drop if `origin == self.node_id`**, otherwise inject
  into the **local hub only**. Never call `self.publish` from here.

Already wired for you: [src/main.rs](../src/main.rs) constructs the bridge only when
`CLUSTER=true` and spawns `run()` at startup; `dispatch` in
[src/routes.rs](../src/routes.rs) already calls `cluster.publish` after the local one.
Single-node mode (`CLUSTER=false`, the default) never touches Redis, so V1–V3 are
fully testable without it. To exercise V4, `docker compose up -d` (Redis, host port
`6303` → [docker-compose.yml](../docker-compose.yml)) and run two instances with
`CLUSTER=true`, different `NODE_ID`s and `PORT`s.

**This doc unlocks these V4 "Done when ALL true" boxes:**

- [ ] A publish on **node A reaches a subscriber on node B** (two-node run). *(§2)*
- [ ] The bridge delivers only to **local** sockets and **never re-publishes** to
  Redis — no echo loop. *(§3, §4)*
- [ ] Every message carries this node's `NODE_ID`, and a node **drops its own**
  coming back around. *(§4)*
- [ ] Redis subscriptions are **lazy** — only for topics with local subscribers — and
  dropped when the last local subscriber leaves. *(§5)*

**The proof** (SPEC): a two-node integration test (publish on A, receive on B) plus a
loop-prevention assertion, with the multi-node setup recorded in `docs/03-benchmarks.md`.

---

*Previous: [presence](02-presence-as-soft-state.md) (V3) — which faces the same
cross-node problem (whose presence set is authoritative?). See also the WebSocket
fundamentals woven through all four verticals:
[the protocol layer](04-websocket-fundamentals.md).*
