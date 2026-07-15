# Concept Bank — Project 03: Real-time Pub/Sub + Presence

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — The fan-out hub & lock discipline *(V1 · `src/hub.rs`)*

**The problem.** A pub/sub hub is "just" a map of topic → subscribers — until thousands of tasks touch it concurrently. Wrap it in one mutex and every publish serializes behind every other. Worse: if you hold that lock *while sending* to a subscriber, one client on a stalled connection freezes message delivery for the entire server. The data structure was never the hard part; the lock-holding discipline is.

**The idea.** Publishers and subscribers are decoupled through the hub (that's what pub/sub *means* — senders don't know receivers). Internally: take the lock only to snapshot who's subscribed, release it, then send outside the lock. Cleanup is the other half — a dropped socket must vanish from every topic it joined, on every exit path, or the map leaks forever.

**In the wild:** Redis pub/sub internals, NATS subject routing, Phoenix PubSub, socket.io rooms — every chat/notification backend has this exact structure at its core.

**You own it when you can explain:**
- [ ] Pub/sub as decoupling: what breaks (in coupling terms) if publishers hold direct references to subscribers.
- [ ] Why "never hold the lock across a send" is the design rule, with the freeze scenario that motivates it.
- [ ] The locking spectrum — one `Mutex`, `RwLock`, per-topic locks, sharded maps — and what each choice serializes or parallelizes.
- [ ] Why `disconnect` must be idempotent and reachable from *every* exit path (clean close, error, panic, timeout), and what the leak looks like if one path forgets.
- [ ] What `publish` returning "reached N subscribers" is useful for (metrics, debugging fan-out).

**Depth probes:**
- A topic has 100k subscribers. What's the publish cost, and where would you shard the work?
- Why is `tokio::sync::broadcast` not simply the answer? (What semantics does it fix for you that you might not want?)

**Trap:** testing with fast local clients. The bugs here only appear with a *slow* or *dead* receiver — which is why the SPEC makes you stall one on purpose.

---

## 🧠 Card 2 — Backpressure & the slow consumer *(V2 · `src/backpressure.rs`)*

**The problem.** A WebSocket can only drain as fast as that client's network. Publish 1,000 msg/s into a room where one phone on hotel wifi reads 10 msg/s, and 990 msg/s must pile up *somewhere*. Unbounded buffering = OOM. Awaiting the slow client's queue = the publisher blocks = every *other* subscriber now receives at hotel-wifi speed. That second failure has a name — **head-of-line blocking** — and it's the default behavior of the "obvious" implementation.

**The idea.** Every connection gets a *bounded* outbound mailbox, and full is a first-class case with an explicit policy: drop-newest, drop-oldest, or disconnect the straggler. The invariant that survives all policies: one slow consumer must never grow server memory unboundedly nor delay anyone else. And every shed message is counted — loss you can graph is a policy; loss you can't see is a bug.

**In the wild:** Slack/Discord gateway connections (they disconnect laggy clients), financial market-data feeds (conflate to latest price), MQTT QoS 0, Kafka consumer lag semantics.

**You own it when you can explain:**
- [ ] Why bounded queues are the *unit* of backpressure — the queue bound is where "producer faster than consumer" becomes a decision instead of an accident.
- [ ] Head-of-line blocking end to end: how awaiting one full mailbox transitively stalls the fan-out.
- [ ] Drop-newest vs drop-oldest vs disconnect — a concrete product (chat history, live scores, stock ticker) where each is the right call.
- [ ] Why "slow consumer" is a failure mode you design *for*, not against — it is guaranteed to happen at scale.
- [ ] Why the drop counter is part of the design, not observability garnish.

**Depth probes:**
- For a live auction feed, which policy? What about a collaborative text editor? Why do they differ?
- How would *conflation* (keep only the latest value per key) fit as a fourth policy, and for what data shape does it beat all three?

**Trap:** "we'll just make the buffer bigger." A bigger buffer converts a fast, visible failure into slow memory growth plus *stale* delivery — the slow client is now seeing messages from 40 seconds ago.

---

## 🧠 Card 3 — Presence as soft state *(V3 · `src/presence.rs`)*

**The problem.** "Who's online?" sounds like a set you maintain. But a laptop lid closing sends no goodbye — TCP can take *minutes* to notice a vanished peer. So any presence set is wrong some of the time; the only question is how wrong, for how long, and in which direction.

