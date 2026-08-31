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
In `src/realtime_pubsub/hub.py`, build the registry that maps **topic → set of
subscribers** and broadcasts a message to all of them. Python's stdlib has no
broadcast primitive at all — `asyncio.Queue` is point-to-point, and the first
consumer to wake takes the item — so this is the thing you'd normally reach for a
library (an actor framework, `broadcaster`, Redis pub/sub) to get:
- `subscribe(topic, conn)` / `unsubscribe(topic, conn)` and a `publish(topic, msg)`
  that delivers to every current subscriber and reports how many it reached.
- `disconnect(conn)` that removes a connection from *every* topic it joined — a
  dropped socket must leave nothing behind (no leaked entries, no empty topics
  growing forever).
- The whole thing is shared across thousands of concurrent tasks, so think hard
  about what "shared" means here. On one event loop a plain `def` that never
  awaits is already atomic — no lock can be contended if no other task can run —
  so the interesting question is not which lock, it's **where the await points
  are**. Above all: **never `await` while fanning out** to a slow subscriber
  (that's the Python spelling of holding the lock while you send, and it's how
  one slow client freezes the whole hub).

**Done when ALL true:**
- [x] `subscribe` / `unsubscribe` / `publish` work, and `publish` reports how many current subscribers it reached.
- [x] `disconnect(conn)` removes the connection from **every** topic it joined — no leaked entries, no empty topics growing forever.
- [x] The fan-out path **never awaits mid-delivery**, so one slow client can't freeze publishes to everyone else.
- [x] Concurrent subscribe/publish/disconnect from many tasks leaves **no dangling subscriber** and never delivers to a closed socket.

**Proof:** `tests/test_hub.py` — concurrency tests for clean teardown + no-leak, and a test proving a stalled receiver doesn't block delivery to others.

*Concept to internalize:* publish/subscribe as decoupling (publishers don't know
subscribers), and why fan-out makes the await discipline — not the map — the
hard part.

### V2. Backpressure — *the slow-consumer problem*
A WebSocket sender can only push bytes as fast as that client's TCP socket
drains. If a subscriber reads slowly while messages keep arriving, something has
to give. In `src/realtime_pubsub/backpressure.py`, give each connection a
**bounded outbound mailbox** and decide what happens when it fills:
- Pushing onto a *bounded* `asyncio.Queue` means a publisher can find it full.
  `await queue.put(...)` applies real backpressure — but now a single slow client
  suspends the publisher and, transitively, every other subscriber. That's
  **head-of-line blocking**, and it's usually the wrong default for fan-out.
- The alternatives are all *lossy or disconnecting*: `put_nowait` and, on
  `QueueFull`, **drop the newest**, **drop the oldest** (`get_nowait()` to evict
  the front, then put), or **disconnect the slow client**. Each is a real product
  decision (a chat backlog vs. a live price feed want different answers).
  Implement the policy switch and make it explicit.
- Whatever you choose, the invariant is the same: **one slow consumer must not
  grow memory without bound, nor slow down delivery to everyone else.**

**Done when ALL true:**
- [x] Each connection has a **bounded** outbound mailbox — there is no unbounded queue anywhere on the publish path.
- [x] An explicit overflow **policy switch** exists (drop-newest / drop-oldest / disconnect-slow) and is honored.
- [x] **Invariant under a deliberately stalled reader:** server memory stays bounded **and** delivery to other subscribers is unaffected.
- [x] Messages shed by the policy are **counted** (a metric) — the loss is observable, never silent.

**Proof:** `tests/test_backpressure.py` — a test that stalls one reader and asserts the drop counter climbs while `qsize()` and other-subscriber delivery stay flat (the V2 payoff in the bench).

*Concept to internalize:* bounded queues as the unit of backpressure, head-of-line
blocking, and "slow consumer" as a first-class failure mode you design *for*, not
against.

### V3. Presence — *soft state with a lifecycle*
In `src/realtime_pubsub/presence.py`, track who is currently in each topic and surface it. The
subtlety is that presence is **soft state**: it's only ever an approximation that
must converge as connections come and go.
- Maintain a per-topic membership set keyed by connection (and a client-supplied
  identity). `join` on subscribe, `leave` on unsubscribe, and — easy to forget —
  remove on *every* disconnect path, including an abrupt socket drop.
