# Publish/Subscribe Systems From First Principles: A Broker Implementer's Field Guide

## TL;DR
- **Pub/sub is fundamentally about three-way decoupling** — space, time, and synchronization (Eugster et al., 2003) — and every design decision in a broker is a tradeoff about how much of that decoupling you preserve versus how much ordering, durability, and delivery-guarantee machinery you bolt on top. The single most consequential architectural axis in 2026 is **shard-per-partition local disk (Kafka/Redpanda) vs. shared-log-on-object-storage (WarpStream/KIP-1150)**, and cross-AZ network cost is what is pushing the industry toward the latter — WarpStream states that "more than 80% of Kafka cloud costs were driven by inter-AZ networking fees — a structural problem that could only be solved by eliminating disk-based replication entirely" (though this magnitude is disputed — see Caveats).
- **"Exactly-once" is real but narrow**: it means idempotent producers (PID + epoch + monotonic sequence numbers) plus transactional atomic writes, i.e. *effectively-once processing within one system*. End-to-end exactly-once *delivery* across independent systems remains impossible in the strict sense (Two Generals / FLP); what you build is at-least-once + idempotency/dedup.
- **If you are building a broker in Rust**, the correct minimal core is an append-only segmented commit log + sparse offset index + a topic/partition registry + a fan-out-read cursor model, and the highest-leverage choices are your async runtime (tokio work-stealing vs. glommio/monoio/compio thread-per-core io_uring), your fsync policy, and whether you test with deterministic simulation from day one.

## Key Findings

1. **The taxonomy still holds.** Eugster, Felber, Guerraoui, and Kermarrec's "The Many Faces of Publish/Subscribe" (ACM Computing Surveys, Vol. 35, No. 2, June 2003, pp. 114–131) remains the canonical framing: pub/sub's differentiator from message queuing, RPC, shared spaces, and message passing is *full decoupling in time, space, and synchronization*. Everything else is implementation.
2. **Log-centric beats queue-centric for scale.** The Kafka model (Kreps, Narkhede, Rao, NetDB 2011) — a partitioned, replicated, append-only commit log with consumer-tracked offsets and a *pull* model — won for high-throughput streaming because it turns fan-out into cheap sequential reads from page cache and pushes delivery-state tracking to the consumer.
3. **Ordering is a consensus problem in disguise.** Total-order broadcast is provably equivalent to consensus; per-partition ordering is cheap, global ordering is expensive, and this is why nearly every scalable system offers *only per-partition/per-key order*.
4. **Replication has consolidated on two idioms.** Kafka's ISR (in-sync replica) model and Raft-per-unit (Redpanda partition, RabbitMQ quorum queue, NATS JetStream stream). Pulsar is the outlier: it segregates serving (stateless brokers) from storage (BookKeeper), giving segment-centric storage.
5. **The frontier is diskless/S3-first.** KIP-1150 (Diskless Topics) was accepted by the Apache Kafka community on March 2, 2026 — Aiven reports the vote "passed with overwhelming support of 9 binding votes and 5 non-binding ones"; WarpStream, AutoMQ, and Confluent Freight already ship the idea. The driver is economic: cross-AZ replication traffic is the dominant cost of cloud Kafka.
6. **Rust is now a first-class broker language.** Apache Iggy, Redpanda (C++ but same philosophy), Fluvio, and clients like async-nats/rumqttd/rskafka demonstrate the thread-per-core + io_uring + zero-copy playbook, and deterministic simulation testing (madsim) is the emerging correctness standard.

---

## Details

### 1. Foundations and first principles

**The three decouplings (Eugster et al.).** In a pub/sub system a subscriber registers interest via `subscribe()` and is asynchronously notified of matching events published via `publish()`; an intermediary event service acts as proxy for the subscribers.

- **Space decoupling** (a.k.a. referential decoupling): publishers and subscribers do not know each other. They hold no references; the broker mediates.
- **Time decoupling:** publisher and subscriber need not be active simultaneously. A subscriber can receive an event published while it was offline (if the system persists it).
- **Synchronization decoupling:** publishers are not blocked while producing, and subscribers are notified asynchronously (e.g. via callbacks) while doing other work. The paper contrasts this with tuple spaces / distributed shared memory, which give space + time decoupling but *not* synchronization decoupling because consumers pull synchronously.

This is the yardstick. Message passing couples in time and space. Synchronous RPC/request-response couples all three (caller waits for a named callee). Message queuing gives space + time decoupling but classic point-to-point queues break the "one event → many subscribers" property because a message is consumed by exactly one receiver.

**Precise distinctions (not hand-waving):**

| Paradigm | Space | Time | Sync | Cardinality | Delivery-state owner |
|---|---|---|---|---|---|
| Request/response (RPC) | coupled | coupled | coupled | 1→1 | caller |
| Point-to-point queue | decoupled | decoupled | half (consumer pulls) | 1→1 (competing consumers) | broker (per-message ack) |
| Pub/sub (topic) | decoupled | decoupled* | decoupled | 1→N | broker or consumer |
| Event streaming (log) | decoupled | decoupled | decoupled | 1→N, replayable | **consumer (offset)** |

\*Time decoupling for pub/sub is only real if the broker persists; core NATS (non-JetStream) and Redis Pub/Sub are fire-and-forget and thus temporally *coupled* — a subscriber that is offline misses the message. This is the single most common source of confusion.

The critical implementation distinction between a **queue** and a **log**: a queue *destroys* a message on ack and typically maintains per-consumer state on the broker; a **log** keeps messages until retention expires and makes each consumer track its own offset/cursor. This is why Kafka/Pulsar/Redis Streams support replay and multiple independent consumer groups reading the same data, while classic RabbitMQ/SQS queues do not.

**Subscription models:**
- **Channel/topic-based:** subscribe to a named subject. O(1) routing. MQTT topics (`home/+/temperature`), Kafka topics, NATS subjects (`events.us.>`).
- **Type-based:** subscribe by message type/class (common in language-integrated systems).
- **Content-based:** subscribe with predicates over message content (`price > 100 AND symbol = 'AAPL'`). Most expressive, most expensive to route.

