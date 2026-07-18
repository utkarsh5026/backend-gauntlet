<!-- status:
state: active            # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 03 — Real-time Pub/Sub + Presence

> A broadcast server looks trivial: "a client subscribes to a topic, and every
> message published to that topic is sent to every subscriber." The trap is what
> *real-time* and *at scale* do to that sentence. WebSockets are long-lived and
> stateful, so the server now holds thousands of open sockets at once. Some of
> those clients read slowly — and a single slow reader must not be allowed to
> stall the fast ones or balloon the server's memory (**backpressure**). And the
> moment you run more than one server instance, a message published on the socket
> connected to node A has to reach a subscriber whose socket lives on node B —
> which the in-process map on node A cannot see. It's a tiny data structure
> wrapped in a hard concurrency, flow-control, and distributed-fan-out problem.
> That's the rung.

## What it does (the easy part)
- A WebSocket endpoint (`GET /ws`) that upgrades and keeps the connection open.
- A small JSON protocol over that socket: a client can `subscribe` / `unsubscribe`
  to named topics and `publish` a payload to a topic.
- Every message published to a topic is fanned out to all current subscribers of
  that topic — including subscribers connected to *other* nodes (V4).
- A **presence** view per topic: who is currently in the room, updated as people
  join and leave.
- A `GET /healthz` for liveness.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it. The criteria describe *what the system must do*, never *how*;
> figuring out the how is the point. A box only flips to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. The fan-out hub — *the in-process pub/sub core, from scratch*
In `src/hub.rs`, build the registry that maps **topic → set of subscribers** and
broadcasts a message to all of them. This is the thing you'd normally reach for a
library (`tokio::sync::broadcast`, an actor framework) to get:
- `subscribe(topic, conn)` / `unsubscribe(topic, conn)` and a `publish(topic, msg)`
  that delivers to every current subscriber and reports how many it reached.
- `disconnect(conn)` that removes a connection from *every* topic it joined — a
  dropped socket must leave nothing behind (no leaked entries, no empty topics
  growing forever).
- The whole thing is shared across thousands of concurrent tasks, so think hard
  about the locking: one big `Mutex` is simple but serialises every publish;
  per-topic locks or a read-mostly `RwLock` scale better. Above all — **don't hold
  the lock while you send** to a slow subscriber (that's how one slow client
  freezes the whole hub).

**Done when ALL true:**
- [x] `subscribe` / `unsubscribe` / `publish` work, and `publish` reports how many current subscribers it reached.
- [x] `disconnect(conn)` removes the connection from **every** topic it joined — no leaked entries, no empty topics growing forever.
- [x] The hub **never holds its lock while sending** to a subscriber, so one slow client can't freeze publishes to everyone else.
- [x] Concurrent subscribe/publish/disconnect from many tasks leaves **no dangling subscriber** and never delivers to a closed socket.

**Proof:** concurrency tests for clean teardown + no-leak, and a test proving a stalled receiver doesn't block delivery to others.

*Concept to internalize:* publish/subscribe as decoupling (publishers don't know
subscribers), and why fan-out makes the lock-holding discipline — not the map —
the hard part.

### V2. Backpressure — *the slow-consumer problem*
A WebSocket sender can only push bytes as fast as that client's TCP socket
drains. If a subscriber reads slowly while messages keep arriving, something has
to give. In `src/backpressure.rs`, give each connection a **bounded outbound
mailbox** and decide what happens when it fills:
- Pushing onto a *bounded* queue means a publisher can find it full. Awaiting a
  full queue (`send().await`) applies real backpressure — but now a single slow
  client blocks the publisher and, transitively, every other subscriber. That's
  **head-of-line blocking**, and it's usually the wrong default for fan-out.
- The alternatives are all *lossy or disconnecting*: `try_send` and on overflow
  **drop the newest**, **drop the oldest**, or **disconnect the slow client**.
  Each is a real product decision (a chat backlog vs. a live price feed want
  different answers). Implement the policy switch and make it explicit.
- Whatever you choose, the invariant is the same: **one slow consumer must not
  grow memory without bound, nor slow down delivery to everyone else.**