- A clean leave is the easy case; a client whose laptop lid closes never sends
  one. Real presence leans on a **heartbeat + TTL**: an entry that isn't refreshed
  within a window is presumed gone and swept. Implement the in-process version;
  reason about the heartbeat (and wire it if you go for the stretch). Stamp
  liveness with `time.monotonic()`, not `time.time()` — a TTL measured against a
  wall clock that NTP can step is a TTL that silently stops working.
- Publish presence changes as their own server messages so rooms see joins/leaves
  live — and think about the thundering-herd cost of doing that in a 10k-member
  room.

**Done when ALL true:**
- [x] Per-topic membership tracks join on subscribe, leave on unsubscribe, and removal on **every** disconnect path (including an abrupt socket drop).
- [x] Absence is handled via **heartbeat + TTL**: an entry not refreshed within the window is presumed gone and swept (in-process version).
- [ ] Presence changes are published as their own server messages, so rooms see joins/leaves live.
- [x] An abrupt drop (no clean leave) **still leaves the room** — no ghost members linger.

**Proof:** `tests/test_presence.py` — a test that drops a socket without a clean leave and asserts the member disappears within the TTL; design-doc note on the heartbeat.

*Concept to internalize:* presence as eventually-consistent soft state, and why
"detecting absence" (TTL/heartbeat) is fundamentally harder than detecting a
clean leave.

### V4. Multi-node fan-out — *one logical topic across many processes*
A single node's hub only knows about *its own* sockets. Run two nodes behind a
load balancer and a publish on node A never reaches a subscriber on node B. In
`src/realtime_pubsub/cluster.py`, bridge the local hub to a **cross-node bus**
(Redis pub/sub, via `redis.asyncio`):
- On a local `publish`, also publish the message to a Redis channel for the topic
  so other nodes can deliver it to *their* local subscribers.
- Run a background task that **subscribes to Redis** and, for each message that
  arrives, injects it into the local hub — but **only** delivers to local sockets;
  it must **not** re-publish back to Redis, or you build an infinite echo.
- Stamp every message with this node's `NODE_ID` so a node can recognise and drop
  its own messages coming back around (loop prevention / de-dup).
- The receive loop is a long-lived `asyncio.Task`, and a task that raises dies
  **silently** — nothing prints until someone awaits it. Make a dropped Redis
  connection reconnect with backoff rather than end the loop.
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
  Protocols via the Starlette/FastAPI `WebSocket` route).
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
- [ ] A bound `structlog` logger per connection carrying a connection id;
  structured fields on subscribe/publish (topic, fan-out size, delivery latency).

### Python craft
- [ ] **pyright strict passes clean** — every `# pyright: ignore` carries a
  justifying comment naming what is unknowable and why.
- [ ] **No blocking call on the event loop** — runs clean under
  `PYTHONASYNCIODEBUG=1`; any sync I/O is in a thread/process pool deliberately.
- [ ] **Bounded pool sized on purpose** — the outbox capacity, the DB pool size
  and the worker count tuned *together*, with the reasoning in the design doc.
- [ ] **Graceful shutdown** drains in-flight work on SIGTERM via the FastAPI
  lifespan, and live sockets get a close frame rather than a severed TCP.
- [ ] **Profile committed** — a `py-spy` flamegraph and a `memray` run in
  `docs/03-benchmarks.md`, naming the top bottleneck.

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
2. A `bench/` load test (e.g. a `k6` or `websockets`-based client that opens **thousands**
   of concurrent WebSocket subscribers) reporting: fan-out **throughput**
   (messages delivered/sec) and end-to-end **delivery latency** p50/p99 under a
   sustained publish rate; the numbers with **one deliberately slow subscriber**
   present (proving it doesn't drag the others — that's the V2 payoff); and a
   **two-node** run proving a publish on node A reaches a subscriber on node B.
   Numbers in `docs/03-benchmarks.md`.
3. A **profile**, not just numbers: a `py-spy` flamegraph taken under fan-out
   load and a `memray` run, both in `docs/03-benchmarks.md`, naming where the
   time and the allocations actually go. Where CPython can't reach a target
   above, *that gap is the finding* — record where it topped out and why (GIL
   contention? GC pauses? per-subscriber serialization? a blocking call on the
   loop?). Targets are not scaled down to make the graph green.