**Content-based matching algorithms** (the genuinely hard part, mostly relevant if you go beyond topic routing):
- **Counting algorithm (Yan/Garcia-Molina, Le Subscribe / Gryphon lineage):** decompose subscriptions into elementary predicates. For an event, first compute the set of satisfied predicates, then use an association table to count, per subscription, how many of its predicates are satisfied; a subscription matches when its count equals its predicate cardinality. Good when many subscriptions share predicates.
- **Siena-style covering/merging + routing trees:** brokers store subscriptions in routing tables and forward events only toward directions with matching filters. Uses *covering* (subscription σ1 covers σ2 if everything matching σ2 also matches σ1) so covered subscriptions need not be propagated, and *advertisements* from publishers to prune routing paths. REBECA uses merging-based routing.
- **Gryphon parallel search trees (PST):** each tree node is a test, each subscription is a root-to-leaf path; matching an event is a tree traversal following only edges whose attribute test the event satisfies. Factorizes shared tests → sub-linear matching in the number of subscriptions. Downside: every broker needs a full copy of all subscriptions, impractical under high subscription churn.
- **Rete-like approaches:** from production-rule systems; share condition-test state across rules — same factorization idea, more general.
- **Bloom-filter filtering:** encode subscriptions/interests as Bloom filters (or counting Bloom filters) for cheap, probabilistic "could this match?" tests at each hop; used for range-limited subscriptions and subscription-forwarding traffic reduction. Accept false positives (extra forwards), never false negatives.

For a first broker, **implement topic-based routing with a trie/hash for exact + wildcard matching** and treat content-based routing as an optional layer — it is where most academic complexity lives and most production systems (Kafka, NATS core) deliberately don't go.

### 2. Delivery semantics and correctness

**The three semantics:**
- **At-most-once:** send, never retry. Data loss possible, no duplicates. (Kafka `acks=0`; MQTT QoS 0; Redis Pub/Sub.)
- **At-least-once:** retry until acked. No loss, duplicates possible. The default and the pragmatic sweet spot. (Kafka `acks=all` without idempotence; MQTT QoS 1; RabbitMQ with publisher confirms + consumer acks.)
- **Exactly-once:** no loss, no duplicates — *within a bounded scope*.

**What "exactly-once" really means.** Kafka's EOS (introduced in 0.11, KIP-98) is built from two mechanisms:
1. **Idempotent producer:** the broker assigns each producer a **Producer ID (PID)** and the producer attaches a **producer epoch** and a **per-partition monotonically increasing sequence number**. These live in the RecordBatch header (PID = int64, epoch = int16, `baseSequence` = int32; see §3.1). The broker keeps, per (PID, partition), the metadata of the **last 5 batches** and rejects a batch whose base sequence is not exactly `lastSeq + 1`: a lower number is a silently-ACKed duplicate, a higher number throws `OutOfOrderSequenceException`. This dedups retries *within a single producer session*. It requires `acks=all`, `retries>0`, and `max.in.flight.requests.per.connection <= 5` — the official docs state "broker only retains at most 5 batches for each producer. If the value is more than 5, previous batches may be removed on broker side." On producer restart a *new* PID is assigned and sequences reset to 0, so idempotence does **not** cover cross-restart duplicates — that's what transactional.id / EOS is for.
2. **Transactions:** atomic writes across multiple partitions plus the consumer-offset commit, via a transaction coordinator and `read_committed` isolation on the consumer, enabling the atomic *consume-transform-produce* loop.

The honest framing every implementer should internalize (and Jay Kreps himself argued this): **end-to-end exactly-once *delivery* between independent processes is impossible** in the strict asynchronous-with-failures model. This follows from the **Two Generals problem** (no finite number of unreliable one-way messages yields common knowledge of delivery) and **FLP** (Fischer-Lynch-Paterson: in an asynchronous system where even one process may crash, no deterministic protocol guarantees consensus with both safety and liveness). Since **total-order broadcast is equivalent to consensus**, and "exactly-once" requires agreement on what was delivered, the same impossibility bites. What EOS actually delivers is **effectively-once processing**: at-least-once delivery + idempotency/dedup so that *observable side effects* happen once. When crossing into an external database, you fall back to Kafka transactions + idempotent DB writes (unique constraints / dedup keys) or the outbox pattern — never a magic "exactly-once" flag.

**Ordering guarantees:**
- **Per-partition / per-key order** is what scalable systems actually promise. Kafka guarantees order *within a partition*; keys hash to partitions so same-key messages stay ordered.
- **Global (total) order** across all partitions requires funneling through a single sequencer/leader — this is consensus, and it does not scale horizontally. This is exactly why diskless designs (KIP-1150) still need a *Batch Coordinator* to assign a global order per partition.
- **Causal order** (Lamport/happens-before) is weaker than total order and cheaper; few mainstream brokers implement it natively.
- Relationship to consensus: a distributed log built via consensus *is* an atomic-broadcast implementation (each log entry = one totally-ordered delivered message), and vice versa — they are inter-reducible.

**Acknowledgement models** you must design for:
- **Auto-ack** (ack on delivery) vs **manual ack** (ack after processing). Auto-ack risks loss on consumer crash.
- **Cumulative ack** (ack offset N implies all ≤ N — Kafka offset commits, Pulsar cumulative) vs **individual/selective ack** (Pulsar shared subscriptions ack one message; needed when processing order ≠ delivery order).
- **Negative ack (nack)** → redelivery.
- **Redelivery + dead-letter queues (DLQ):** after a delivery-count/redelivery limit, route the "poison" message to a DLQ for out-of-band inspection. RabbitMQ quorum queues use `x-delivery-limit`; only actual redeliveries increment the counter.
- **Poison messages:** a message that repeatedly crashes the consumer; without a delivery limit it loops forever. Always bound it.

**Duplicate detection / dedup:**
- **Idempotency keys:** attach a unique ID; consumer keeps a seen-set / dedup table.
- **Deduplication windows:** broker-side dedup over a time or count window (Azure Service Bus duplicate detection records message IDs over a user-defined window; Pulsar and NATS JetStream have message-dedup via producer sequence / `Nats-Msg-Id`).
- Producer-side PID+sequence (above) is broker-enforced dedup within a session.

### 3. Internal architecture of a broker (the meat)

#### 3.1 Storage engine: the append-only commit log

The Kafka design (and everyone who copied it) rests on: **sequential disk I/O + OS page cache + zero-copy**.

