# Concept Bank — Project 07: Distributed Cache

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — O(1) bounded caching: LRU/LFU from scratch *(V1 · `src/store.rs`)*

**The problem.** An unbounded `HashMap` cache is an OOM on a timer. So you cap it — and now every insert into a full cache must pick a victim. "Evict the oldest" via a scan is O(n) per insert; at a million entries your cache spends its life choosing victims instead of serving. The eviction *choice* is also a policy question: throw out what hasn't been touched lately (LRU)? What's touched rarely (LFU)? They disagree, and the difference is your hit ratio.

**The idea.** O(1) eviction requires a second structure beside the map that maintains the eviction order *incrementally*: the classic LRU is a hash map whose values point into a doubly-linked list — `get` unlinks and moves to front, evict pops the tail, both O(1). LFU swaps the recency list for frequency buckets. Make the policy swappable and you can *measure* the disagreement. TTLs check lazily on read (an expired entry is a miss that also cleans up), and the locking granularity (one lock vs sharded) decides your concurrent throughput.

**In the wild:** Redis's approximated-LRU/LFU eviction, memcached's slab LRU, CPU caches (pseudo-LRU in silicon), Caffeine's TinyLFU (the modern answer to LRU's scan-pollution weakness).

**You own it when you can explain:**
- [ ] Why O(1) eviction structurally requires the second index — and walk the map+linked-list design for get/put/evict.
- [ ] An access trace where LRU and LFU evict *different* keys, and which workload shape favors each (recency-of-use vs long-term popularity).
- [ ] Why a `get` must touch the eviction state — what "the key you keep reading gets evicted anyway" implies about a broken implementation.
- [ ] Lazy TTL expiry vs a background sweeper — what each costs and why lazy alone can strand memory (expired keys nobody reads again).
- [ ] The scan-pollution problem: one bulk export reads a million cold keys once and LRU evicts your entire hot set — and how an admission filter (TinyLFU) refuses entry to one-hit wonders.

**Depth probes:**
- Sharded locks: how does sharding by key hash change contention, and what operation gets *harder* (global size accounting, eviction across shards)?
- Why does Redis use *sampled* LRU rather than a true linked list? What did they trade?

**Trap:** benchmarking with uniform random access. Real traffic is Zipfian — a cache that looks mediocre under uniform access may be excellent under real skew, and eviction-policy differences only show up under skew.

---

## 🧠 Card 2 — Consistent hashing & virtual nodes *(V2 · `src/ring.rs`)*

**The problem.** Shard keys across N nodes with `hash(key) % N` and everything works — until N changes. Add one node and *almost every key* changes owner (`% 4` vs `% 5` agree on almost nothing). For a cache that means a cluster-wide cold miss storm — an outage caused by *scaling up*. On Black Friday.

**The idea.** Hash both nodes and keys onto a circle; a key belongs to the first node clockwise from it. Now adding a node only claims the arc between it and its predecessor — ≈1/N of the keyspace moves, the theoretical minimum. Raw rings clump (random node positions make uneven arcs), so each physical node appears as many **virtual nodes** scattered around the ring, flattening the load at the cost of ring memory. Replica sets fall out naturally: walk clockwise collecting the next N *distinct physical* nodes.

**In the wild:** DynamoDB (the Dynamo paper made this famous), Cassandra's token ring, memcached client libraries (ketama), Envoy's ring-hash LB, Discord's guild sharding.

**You own it when you can explain:**
- [ ] The `% N` catastrophe quantified: what fraction of keys move for N→N+1 in both schemes.
- [ ] Ring lookup mechanics: sorted vnode positions + binary search for "first ≥ hash(key)", wrapping at the top.
- [ ] What vnodes fix (arc-size variance → load variance) and the tradeoff in their count (memory + rebalance granularity vs balance).
- [ ] Why replica selection must dedupe physical nodes while walking vnodes.
- [ ] What happens to a key's *data* when ownership moves — cold miss vs migration, and why a cache can just choose cold-miss.

**Depth probes:**
- Weighted nodes (one box is 2× the RAM): how do vnodes make this trivial?
- Compare with rendezvous (HRW) hashing — when is a ring overkill?

**Trap:** thinking consistent hashing balances *load*. It balances *keyspace*. One viral key still melts one node — hot-key handling (local L1, key replication) is a separate problem.

---

## 🧠 Card 3 — SWIM: membership without a master *(V3 · `src/membership.rs`)*

**The problem.** Nodes must agree on who's in the cluster and who's dead — with no coordinator, no ZooKeeper. Everyone-pings-everyone is O(n²) messages per interval and collapses at scale; worse, one dropped packet under load looks identical to a dead node, so naive detectors evict healthy nodes exactly when the cluster is busiest.