**Done when ALL true:**
- [x] Each connection has a **bounded** outbound mailbox — there is no unbounded queue anywhere on the publish path.
- [x] An explicit overflow **policy switch** exists (drop-newest / drop-oldest / disconnect-slow) and is honored.
- [x] **Invariant under a deliberately stalled reader:** server memory stays bounded **and** delivery to other subscribers is unaffected.
- [x] Messages shed by the policy are **counted** (a metric) — the loss is observable, never silent.

**Proof:** a test that stalls one reader and asserts the drop counter climbs while memory and other-subscriber delivery stay flat (the V2 payoff in the bench).

*Concept to internalize:* bounded queues as the unit of backpressure, head-of-line
blocking, and "slow consumer" as a first-class failure mode you design *for*, not
against.

### V3. Presence — *soft state with a lifecycle*
In `src/presence.rs`, track who is currently in each topic and surface it. The
subtlety is that presence is **soft state**: it's only ever an approximation that
must converge as connections come and go.
- Maintain a per-topic membership set keyed by connection (and a client-supplied
  identity). `join` on subscribe, `leave` on unsubscribe, and — easy to forget —
  remove on *every* disconnect path, including an abrupt socket drop.
- A clean leave is the easy case; a client whose laptop lid closes never sends
  one. Real presence leans on a **heartbeat + TTL**: an entry that isn't refreshed
  within a window is presumed gone and swept. Implement the in-process version;
  reason about the heartbeat (and wire it if you go for the stretch).
- Publish presence changes as their own server messages so rooms see joins/leaves
  live — and think about the thundering-herd cost of doing that in a 10k-member
  room.

**Done when ALL true:**
- [x] Per-topic membership tracks join on subscribe, leave on unsubscribe, and removal on **every** disconnect path (including an abrupt socket drop).
- [x] Absence is handled via **heartbeat + TTL**: an entry not refreshed within the window is presumed gone and swept (in-process version).
- [ ] Presence changes are published as their own server messages, so rooms see joins/leaves live.
- [x] An abrupt drop (no clean leave) **still leaves the room** — no ghost members linger.

**Proof:** a test that drops a socket without a clean leave and asserts the member disappears within the TTL; design-doc note on the heartbeat.

*Concept to internalize:* presence as eventually-consistent soft state, and why
"detecting absence" (TTL/heartbeat) is fundamentally harder than detecting a
clean leave.

### V4. Multi-node fan-out — *one logical topic across many processes*
A single node's hub only knows about *its own* sockets. Run two nodes behind a
load balancer and a publish on node A never reaches a subscriber on node B. In
`src/cluster.rs`, bridge the local hub to a **cross-node bus** (Redis pub/sub):
- On a local `publish`, also publish the message to a Redis channel for the topic
  so other nodes can deliver it to *their* local subscribers.
- Run a background task that **subscribes to Redis** and, for each message that
  arrives, injects it into the local hub — but **only** delivers to local sockets;
  it must **not** re-publish back to Redis, or you build an infinite echo.
- Stamp every message with this node's `NODE_ID` so a node can recognise and drop
  its own messages coming back around (loop prevention / de-dup).
- Subscribe to a Redis channel lazily — only for topics this node actually has
  subscribers for — and unsubscribe when the last local subscriber leaves, so a
  node isn't firehosed with traffic for rooms nobody here is in.

**Done when ALL true:**
- [ ] A publish on **node A reaches a subscriber on node B** (verified in a two-node run).
- [ ] The Redis-bridge task delivers only to **local** sockets and **never re-publishes** back to Redis — no echo loop.
- [ ] Every message carries this node's `NODE_ID`, and a node **drops its own** messages coming back around (de-dup).
- [ ] Redis channel subscriptions are **lazy** — only for topics with local subscribers — and are dropped when the last local subscriber leaves.

**Proof:** a two-node integration test (publish on A, receive on B) plus a loop-prevention assertion; the multi-node setup recorded in `docs/03-benchmarks.md`.

*Concept to internalize:* the split between the **local hub** (owns the sockets)
and the **cross-node bus** (carries messages between nodes), and why naive
bridging creates echo loops you must explicitly break.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] HTTP upgrade to **WebSocket** done correctly (`GET /ws`, 101 Switching
  Protocols via the axum upgrade extractor).
- [ ] A versioned, typed JSON message protocol (`subscribe`/`unsubscribe`/
  `publish` in; `message`/`presence`/`error` out). Reject malformed frames
  with an `error` message, don't drop the connection silently.