- **Segments.** Each partition is a directory; the log is split into **segment files** named by their base offset (`00000000000000000000.log`). Writes go to the *active segment*; when it hits `segment.bytes` (or a time bound) it's rolled and a new active segment opens. Iggy seals segments at 1 GiB and rotates automatically — the same pattern.
- **The record format matters at the byte level.** Kafka's v2 `RecordBatch` has a **fixed 61-byte header** (per KIP-98): `baseOffset` (int64), `batchLength` (int32), `partitionLeaderEpoch` (int32), `magic` (int8 = 2), `crc` (int32, **CRC-32C / Castagnoli**), `attributes` (int16, holding compression codec bits 0–2, timestampType, isTransactional, isControlBatch), `lastOffsetDelta` (int32), `firstTimestamp`/`baseTimestamp` (int64), `maxTimestamp` (int64), `producerId` (int64), `producerEpoch` (int16), `baseSequence` (int32), and a `records` count (int32). The official docs specify the CRC covers "the data from the attributes to the end of the batch" — `partitionLeaderEpoch` is deliberately excluded so the leader epoch can be stamped without recomputing the CRC. Individual records inside the batch then use **varint/zigzag (Protobuf-style)** encoding for `length`, `timestampDelta` (varlong), `offsetDelta`, key length, value length, and headers count, with deltas computed relative to the batch base — this is how Kafka amortizes per-message overhead down to ~7–21 bytes. Compression compresses only the records blob, not the 61-byte header. **Lesson for your Rust broker: define one framed batch format, keep offsets/timestamps as deltas, use a CRC (crc32c crate) over a well-defined range, and keep the on-disk format identical to the wire format so you can zero-copy.**
- **Sparse index.** The `.index` file is a series of **8-byte entries** = 4-byte *relative* offset (target − base) + 4-byte physical file position, **memory-mapped**, and **sparse** (one entry per `log.index.interval.bytes`, default 4096 bytes). Lookup is a binary search for the greatest offset ≤ target, then a short linear scan of the `.log` from that byte position. The `.timeindex` uses **12-byte entries** (8-byte timestamp + 4-byte relative offset). A 1 GB segment with 4096-byte interval → ~262,144 entries × 8 bytes ≈ 2 MB index. **Relative offsets are the trick that keeps entries at 8 bytes.**
- **Log compaction.** Instead of (or in addition to) time/size retention, keep only the latest value per key (tombstones delete). Kafka is explicit that compaction is *non-deterministic in timing* — you may transiently see multiple records (or a tombstone) for a key; do not assume single-copy-at-all-times.
- **Page cache + zero-copy.** Kafka writes land in the OS page cache (write()), and `fsync` is deferred by policy (`log.flush.*`), trading a durability window for throughput. Consumer reads use `sendfile()` (`FileChannel.transferTo`) to go **page-cache → NIC with no user-space copy**. On a caught-up cluster you'll see *zero* read disk I/O. **Caveat that trips people up: enabling TLS disables sendfile zero-copy** because data must be copied to user space for encryption (2–3 extra copies).
- **The Redpanda counter-position.** Redpanda (C++/Seastar) **bypasses the page cache entirely** and manages its own memory and direct async I/O (O_DIRECT, io_uring), because "Redpanda understands its own access patterns better than the OS." Roughly 1 GB/s writes per core; ~2 cores to saturate one NVMe. This is a fundamental fork: *rely on the kernel (Kafka) vs. own the whole stack (Redpanda/Iggy)*.
- **fsync policy is THE durability/latency knob.** Kafka by default relies on **replication** for durability rather than fsync-per-write (fsync on every write costs 2–3 orders of magnitude). NATS JetStream file store, tellingly, **does not fsync every message by default** — it uses a `sync_interval` (default 2 minutes), so a non-replicated JetStream server can lose recently-ACKed messages on OS/power failure. RabbitMQ quorum queues batch Raft-log writes before fsync; an AMQP 1.0 benchmark showed 1,244 vs 15,493 fsyncs (≈83k vs ≈10k msg/s) depending on flow-control batching. **You must decide: fsync-before-ack (durable, slow), replicate-then-ack (Kafka/Raft), or interval-fsync (fast, lossy window).**
- **Tiered / object storage.** Kafka KIP-405 asynchronously tiers *old* segments to S3 while new data stays on local disk + replicated. This is distinct from KIP-1150 (below), which writes new data directly to S3.
- **Write amplification:** replication factor N + compaction rewrites + index/timeindex + tiering all multiply bytes-written; budget for it.

#### 3.2 Broker-side data structures & coordination

- **Topic/partition layout:** partition = unit of parallelism and ordering. Redpanda pins each partition to exactly one CPU **shard**; Iggy shards partitions across CPU-pinned cores. More partitions = more parallelism but more metadata, more open files (Kafka recommends `nofile` ≈ 100,000), and slower rebalances/failovers.
- **Offset management:** Kafka stores committed consumer offsets in an internal compacted topic `__consumer_offsets`. Cursors are cheap; consumer owns its position.
- **Consumer group coordination & rebalancing** — get this right, it's where production pain concentrates:
  - **Eager ("stop-the-world") rebalancing:** on any membership change, *all* consumers revoke *all* partitions, the leader recomputes assignment, everyone resumes. Throughput → 0 during the pause. Confluent's published incremental-cooperative-rebalancing benchmark — a 10-instance stateful Streams app with RocksDB stores under a rolling bounce — measured total pause time of **37,138 ms** under eager.
  - **Incremental cooperative rebalancing (KIP-429, Kafka 2.4+):** consumers keep partitions not being reassigned; only moved partitions are revoked (two rebalance cycles). The same Confluent test measured the pause dropping to **3,522 ms** (~10× better). `CooperativeStickyAssignor` is the production default.
  - **Static membership (KIP-345):** `group.instance.id` avoids rebalances on transient restarts.
  - **KIP-848 (Kafka 4.0):** moves coordination to the broker with server-driven incremental reconciliation, dropping the client-side global sync barrier.

#### 3.3 Replication and consistency