**The idea.** SWIM: each round, every node pings *one random* peer (constant per-node load, regardless of cluster size). No reply? Ask k other peers to ping the target for you (**indirect probing**) — only if nobody can reach it does it become *suspect*. Suspicion spreads by **gossip piggybacked** on the ping traffic; the accused node can refute by re-announcing itself with a higher **incarnation number** (the tiebreaker that stops stale rumors from beating fresh truth). Failure detection latency stays bounded while message load stays flat.

**In the wild:** HashiCorp Serf/Consul (memberlist), Redis Cluster's gossip bus, Cassandra's gossip; the SWIM paper itself is a genuinely readable classic.

**You own it when you can explain:**
- [ ] The O(n²) argument against all-to-all heartbeats, and what randomized probing changes about per-round cost.
- [ ] Indirect probing as false-positive insurance: why "I can't reach X" ≠ "X is dead" (asymmetric routes, your own congestion).
- [ ] The alive → suspect → dead lifecycle, and what the suspicion timeout buys the accused.
- [ ] Incarnation numbers: who increments them (only the node itself) and why that's what makes refutation authoritative.
- [ ] How membership changes feed the hash ring (V2) — detection → ring update → ownership shift, automatically.

**Depth probes:**
- What's the expected detection latency for a dead node in an N-node cluster with period T? What knobs shorten it, at what false-positive cost?
- Why does gossip dissemination scale as O(log n) rounds to reach everyone?

**Trap:** tuning suspicion timeouts short "for fast detection" — under a GC pause or CPU spike, the cluster starts evicting *live* nodes, the ring churns, and the resulting rebalance load causes more pauses. Failure detectors can cause the failures they detect.

---

## 🧠 Card 4 — Replication, quorums & choosing availability *(V4 · `src/coordinator.rs`)*

**The problem.** Sharding without replication means every node death loses its whole shard — for a cache, a synchronized cold-miss storm into your database. So each key lives on N nodes. But now writes must reach several places, reads might see old values, and *someone* has to route requests — while the client should stay blissfully unaware of the topology.

**The idea.** Replicate each key to the next N distinct nodes on the ring. Any node can **coordinate**: resolve the replicas, serve locally if it owns a copy, else forward and proxy back. Then make the consistency decision *on purpose*: W=1 with async replication (fast, may serve stale after a failover) vs quorum R+W>N (read and write sets must overlap — stronger, slower). A cache should usually choose availability and admit the staleness — and be able to say so out loud.

**In the wild:** the Dynamo paper's coordinator+quorum model, Cassandra's tunable consistency (ONE/QUORUM/ALL), Riak; Redis Cluster chooses differently (single-owner + failover) — comparing the two is instructive.

**You own it when you can explain:**
- [ ] Replication factor vs quorum: N, R, W as three separate dials, and *why* R+W>N guarantees overlap (pigeonhole).
- [ ] The coordinator pattern: what forwarding costs (a hop) vs what client-side routing costs (topology-aware clients).
- [ ] Why "cache" changes the CAP answer: what a stale read costs here vs in a ledger — and where the line is (sessions? counters?).
- [ ] What must recompute when membership changes, and why requests must never route to a node marked dead.
- [ ] (Stretch) Hinted handoff and read-repair — the two self-healing patterns for a node that missed writes.

**Depth probes:**
- With W=1 async replication, a write lands on the primary replica which dies 5 ms later. Walk the read path — what do clients see, and for how long?
- Why do quorums *not* give you transactions or CAS? What's still missing?

**Trap:** claiming "R+W>N = strong consistency" unqualified. It gives read-your-latest-completed-write overlap — it does not linearize concurrent writes or survive every failover nuance. Precision here is what separates having built it from having read about it.

---

## ⚡ Rapid-fire round

- [ ] Why gossip rides UDP (loss-tolerant, no connection state, no head-of-line blocking) while the data path rides TCP/HTTP.
- [ ] Graceful shutdown = gossip your own departure — peers learn instantly instead of waiting a suspicion timeout.
- [ ] TTL jitter, again (project 01's lesson applied node-local).
- [ ] Why internal node-to-node RPC is separated and authenticated — a forged "forward" is a cache-poisoning vector.
- [ ] The observability that proves the ring works: per-node key counts before/after a join.

## 🔗 Connects to

- The ring is the routing layer under project 20's shard placement thinking.
- SWIM's "detect absence via timeout" is project 03's presence TTL, hardened.
- The availability-vs-consistency choice made here is inverted in project 09 (Raft chooses consistency) — hold both in your head and you understand the spectrum.