4. A short `docs/03-design.md`: your hub concurrency strategy and why; the backpressure
   policy you shipped and the product reasoning; how presence handles abrupt
   disconnects; and the cross-node bus design including how you break echo loops.
5. `make verify` is green (ruff format + ruff lint + pyright strict + pytest);
   no `raise NotImplementedError` remains on a checked path.

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
uv sync                     # once, from the repo root or here
docker compose up -d redis  # only needed for V4 / CLUSTER=true
cp .env.example .env        # then fill in values (WS_AUTH_TOKEN is required!)
make run

# in another shell — `make ws` wraps this, reading the token from .env:
#   websocat 'ws://localhost:8080/ws?token=YOUR_TOKEN&identity=cli'
# then send a frame:
#   {"type":"subscribe","topic":"room1"}
#   {"type":"publish","topic":"room1","payload":{"hello":"world"}}

# multi-node test (V4): run two with CLUSTER=true on different ports,
# subscribe on one, publish on the other, watch it arrive.
#   CLUSTER=true NODE_ID=node-a PORT=8080 make run
#   CLUSTER=true NODE_ID=node-b PORT=8081 make run
```

## 🔬 From the field

<!-- Adoption backlog distilled from RESEARCH.md by /harvest. NOT graded:
     [~] = open, [✔] = adopted — not counted toward graded progress;
     shown under FROM THE FIELD in status detail.
     Tick a box when the idea has actually landed in this project. -->

### Protocol extras (MQTT-inspired)

- [~] Retained messages: the broker keeps the last message per topic, and a new
  subscriber receives it immediately on subscribe; publishing an empty retained
  payload clears it *(→ RESEARCH.md §4)*
- [~] Last Will & Testament: a client registers a "will" message at connect,
  and an abrupt disconnect makes the broker publish it — the canonical
  online/offline pattern, a natural companion to presence *(→ RESEARCH.md §4)*
- [~] Persistent sessions: a subscriber that reconnects with its session id
  receives the messages published while it was offline — real time decoupling,
  not fire-and-forget *(→ RESEARCH.md §1 & 4)*
- [~] QoS 1 over the socket: delivery is publish → ack, and unacked messages
  are redelivered on reconnect — at-least-once to the client, duplicates
  possible and documented *(→ RESEARCH.md §4)*
- [~] Wildcard subscriptions: one subscription matches a topic family
  (`home/+/temp`, `events.us.>`) via trie-based matching, not per-topic
  enumeration *(→ RESEARCH.md §1 & 4)*

### Delivery & flow-control upgrades

- [~] Credit-based flow control (AMQP 1.0 model): a subscriber grants credit
  and the server sends at most that many messages — a slow consumer throttles
  itself without being disconnected and without a fixed drop policy
  *(→ RESEARCH.md §3.4)*
- [~] Write batching: the outbound path coalesces queued messages into batched
  socket writes, and the fan-out throughput gain is measured
  *(→ RESEARCH.md §3.6)*
- [~] Zero-copy fan-out: one published payload is shared by reference
  (`bytes::Bytes`) across every subscriber's mailbox — no per-subscriber copy
  of the body *(→ RESEARCH.md §3.6)*
- [~] A replayable topic buffer (fan-out-read): each topic keeps a bounded
  in-memory log with per-subscriber cursors, so a new subscriber can replay the
  last N messages instead of joining blind *(→ RESEARCH.md §3.1 & 3.5)*
- [~] Consumer groups: N members of a group share a topic's messages (each
  message to exactly one member) while ordinary subscribers still get them all
  — competing consumers layered on pub/sub *(→ RESEARCH.md §3.5)*
- [~] Redis Streams as the cross-node bus: the V4 bridge rides `XADD` /
  `XREADGROUP` instead of fire-and-forget pub/sub, so a node that briefly
  disconnects catches up instead of silently missing messages
  *(→ RESEARCH.md §4 & 5)*

### Correctness practice

- [~] Deterministic simulation testing: the hub + bus run under an injected
  clock/network (madsim/turmoil) with faults, and any failure reproduces
  exactly from its seed *(→ RESEARCH.md §7)*
- [~] Fuzz the frame parser: the JSON protocol parser survives a fuzzer
  (arbitrary bytes never panic it — malformed input always becomes a protocol
  `error`) *(→ RESEARCH.md §7)*