- **Kafka ISR:** each partition has a leader and follower replicas; the **in-sync replica set (ISR)** are replicas caught up within `replica.lag.time.max.ms`. A write with `acks=all` is committed once all ISR members have it, with `min.insync.replicas` enforcing a floor. Only ISR members can be elected leader — unless you enable `unclean.leader.election.enable=true`, which trades consistency for availability (a non-ISR leader can lose committed data). Kafka's key insight vs. majority-vote quorum: ISR tolerates *f* failures with *f+1* replicas (vs *2f+1* for majority vote), and doesn't assume stable storage survives crashes (a recovering replica must fully re-sync before rejoining ISR).
- **KRaft** (KIP-500, sole mode in Kafka 4.0): metadata moved from ZooKeeper into an internal single-partition topic `__cluster_metadata` replicated by a **Raft** quorum of controllers. Key differences from data-plane replication: metadata uses **quorum (majority) commit, not ISR**, records are **fsync'd to disk synchronously** (Raft correctness), and it's **pull-based** (followers fetch, unlike push-based textbook Raft). Brokers are observers that cache metadata in memory → fast failover.
- **Raft-per-unit designs:** Redpanda = one Raft group *per partition* (metadata + data). RabbitMQ quorum queues = one Raft group per queue (via the Ra library; uses Multi-Raft to batch inter-node comms across many queues). NATS JetStream = a Raft group per stream *and* per (non-ephemeral) consumer, plus a meta-group for the API/placement; formal write consistency is **linearizable**.
- **Segregated storage (Pulsar/BookKeeper):** Pulsar brokers are **stateless**; durable storage is Apache BookKeeper. A partition's log is a sequence of **ledgers** (segments) striped across **bookies**; each entry is written to a *write quorum* / acked by an *ack quorum* (ensemble/quorum change on bookie failure). Benefits: unbounded partitions (not limited by one disk), instant broker failover (just reassign ownership, no data copy), independent scaling of compute vs storage. Cost: an extra system (BookKeeper + its journal) and more moving parts. A 2026 arXiv benchmark measured 13–18 ms median publish latency at moderate load, tracing spikes to a BookKeeper `ForceWriteThread` kernel-writeback interaction.
- **Chain replication** (not mainstream in these brokers, but worth knowing): writes flow head→tail, reads from tail; gives strong consistency with simple failover, used in some storage layers.

#### 3.4 Push vs. pull, backpressure, flow control

