<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 07 — Distributed Cache

> A single-node cache is a `HashMap` with an eviction rule. The moment one box
> can't hold the working set — or can't survive being rebooted — you need a
> *cluster*, and every easy thing gets hard: which node owns a key, how the set of
> nodes agrees on who's alive without a coordinator, and how you add a node on
> Black Friday without cold-missing the entire keyspace. This project builds that
> cluster from the ring up: a hand-rolled LRU/LFU on each node, a consistent-hash
> ring to shard across them, SWIM gossip so they find each other and evict the
> dead, and replication so one funeral doesn't lose a shard.

## What it does (the easy part)
- `PUT /cache/{key}` with a body → stores the value (optional `?ttl=<secs>`).
- `GET /cache/{key}` → `200` + the bytes, or `404` if absent/expired.
- `DELETE /cache/{key}` → evicts it.
- Any node accepts any key: it routes the request to the node(s) that own it and
  proxies the answer back — the client never needs to know the topology.
- `GET /cluster` → this node's view of the membership (who's alive/suspect/dead).

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. A bounded local cache with O(1) eviction — *no `cargo add lru`*
Each node needs an in-memory store that is **fast** and **bounded**: gets and puts
in O(1), and a hard cap so a hot node never OOMs. The naive `HashMap` grows without
limit; the naive "evict the oldest" scans the whole map (O(n)) on every insert.
Build the store in `src/store.rs` — start with **LRU**, then make the eviction
policy swappable so you can add **LFU** and compare.