**The idea.** Treat presence as *soft state*: an approximation that converges. Clean joins/leaves update it immediately; absence is detected by heartbeat + TTL — an entry not refreshed within the window is presumed gone and swept. You're choosing a detection latency (the TTL) rather than pretending exactness is possible.

**In the wild:** Discord/Slack online indicators (watch them lag ~30–60 s behind reality), Redis key TTLs as presence, Kubernetes node heartbeats, Zookeeper ephemeral nodes.

**You own it when you can explain:**
- [ ] Why detecting *absence* is fundamentally harder than detecting a leave — nothing arrives to tell you.
- [ ] The heartbeat + TTL mechanism and the tradeoff in the TTL (short = fast detection + false-offline flaps; long = ghosts linger).
- [ ] "Eventually consistent" applied here: what the set is allowed to be wrong about, and for how long.
- [ ] The ghost-member bug: which disconnect paths create them and how the sweep bounds their lifetime.
- [ ] The presence-broadcast amplification problem: a join in a 10k-member room costs 10k sends — and what you'd do about it (batch, digest, or don't broadcast).

**Depth probes:**
- Two servers each think they own client X's presence entry after a reconnect. What converges the view, and how fast?
- Why is TCP keepalive alone not a presence mechanism?

**Trap:** returning presence as exact truth to product code ("send push notification if offline"). Soft state consumed as hard state produces double-notifications and missed messages — the consumer must know the staleness contract.

---

## 🧠 Card 4 — Multi-node fan-out & the echo loop *(V4 · `src/cluster.rs`)*

**The problem.** Put two server nodes behind a load balancer and pub/sub silently breaks: the publisher's socket lives on node A, the subscriber's on node B, and node A's in-memory map has never heard of B's clients. Sticky sessions don't save you — two users in the same room can land on different nodes.

**The idea.** Split the system into a **local hub** (owns actual sockets, does the final delivery) and a **cross-node bus** (Redis pub/sub, carries messages between nodes). Every local publish also goes to the bus; a bridge task on each node injects bus messages into its local hub — *only* locally, never back onto the bus. Stamp messages with the origin `NODE_ID` so a node drops its own echoes, or a 2-node cluster becomes an infinite message loop. Subscribe to bus channels lazily — only for topics with local interest.

**In the wild:** Phoenix PubSub's Redis adapter, socket.io-redis, Ably/Pusher internals, and the same shape as project 16's cross-pod chat.

**You own it when you can explain:**
- [ ] Why horizontal scaling breaks in-memory pub/sub, with the A/B diagram.
- [ ] The hub/bus split: exactly which component owns sockets, which carries inter-node traffic, and why Redis here is a bus, *not* a store (nothing is durable, and that's fine).
- [ ] How the echo loop forms in a naive bridge, and how origin-stamping + drop-your-own breaks it.
- [ ] Why lazy channel subscription matters: what a node subscribed to *every* topic pays on a big cluster.
- [ ] The delivery guarantee you end up with (at-most-once, no ordering across nodes) and why that's acceptable for this product.

**Depth probes:**
- Redis (the bus) dies for 10 seconds. What do users on one node vs two nodes experience? Is that acceptable? What would a durable bus (NATS JetStream, project 05) change?
- Three nodes, and node B's bridge re-publishes to the bus by mistake. Trace one message's life.

**Trap:** thinking the bus needs to be reliable. For live chat/presence, a lossy fire-and-forget bus is the *correct* cost/benefit — knowing when best-effort is enough is the senior-engineer judgment here.

---

## ⚡ Rapid-fire round

- [ ] The WebSocket upgrade: what the `101 Switching Protocols` handshake actually negotiates and why the connection is stateful afterward.
- [ ] Ping/pong as liveness: who sends, what a missed pong means, and why you close with a proper frame + code instead of dropping TCP.
- [ ] Why auth happens on the upgrade request — before the socket exists — not after.
- [ ] The caps every client-controlled input needs: message size, topics per connection, publish rate, topic-name length/charset — and the attack each cap blocks.
- [ ] Graceful shutdown for long-lived connections: stop accepting, close frames, drain — vs the reconnect storm a hard kill causes.

## 🔗 Connects to

- The bounded-mailbox/slow-consumer answer scales up to 100k-viewer chat in project 16 (V4).
- The NODE_ID echo-suppression trick returns verbatim in project 16's cross-pod bus.
- Heartbeat+TTL absence detection is SWIM's core idea in project 07, upgraded with indirect probing.