- **Pull (Kafka, Pulsar consumers, JetStream pull consumers):** consumer fetches at its own rate → natural backpressure, easy rewind/replay, no consumer-overrun. Cost: polling latency, mitigated by **long polling** (`fetch.max.wait.ms` — broker holds the fetch open until data or timeout). LinkedIn's original paper chose pull precisely so consumers can't be flooded and can rewind.
- **Push (core NATS, MQTT delivery, STOMP):** low latency, but the broker must handle slow consumers (buffer, drop, or disconnect). NATS famously protects itself by cutting off "slow consumers."
- **Credit-based flow control (AMQP 1.0, Pulsar):** the receiver grants **link credit** = the number of messages the sender may transmit; sender decrements per transfer, receiver replenishes via `flow` frames. This is *decoupled from acknowledgement* (unlike AMQP 0-9-1's channel `basic.qos` prefetch, which is coupled to acks and applies per-channel). AMQP 1.0 also has a session-level window (frame-count based, like TCP windows). RabbitMQ 4.0 defaults: `max_link_credit` 128, granting more when it drops below 0.5×. Pulsar consumers use a receiver-queue permit system analogously. **Credit-based control lets you throttle one slow queue/link without blocking others on the same connection** — a real advantage over TCP-backpressure-only or per-connection prefetch.

#### 3.5 Fan-out strategies

The core tradeoff:
- **Fan-out write (per-subscriber queue):** copy each message into N per-consumer queues on publish (classic RabbitMQ: exchange → bindings → N queues). Simple per-consumer semantics (individual ack, TTL, DLQ), but write amplification and poor replay; scales badly to many subscribers.
- **Fan-out read (shared log + cursors):** store the message once; each consumer group reads with its own offset/cursor (Kafka, Pulsar, Redis Streams, JetStream). One copy on disk, cheap additional subscribers, replay for free. This is why the log model dominates high-fan-out streaming.
- Pulsar interestingly does fan-out-read at storage (one BookKeeper copy) while supporting queue-like *shared* subscriptions at the serving layer — a hybrid.

**For your Rust broker: default to fan-out-read with per-consumer-group cursors.** It's less code and scales better; add per-subscriber queues only if you need per-message queue semantics.

#### 3.6 Networking layer

- **Event loop / readiness vs completion:** epoll/kqueue are *readiness* interfaces (tell you when you can read, you then syscall); **io_uring** is a *completion* interface (kernel does the I/O and notifies you via a completion queue) using two lock-free ring buffers (SQ/CQ), drastically cutting syscalls and context switches. Iggy migrated from Tokio/epoll to io_uring (via the `compio` runtime) specifically because epoll + work-stealing "can't deliver predictable latencies for block-device I/O"; per the Apache Iggy FAQ/migration blog (Feb 27, 2026), the Tokio→compio migration starting in v0.6.0 delivered "up to 92% better P9999 tail latency and 18% throughput improvement with fsync enabled."
- **Thread-per-core (shared-nothing) vs work-stealing:**
  - **Thread-per-core (Redpanda/Seastar, Iggy):** pin one reactor thread per core; each core exclusively owns a shard of partitions/memory; cross-core coordination is *explicit message passing*, never shared-memory locks. Result: no lock contention, NUMA-local memory, predictable tail latency, no GC. Seastar even asserts no instruction should block >500 µs.
  - **Work-stealing (Kafka JVM, default Tokio):** thread pools share queues and data structures, needing locks; simpler to program, but lock contention + context switches + (for JVM) GC pauses hurt tail latency.
- **Batching & compression:** amortize per-message overhead by batching (producer-side record batches) and compress the batch (gzip/snappy/lz4/zstd). This is often the biggest throughput lever.
- **Protocol framing:** Kafka uses a length-prefixed binary protocol with versioned request/response schemas; NATS uses a tiny text protocol; choose framing that lets you parse without copying.
- **Zero-copy paths:** `sendfile` for disk→socket; `bytes::Bytes` (refcounted, cheaply sliceable buffers) in Rust to avoid copies through your own pipeline; Iggy credits zero-copy (de)serialization with doubling consumer throughput to ~4 GB/s.

#### 3.7 Memory management

- Buffer pools / arena allocation to avoid per-message allocation churn.
- In Rust you sidestep GC entirely (a stated Iggy/Redpanda advantage vs JVM Kafka), but you trade it for careful ownership: `bytes::Bytes` for zero-copy sharing, and be aware of pitfalls like `RefCell` panics across `.await` boundaries (Iggy hit exactly this and redesigned state management, landing on a "shared-something" hybrid using the `left-right` crate for read-heavy shared state).

### 4. Wire protocols (with concrete frames)

**MQTT** (IoT-oriented, TCP, compact binary):
- **QoS 0** (at-most-once): fire-and-forget, single `PUBLISH`.
- **QoS 1** (at-least-once): `PUBLISH` → `PUBACK`; retransmit on reconnect if unacked.
- **QoS 2** (exactly-once *for the transfer*, 4-packet handshake): `PUBLISH` → `PUBREC` → `PUBREL` → `PUBCOMP`.
- **Crucial correctness facts most people get wrong:** MQTT QoS is **hop-by-hop, not end-to-end**; the delivered QoS is `min(publisher QoS, subscriber QoS)`. Retransmission is **only on reconnect**, not on a timer (MQTT 5 forbids resending at other times). QoS 1/2 messages are queued for an offline client **only if it has a persistent session**.
- **Retained messages:** broker stores the *last* message per topic (`retain=1`); new subscribers get it immediately; retained state is **independent of sessions**; publish empty payload with retain=1 to delete.
- **Last Will & Testament (LWT):** registered at CONNECT; broker publishes it if the client drops ungracefully — the canonical "device online/offline" pattern (often combined with retain).
- **Session persistence:** MQTT 3.1.1 `cleanSession=false`; MQTT 5 splits this into **Clean Start** (discard existing session now?) + **Session Expiry Interval** (how long to keep it after disconnect), fixing 3.1.1's unbounded-session-accumulation problem. MQTT 5 adds reason codes, user properties, message/session expiry, shared subscriptions, topic aliases.

**AMQP 0-9-1 vs AMQP 1.0** — *different protocols with the same name*:
- **0-9-1** (RabbitMQ's classic model): broker-centric. Producer → **exchange** → (routing key matched against **bindings**) → **queue(s)** → consumer. Exchange types: direct, topic, fanout, headers. Flow control = per-channel `basic.qos` prefetch, coupled to acks. Message consumed by exactly one consumer per queue (competing consumers).
- **1.0** (OASIS/ISO standard, broker-agnostic): peer-to-peer, layered (transport/session/link/messaging). Frame bodies: `open`, `begin` (session, sets window), `attach` (link, unidirectional), `transfer` (message), `flow` (credit), `disposition` (settle/ack), `detach`, `end`, `close`. **Credit-based link flow control** decoupled from acks (see §3.4). No mandated exchange/queue model. Example flow: attach link → receiver sends `flow{link-credit=10}` → sender sends up to 10 `transfer` frames → receiver `disposition{settled}` → replenish credit with another `flow`.

**Kafka binary protocol:** length-prefixed request/response over TCP, each API keyed (Produce=0, Fetch=1, …) and versioned. Produce carries the v2 RecordBatch (§3.1) verbatim so it lands on disk unchanged; Fetch returns log bytes suitable for sendfile.

**NATS text protocol** (human-readable, tiny): verbs `CONNECT`, `PUB <subject> <#bytes>\r\n<payload>\r\n`, `SUB <subject> <sid>`, `MSG <subject> <sid> <#bytes>`, `PING`/`PONG`, `+OK`/`-ERR`. Subjects are dot-delimited with `*` (one token) and `>` (rest) wildcards. JetStream layers persistence on top by publishing to reserved subjects and adding message headers (e.g. `Nats-Msg-Id` for dedup).

**STOMP:** simple text frames (`CONNECT`, `SEND`, `SUBSCRIBE`, `MESSAGE`, `ACK`, `NACK`) with a `destination` header; JMS-like, easy to implement, common for browser/websocket bridges.

**Redis RESP + pub/sub:** RESP is the length-prefixed Redis wire format. Pub/sub commands: `SUBSCRIBE`/`PSUBSCRIBE` (pattern), `PUBLISH channel msg`; classic Redis Pub/sub is **fire-and-forget, no persistence** (temporally coupled). **Redis Streams** (`XADD`, `XREADGROUP`, `XACK`) add a persistent append-only log with consumer groups and a pending-entries list for at-least-once — a genuine log, unlike Pub/Sub.

**WebSockets / SSE for browser fan-out:** SSE = one-way server→client over HTTP (auto-reconnect, text); WebSocket = bidirectional. Both are the last-mile transport that broker gateways (MQTT-over-WS, STOMP-over-WS) use to reach browsers.

**gRPC streaming:** HTTP/2 multiplexed streams (server-, client-, bi-directional). Not a broker, but a common transport for building one or for point-to-point streaming with backpressure via HTTP/2 flow control.

### 5. Survey of real systems

| System | Storage model | Replication/consensus | Ordering | Fan-out | Delivery | Latency | Ops complexity |
|---|---|---|---|---|---|---|---|
| **Apache Kafka** | Partitioned local-disk log + page cache; KIP-405 tiering | ISR + KRaft (Raft) for metadata | Per-partition | Read (cursors) | at-least / EOS | low (ms), GC jitter | high (but KRaft simplified) |
| **Redpanda** | Local-disk log, **no page cache**, thread-per-core C++ | Raft **per partition** | Per-partition | Read | at-least / EOS (Kafka-compat) | very low, tight p99 | low (single binary) |
| **Apache Pulsar** | **BookKeeper** ledgers (segment-centric), stateless brokers | BK write/ack quorum | Per-partition (+ ordered subs) | Read + shared/queue subs | at-least / effectively-once | low, some tail spikes | high (broker+BK+ZK/etcd) |
| **RabbitMQ** | Queues (mem+disk); Streams (log) | Quorum queues = Raft (Ra); Streams = Osiris | FIFO per queue | **Write** (bindings) + Streams read | at-least (+ confirms) | low | medium |
| **NATS + JetStream** | File/mem stream store | NATS-optimized Raft per stream/consumer | Per-stream (linearizable) | Subject fan-out + durable consumers | core: at-most; JS: at-least | very low | low |
| **Redis Pub/Sub** | none | none | none | fire-and-forget | at-most-once | ultra-low | very low |
| **Redis Streams** | In-mem append log (+ AOF/RDB) | primary-replica (async) | Per-stream | Read (consumer groups) | at-least | ultra-low | low |
| **ZeroMQ** | none (brokerless library) | app-defined | app-defined | app-defined patterns | app-defined | ultra-low | n/a (it's a library) |
| **MQTT brokers** (Mosquitto/EMQX/HiveMQ) | session/retain stores | broker-specific clustering | per-topic | push | QoS 0/1/2 | low | low–medium |
| **Google Cloud Pub/Sub** | managed | managed (multi-region) | optional ordering keys | read (subscriptions) | at-least (exactly-once opt.) | ms | none (managed) |
| **AWS SNS/SQS/Kinesis/EventBridge** | managed | managed | SQS FIFO / Kinesis per-shard | SNS fanout / SQS queue | at-least (FIFO exactly-once-ish) | ms | none (managed) |
| **Azure Service Bus / Event Hubs** | managed (AMQP 1.0) | managed | sessions / per-partition | topics/subscriptions | at-least (+ dup detection) | ms | none (managed) |
| **Apache RocketMQ** | commit-log + consume queues | Raft (DLedger) option | per-queue | read | at-least / txn | low | medium |
| **NSQ** | per-node, memory+disk overflow | none (no replication) | none | push, per-channel fan-out | at-least | low | low |
| **Solace** | appliance/software, persistent | active/standby | per-queue/topic | rich topic hierarchy | at-least / exactly-once | low | medium (commercial) |
| **WarpStream / AutoMQ / Confluent Freight** | **S3-first, diskless** | object store durability + metadata coordinator | per-partition (coordinator-sequenced) | read | at-least / EOS (Kafka-compat) | high (~500 ms p99) or S3E1Z low | very low (stateless agents) |
| **Iggy (Rust)** | local segmented log, io_uring, thread-per-core | single-node (VSR clustering planned) | per-partition | read (consumer groups) | persistent at-least | ultra-low, tight p99 | low (single binary) |
| **Fluvio (Rust)** | local log (SPU), WASM stream processing | replication across SPUs | per-partition | read | at-least | low | low–medium |

**Honest tradeoff reading:** If you want lowest tail latency on-prem, thread-per-core local disk (Redpanda/Iggy) wins. If you want lowest *cloud cost* at scale, S3-first (WarpStream/KIP-1150) wins by eliminating cross-AZ replication fees — at the price of ~500 ms latency (or use S3 Express One Zone for ~4× lower). If you need rich per-message routing/queue semantics (priorities, per-message TTL, complex bindings), RabbitMQ. If you need multi-tenancy + independent storage scaling, Pulsar. For simple ultra-low-latency ephemeral messaging, NATS core or Redis.

### 6. Patterns, anti-patterns, system design

- **Event-driven architecture / event sourcing / CQRS:** store state as an ordered log of events (event sourcing); serve reads from projections built by consumers (CQRS). The broker's log *is* the source of truth. Requires event **versioning** and replayability.
- **Outbox pattern:** to avoid the dual-write problem (write DB + publish message non-atomically), write the event into an `outbox` table *in the same DB transaction* as the business data; a relay/CDC process publishes it. Guarantees the event iff the transaction committed.
- **Change Data Capture (CDC):** tail the DB transaction log (e.g. Debezium reading MySQL binlog / Postgres WAL) and emit change events to the broker — the robust way to feed the outbox and to integrate legacy DBs.
- **Saga pattern:** model a distributed transaction as a sequence of local transactions with compensating actions, coordinated via events — because 2PC across services doesn't scale.
- **Competing consumers:** N consumers in a group share a partitioned/queued workload for horizontal throughput; combine with idempotency because you get at-least-once.
- **Claim-check:** for large payloads, put the blob in object storage and send only a reference through the broker (keeps messages small, avoids broker bloat).
- **Schema registry & evolution:** centralize Avro/Protobuf/JSON-Schema; enforce **compatibility** (backward: new schema reads old data; forward: old readers read new data; full: both). Avro's reader/writer schema resolution is the classic mechanism; Protobuf uses field numbers/reserved fields; add fields with defaults, never repurpose tags. This is what lets producers and consumers deploy independently.
- **Partitioning / key design:** the key determines the partition (hash) and therefore ordering scope and load distribution. **Hot partitions** happen when a key is too popular (e.g. a whale tenant) or keys skew — mitigate with composite keys, salting, or explicit partitioners. Over-partitioning wastes metadata/files and slows rebalances; under-partitioning caps parallelism.
- **Consumer lag monitoring:** lag = latest offset − committed offset; the single most important health metric. Rising lag = consumers falling behind = imminent SLA breach.
- **Common production failure modes:** poison-message loops without a DLQ; rebalance storms from bad `session.timeout.ms`/`max.poll.interval.ms`; unclean leader election losing data; hot partitions; running out of disk (bricking a broker) or file descriptors; unbounded MQTT session/retained-message accumulation; slow-consumer disconnects in push systems; fsync-window data loss on power failure in interval-fsync configs.

### 7. Building one yourself (Rust)

**Minimal correct broker checklist:**
1. **Topic registry** — concurrent map of topic → partition set → segment list; metadata persisted.
2. **Append-only segment writer** — batch, CRC, append to active segment, roll at size/time, control fsync policy.
3. **Offset index** — sparse `(relative_offset, file_pos)` mmap, binary search; optional time index.
4. **Subscriber fan-out loop** — per-consumer-group cursor; long-poll fetch; sendfile/`Bytes` zero-copy read path.
5. **Consumer group coordinator** — membership, heartbeat, partition assignment (start with sticky), offset commit store.
6. **Replication** — start single-node; add Raft (or VSR, Iggy's chosen path) per partition later.

**Concrete code sketches (illustrative pseudocode):**

```rust
// 1. Topic registry
struct Registry { topics: DashMap<String, Arc<Topic>> }
struct Topic { partitions: Vec<Arc<RwLock<Partition>>> }   // index = partition id
struct Partition { segments: Vec<Segment>, active: usize, next_offset: u64 }

// 2. Append-only segment writer (batch already framed: 61B header + varint records)
fn append(p: &mut Partition, batch: &Bytes) -> io::Result<u64> {
    let seg = &mut p.segments[p.active];
    if seg.size + batch.len() as u64 > SEGMENT_BYTES { roll(p); }
    let pos = seg.log.stream_position()?;
    seg.log.write_all(batch)?;                 // page cache; fsync per policy
    if p.next_offset % INDEX_INTERVAL == 0 {   // sparse: default every 4096B
        seg.index.append((p.next_offset - seg.base_offset) as u32, pos as u32);
    }
    let base = p.next_offset;
    p.next_offset += records_in(batch);
    Ok(base)
}

// 3. Offset index lookup: binary search mmap of 8-byte entries -> byte pos
fn find_position(seg: &Segment, target: u64) -> u64 {
    let rel = (target - seg.base_offset) as u32;
    let entries: &[(u32,u32)] = seg.index.as_slice();       // memmap2
    let i = entries.partition_point(|&(o,_)| o <= rel).saturating_sub(1);
    entries[i].1 as u64                                       // then linear-scan .log
}

// 4. Subscriber fan-out loop (fan-out READ: one copy, per-group cursor)
async fn fetch(p: &Partition, mut cursor: u64, max_wait: Duration) -> Bytes {
    if cursor >= p.next_offset { notify.wait_timeout(max_wait).await; } // long poll
    let seg = segment_containing(p, cursor);
    let pos = find_position(seg, cursor);
    zero_copy_read(&seg.log, pos)   // sendfile equivalent / Bytes slice
}

// 5. Consumer-group coordinator (sticky assignment sketch)
fn assign(members: &[MemberId], parts: &[PartId], prev: &Assignment) -> Assignment {
    // keep prev[m] where still valid (sticky); redistribute only orphaned parts
    // cooperative: revoke-then-rejoin only the moved partitions
}
```

**Existing Rust implementations to study:**
- **Iggy** (Apache incubating): full persistent streaming server; Stream→Topic→Partition→Segment model mirroring Kafka; TCP/QUIC/WebSocket/HTTP; migrated Tokio→io_uring/compio; thread-per-core shared-nothing; zero-copy serialization; clustering via Viewstamped Replication planned; deterministic simulation harness being built. The best end-to-end Rust reference.
- **Fluvio** (InfinyOn): Kafka-like SPUs with WASM-based SmartModules for in-stream transforms.
- **rskafka:** minimal async Kafka *client* (no consumer groups) — good for learning the Kafka wire protocol.
- **async-nats:** official NATS client — clean async protocol implementation.
- **rumqttd:** a full MQTT broker in Rust — study its session/retain/QoS handling.

**Runtime & crates:**
- **Runtime:** `tokio` (work-stealing, huge ecosystem, easiest) vs **`glommio`/`monoio`/`compio`** (thread-per-core, io_uring, best tail latency, smaller ecosystem). Iggy's arc — Tokio → monoio experiment → compio — is instructive: thread-per-core + io_uring is where the latency wins are, but it constrains your library choices and forces `!Send` state design.
- **Zero-copy:** `bytes::Bytes` for refcounted buffers; `memmap2` for mmap'd index/segment files.
- **Checksums/encoding:** `crc32c`, `integer-encoding` (varint), or hand-rolled zigzag.
- **Lock-free / concurrency:** `crossbeam` (channels, epoch GC), `arc-swap`/`left-right` for read-mostly shared state (Iggy uses `left-right`), `parking_lot` locks off the hot path only.
- **Consensus:** `openraft` or `raft-rs` if you go Raft.

**Testing correctness — do this from day one:**
- **Deterministic Simulation Testing (DST):** pioneered by **FoundationDB**, adopted by **TigerBeetle**, WarpStream, RisingWave. Make the whole system deterministic (single-threaded logical execution, injected clock + RNG), then run it against a simulator that injects network faults, disk faults, crashes, and reorderings — with a seed so any failure reproduces exactly. In Rust, **`madsim`** (used by RisingWave) overrides `getrandom`/`clock_gettime`/network at the libc/crate level; **`turmoil`** (Tokio) simulates hosts/network/time; `mad-turmoil` (S2.dev) blends them. This is how FoundationDB "found all the bugs."
- **Jepsen-style** black-box testing for linearizability/consistency claims under partitions.
- Property tests for the log/index invariants; fuzz the wire parser.

### 8. Research & recent developments (2024–2026)

- **Diskless / S3-first is the dominant theme.** **KIP-1150 (Diskless Topics)** was **accepted by the Apache Kafka community on March 2, 2026** — Aiven reports the vote "passed with overwhelming support of 9 binding votes and 5 non-binding ones." It is a "meta/motivational" KIP establishing direction, not yet production code — the first time the community endorsed object storage as the primary data layer. It introduces per-topic choice between classic (low-latency local disk) and diskless (low-cost S3) topics, a **leaderless** write path (any broker serves any partition), **Shared Log Segment Objects** (one S3 object bundles many partitions' data to dodge the small-file problem), and a **Batch Coordinator** that sequences batches and assigns global per-partition offsets *after* the write (offsets are injected on read). Follow-ups: KIP-1163 (core), KIP-1164 (Batch Coordinator), KIP-1165 (compaction). Aiven's open prototype is **Inkless** (uses Postgres as the batch coordinator + Infinispan cache).
- **The economics driving it:** in cloud Kafka, cross-AZ replication + networking is frequently cited as the dominant cost. Stanislav Kozlovski (2 Minute Streaming) frames it as "a well-optimized Kafka deployment has 80% of its costs coming from cross-AZ networking. In other words, $176k out of a $215k annual bill can go to networking." **Important nuance:** Kozlovski's own follow-up analysis disputes the magnitude, finding self-hosted Kafka on AWS only ~32% dearer than WarpStream once KIP-392 Fetch-From-Follower is applied — so treat the "80%" as an unoptimized upper bound, not a universal. Object storage also costs ~$0.02/GiB/month vs ~$0.39–0.48 for triple-replicated EBS/NVMe (~18–24×). One published estimate: a 1 GiB/s cluster dropping from ~$1.8M to ~$20K/year. Trade-off: end-to-end p99 rises to ~500 ms (S3 Standard); **S3 Express One Zone** recovers ~4× (single-digit-ms access) but needs quorum writes across directory buckets to survive AZ loss. **Note this benefit is AWS/GCP-specific: in Azure, inter-zone networking is free, so diskless does not materially cut the bill.**
- **The design fork (Jack Vanlightly's framing):** "revolutionary" (stateless serving layer + central coordination, à la WarpStream — three competing KIPs 1150/1176/1183 all attack cross-AZ cost) vs. evolutionary tiering. WarpStream's specifics worth stealing: **zone-aligned producer/consumer routing** (zero cross-AZ), leaderless agents, a per-zone shared read cache ("distributed mmap") so agents don't each re-download the same S3 object, and background compaction of small objects into large ones. **AutoMQ**'s contrast: keep Kafka's leader-based model but swap the storage layer for a WAL that absorbs writes at low latency (EBS/S3) then flushes to S3 — ~500 ms with S3 WAL, sub-10 ms with regional EBS WAL.
- **Shared-nothing / thread-per-core** continues to spread (Redpanda, Iggy, ScyllaDB/Seastar lineage), and **io_uring** is now the assumed substrate for new high-performance Rust/C++ brokers.
- **Where the field is heading:** separation of compute from storage as the default cloud posture; object storage as source of truth with a small stateful coordinator; per-topic latency/cost knobs; and correctness increasingly guaranteed by deterministic simulation rather than only integration tests.

## Recommendations

**Staged plan for building your Rust broker:**

1. **Stage 0 — single-node log core (weeks).** Implement topic registry, segmented append-only log with a defined batch format (CRC-32C, delta-encoded records), sparse mmap offset index, and a pull-based fetch API with long polling. Use `tokio` + `bytes::Bytes` first; correctness before latency. **Benchmark gate:** sustained sequential write ≥ your target MB/s with fsync-on-interval; correct offset lookup under binary search.
2. **Stage 1 — consumers & groups.** Add per-consumer-group cursors (fan-out-read), offset commit persistence, and a sticky/cooperative assignment coordinator. **Gate:** a consumer can crash and resume without loss or (with idempotency) harmful duplication; rebalance doesn't stop the world for unaffected consumers.
3. **Stage 2 — durability semantics.** Make the fsync/replication policy explicit and configurable: choose *replicate-then-ack* (add Raft-per-partition via `openraft`) once you go multi-node. Add producer idempotence (PID + epoch + per-partition sequence, retain last N batches). **Gate:** documented, measured durability window; no committed-message loss under single-node-kill in DST.
4. **Stage 3 — correctness hardening.** Stand up **deterministic simulation testing** (madsim/turmoil) with fault injection *before* adding clustering complexity; fuzz the wire parser; property-test log/index invariants. **Gate:** seeds reproduce injected-fault failures deterministically.
5. **Stage 4 — performance.** Only now consider thread-per-core + io_uring (glommio/monoio/compio), sendfile/zero-copy read path, and batching/compression. **Gate:** measure p99/p9999, not just throughput; watch for `!Send`/`RefCell`-across-`.await` refactors (Iggy's lesson).
6. **Stage 5 — cloud economics (if cloud-targeted).** Evaluate an S3-first tier: zone-aligned routing, objects bundling multiple partitions, a metadata/coordinator service for sequencing, background compaction. **Threshold to pursue this:** model your *own* cross-AZ traffic first (the "80%" is an unoptimized upper bound); if projected cross-AZ replication would dominate infra cost on AWS/GCP, diskless pays off; on Azure (free inter-zone) or for sub-10-ms-latency needs, stay local-disk.

**Decision thresholds that should change your mind:**
- Need **global total order** → you need a single sequencer/consensus; accept it won't scale horizontally, or redesign to per-key order.
- Need **sub-ms p99** → local disk + thread-per-core; do *not* go S3-first.
- Need **lowest cloud $ at GB/s scale** → S3-first, accept ~500 ms (or S3 Express for ~4× less).
- Need **rich per-message routing / priorities / per-message TTL** → a queue model (RabbitMQ-style), not a pure log.
- Many subscribers per topic → **fan-out-read**, never fan-out-write.

## Caveats

- **Vendor blogs oversell.** Cost-reduction figures come largely from vendors with obvious incentives and vary wildly by workload: WarpStream states "Character.AI built its real-time trust and safety and experimentation pipelines on WarpStream, slashing streaming costs by 85–90%," and lists ShareChat saving ~60% and Robinhood ~45% — but these are self-reported case studies. Even the headline "80% of cost is cross-AZ" is contested: Kozlovski's own follow-up finds the gap shrinks to ~32% once Kafka's Fetch-From-Follower (KIP-392) is enabled. Treat all specific percentages as workload-dependent marketing until you model your own traffic. The Azure "inter-zone free" caveat materially changes the math.
- **"Diskless" is a misnomer.** Even KIP-1150 brokers use local disk for KRaft metadata, caching, and staging; "direct-to-S3" is the accurate term.
- **KIP-1150 is directional, not shipped.** As of its March 2026 acceptance, exactly-once, transactions, compaction, and the latency gap under failure are explicitly still open; don't assume production-readiness.
- **"Exactly-once" claims** should always be read as *effectively-once within a scope*; verify whether a vendor means producer-idempotence, transactional EOS, or genuine end-to-end (the last is impossible in the strict model).
- **Latency numbers** for Iggy ("5M msgs/sec," "sub-ms p99") come from tuned benchmarks and vendor/community posts; real production behavior needs your own validation. The "92% P9999 improvement / 18% throughput" figure is Iggy's own reported result for the compio/io_uring migration with fsync enabled.
- **Some sources here are secondary** (Medium deep-dives, engineering blogs, DeepWiki source summaries). Where it matters — record format, index layout, KIP semantics, protocol frames — I've grounded claims in primary docs (kafka.apache.org message-format docs, KIP-98, OASIS AMQP 1.0 spec, NATS docs, RabbitMQ docs, the Eugster and Kreps papers) and the Kafka Java/Scala source, but confirm exact constants against the current version of each system's source before you depend on them.
- **The Pulsar latency arXiv figure** (13–18 ms median) is from one enterprise deployment at moderate load, not a universal characterization.