**Done when ALL true:**
- [ ] `get` and `put` are **O(1)** — no scan of the keyspace to serve a read or to pick a victim.
- [ ] The store **never exceeds its capacity**: inserting into a full store evicts exactly one entry (the policy's victim) first.
- [ ] A `get` on the hot path **updates recency/frequency** so the eviction policy actually reflects access order — a key you keep reading is not the one thrown out.
- [ ] **TTL is honoured:** an entry past its TTL is not returned by `get` and does not count as a live entry — expiry is observable without an external sweep.
- [ ] The **eviction policy is swappable** (LRU vs LFU) behind one interface, and a test shows the two make *different* victim choices on the same access trace.
- [ ] Under concurrent readers + writers the store stays correct (no lost updates, no panic) — and the locking granularity is a **documented decision** (one big lock vs sharded).

**Proof:** unit tests for the capacity invariant and LRU-vs-LFU victim divergence; a
property test that a random op sequence never exceeds capacity; a `bench/` number
for get/put throughput (single-threaded and N-thread contended) in `docs/07-benchmarks.md`.

*Concept to internalize:* why O(1) eviction needs a second data structure beside the
map (an ordering/frequency index), and the LRU vs LFU vs LRU-K tradeoff. **Stretch:**
an admission filter (TinyLFU) so a one-hit-wonder scan can't evict your whole working set.

### V2. Consistent hashing with virtual nodes — *`key % N` is a trap*
To spread keys across nodes you might reach for `hash(key) % N`. Add or remove one
node and **almost every key** remaps to a different node — a total cache flush, an
outage disguised as a deploy. Build a consistent-hash ring in `src/ring.rs` so that
adding/removing a node only moves the keys it should, and **virtual nodes** so load
stays even instead of clumping.

**Done when ALL true:**
- [ ] A key maps to a node by position on the ring — the same key always resolves to the same node given the same membership.
- [ ] **Minimal disruption:** adding an Nth node remaps only ≈`1/N` of keys; removing a node remaps only the keys it owned — *not* the whole keyspace.
- [ ] **Virtual nodes** spread each physical node across many ring positions, and increasing the vnode count measurably **flattens the load distribution** (lower spread across nodes).
- [ ] Looking up the **N replicas** for a key returns N *distinct physical* nodes walking the ring clockwise (never the same node twice, even though vnodes repeat).
- [ ] Ring lookup is **sub-linear** in the number of nodes (a sorted structure, not a scan of every vnode).

**Proof:** a test that measures the fraction of keys that move when a node is
added/removed (asserts ≈`1/N`, not ≈`1`); a distribution test showing vnodes flatten
load; `docs/07-design.md` records the hash function and vnode count you chose and why.

*Concept to internalize:* why `mod N` is catastrophic on resize, how the ring bounds
key movement to `O(keys/N)`, and how vnode count trades memory for balance.

### V3. Gossip membership & failure detection (SWIM) — *no coordinator*
Nodes must discover each other, notice when one dies, and agree on the live set —
**without** a central registry or ZooKeeper. All-to-all heartbeating is `O(n²)`
messages and gives false positives under load. Build a SWIM-style membership layer
in `src/membership.rs`: randomized ping / indirect-ping probing over UDP, a
suspicion state machine, and gossip-piggybacked dissemination.

**Done when ALL true:**
- [ ] A new node that knows **one seed** joins the cluster and, within a bounded number of gossip rounds, every node's `/cluster` view converges to include it.
- [ ] A **killed node is detected** and marked dead by the rest of the cluster within a bounded time — without every node having pinged it directly.
- [ ] **Indirect probing** exists: a node that fails a direct ping asks *k* peers to ping the target before declaring it suspect — a single dropped packet does **not** evict a healthy node.
- [ ] The **suspect → dead** lifecycle uses incarnation numbers so a wrongly-suspected node can **refute** and stay alive (no flapping).
- [ ] Per-round message load is **bounded and independent of cluster size** (constant fan-out) — not `O(n)` pings per node per round.
- [ ] A membership change **updates the hash ring** (V2), so ownership follows the live set automatically.

**Proof:** an integration test that boots a 3-node cluster, kills one, and asserts
the survivors' views converge to `dead` within the timeout; a test that a single
dropped ping does not evict a node (indirect probe saves it); `docs/07-design.md`
records the probe interval, suspicion timeout, and fan-out.

*Concept to internalize:* why gossip/SWIM beats `O(n²)` heartbeating, the role of
incarnation numbers, and the false-positive vs detection-latency tradeoff.

### V4. Replication & request coordination — *survive a node loss*
With sharding alone, losing a node loses its whole shard. Replicate each key to the
**next N nodes** on the ring, and make any node able to coordinate a request:
resolve the key's replicas, serve locally if it owns a copy, otherwise forward to a
replica and proxy the result. Build this in `src/coordinator.rs`.

**Done when ALL true:**
- [ ] A key is stored on **N distinct replicas** (the ring's N successors); a `PUT` is not acknowledged until it reaches the replica(s) your chosen write policy requires.
- [ ] **Any node can serve any key:** a request to a non-owner is routed to an owner and the answer proxied back — the client sees no difference.
- [ ] Killing **one** replica of a key leaves the value still `GET`-able from another replica — a single node loss is **not** data loss for replicated keys.
- [ ] When membership changes, ownership recomputes from the **current** ring — a request never routes to a node marked dead.
- [ ] The **consistency tradeoff is explicit:** you can state the read/write quorum you chose (e.g. W=1 async replicate, or R+W>N) and what staleness that admits — it's a cache, so name what you gave up.

**Proof:** an integration test that writes a key, kills one of its replicas, and
reads the value back from another node; a test that a request to a non-owner returns
the same value as one to an owner; `docs/07-design.md` names the replication factor
and the read/write policy.

*Concept to internalize:* replication factor vs quorum, coordinator/proxy routing,
and why a cache can pick availability over consistency where a database can't.
**Stretch:** hinted handoff or read-repair so a rejoining node heals its shard.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] **Client API** (`GET`/`PUT`/`DELETE /cache/{key}`) is clean HTTP with correct status codes: `404` for miss/expired, `204` for delete, body echoes bytes verbatim.
- [ ] **Internal node-to-node RPC** is separated from the public API (a distinct route/path or port) so a forwarded request can't be spoofed as a client one.
- [ ] **Gossip transport is UDP** (datagram, fire-and-forget) — and `docs/07-design.md` says why UDP is right for SWIM (loss-tolerant, no head-of-line blocking) where the data path uses TCP/HTTP.
- [ ] **Graceful shutdown:** on SIGTERM the node leaves the cluster (gossips its own departure) so peers don't wait a full suspicion timeout to notice.

### Caching
- [ ] Eviction policy (V1) with a hard capacity and honoured TTLs.
- [ ] **TTL jitter** so a batch of keys written together don't all expire on the same tick (thundering-herd of misses).
- [ ] Cache semantics documented: what happens on a full store, on an expired-but-present entry, and on a rebalance (do moved keys survive or cold-miss?).

### Security
- [ ] **Auth on writes / cluster control:** `PUT`/`DELETE` and any admin endpoint require a shared key/token; the internal RPC path is authenticated so an outsider can't inject values. Keys are never logged; the comparison's timing-safety is a documented decision.
- [ ] **Input validation:** key length/charset bounded and value size capped — a giant value or pathological key can't blow the node's memory budget or the UDP MTU for gossip.

### Observability
- [ ] `tracing` span per request (via `common-telemetry`) with a request id, including whether the request was served **locally or forwarded** and to which node.
- [ ] Metrics at `/metrics`: **cache hit/miss ratio, entry count & bytes vs capacity, evictions, membership size, and gossip round / suspicion events.**
- [ ] The **key distribution across nodes** is observable (per-node key count) so you can *see* the ring rebalance when a node joins or leaves.

---

## Cross-cutting scale skills (every project carries these)
- **Backpressure & bounds:** the store is capacity-bounded; the gossip path is
  datagram-bounded (no unbounded queue of pending pings).
- **Graceful shutdown:** drain in-flight HTTP *and* announce departure to the cluster.
- **Benchmarks with numbers:** `bench/` + `docs/07-benchmarks.md` — get/put throughput,
  and the fraction of keys moved on a node join (the consistent-hashing payoff).

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. `bench/` contains (a) local store get/put throughput (single + contended) and
   (b) a measurement of the fraction of keys remapped when the cluster grows from
   N→N+1 nodes — numbers in `docs/07-benchmarks.md`.
3. `docs/07-design.md` records the four decisions the SPEC grades: **eviction policy
   (LRU/LFU + locking), hash function & vnode count, SWIM timings (probe/suspicion/
   fan-out), and replication factor & read/write policy.**
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p distributed-cache`
   are green; no `todo!()` remains on a checked path.

## Suggested order of attack
1. Get one node serving `GET`/`PUT`/`DELETE` straight against an unbounded `HashMap`.
2. Make the store bounded with LRU eviction + TTL (V1), then make the policy swappable.
3. Build the consistent-hash ring with vnodes and unit-test key movement (V2) — still single node.
4. Add SWIM gossip so multiple nodes find each other and detect death (V3); wire membership → ring.
5. Add replication + coordinator routing so any node serves any key and a node loss is survivable (V4).
6. Add auth, metrics, key-distribution observability; benchmark, document, tune.

## Run a local cluster
```bash
# Single node (dev):
cp .env.example .env
cargo run -p distributed-cache

# A 3-node cluster (gossip + rebalancing you can watch):
docker compose up --build          # cache-a (seed) + cache-b + cache-c
curl -XPUT  localhost:8071/cache/hello -d 'world'
curl        localhost:8072/cache/hello     # served from whichever node owns it
curl        localhost:8073/cluster         # membership view converges across nodes
```