- [ ] Respond to **ping/pong** and use it as the liveness/heartbeat signal; close
  idle or unresponsive sockets with a proper close frame + code.
- [ ] Graceful shutdown: stop accepting, then close live sockets with a close
  frame rather than yanking the TCP connection.

### State & caching
- [ ] The hub is the in-memory source of truth for local subscriptions (V1).
- [ ] Redis is the **bus**, not the store — it carries messages between nodes (V4);
  no per-topic state needs to be durable for V1–V3.
- [ ] Bounded per-connection buffers (V2); no unbounded queue anywhere on the
  publish path.

### Security / abuse protection
- [ ] Authenticate the upgrade (an API key / token on the `GET /ws` request)
  before accepting the socket — don't let anonymous clients open sockets.
- [ ] Validate and **cap** everything a client controls: max message size, max
  topics per connection, max subscribers, publish rate per connection.
- [ ] Topic-name validation (length, charset) so a client can't wedge the map
  with absurd keys.
- [ ] Never trust `identity` from the client for anything but display; never log
  tokens.

### Observability
- [ ] Gauges: open connections, total subscriptions, topics, presence per room.
- [ ] Counters: messages published vs. delivered, **messages dropped by the
  backpressure policy** (this number is the whole point of V2), slow-client
  disconnects.
- [ ] A `tracing` span per connection with a connection id; structured fields on
  subscribe/publish (topic, fan-out size, delivery latency).

---

## Cross-cutting scale skills
- Flow control: a defined, *tested* answer to "what happens when a consumer is
  slower than the producer" — proven by a test with a deliberately stalled reader.
- Concurrency correctness: concurrent subscribe/publish/disconnect never leaves a
  dangling subscriber or leaks an empty topic; never delivers to a closed socket.
- Connection lifecycle hygiene: every exit path (clean close, error, abrupt drop,
  server shutdown) removes the connection from the hub *and* presence.
- Horizontal scalability: the same client experience whether it's 1 node or N.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its **Proof** artifact).
2. A `bench/` load test (e.g. a Rust or `k6`/Tsung client that opens **thousands**
   of concurrent WebSocket subscribers) reporting: fan-out **throughput**
   (messages delivered/sec) and end-to-end **delivery latency** p50/p99 under a
   sustained publish rate; the numbers with **one deliberately slow subscriber**
   present (proving it doesn't drag the others — that's the V2 payoff); and a
   **two-node** run proving a publish on node A reaches a subscriber on node B.
   Numbers in `docs/03-benchmarks.md`.
3. A short `docs/03-design.md`: your hub locking strategy and why; the backpressure
   policy you shipped and the product reasoning; how presence handles abrupt
   disconnects; and the cross-node bus design including how you break echo loops.
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p realtime-pubsub` are
   green; no `todo!()` remains on a checked path.

## Suggested order of attack
1. Get a socket talking: accept the WS upgrade and echo frames back. Then add the
   JSON protocol and reply to a `publish` with a hard-coded `message`.
2. Build the in-process hub (V1): real `subscribe`/`publish`/`disconnect`, single
   node, one fast client and one publisher.
3. Add bounded mailboxes and a backpressure policy (V2); prove it with a test that
   stalls one reader and watches the drop counter, not memory, climb.
4. Add presence (V3): join/leave/members and broadcast presence changes; make sure
   an abrupt drop still leaves the room.
5. Bridge to Redis (V4): publish to the bus, subscribe a background task, inject
   into the local hub, and break the echo loop with `NODE_ID`. Run two instances.
6. Auth the upgrade, add the caps/limits and observability, then benchmark and
   document.

## Run the dependencies
```bash
docker compose up -d        # redis (only needed for V4 / CLUSTER=true)
cp .env.example .env        # then fill in values
cargo run -p realtime-pubsub

# in another shell — connect with any WS client, e.g. websocat:
#   websocat ws://localhost:8080/ws
# then send a frame:
#   {"type":"subscribe","topic":"room1"}
#   {"type":"publish","topic":"room1","payload":{"hello":"world"}}

# multi-node test (V4): run two with CLUSTER=true on different ports,
# subscribe on one, publish on the other, watch it arrive.
```
