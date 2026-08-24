<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 23 — DynamoDB Data Plane

> A key-value store with a hash key is a weekend. DynamoDB's hard parts are the
> ones that actually page you: you **cannot query without the partition key**, and
> that isn't an API quirk — it's physics, because the key decides where the item
> physically lives. A secondary index is a second copy of your data that you pay to
> maintain and that *lags*. A conditional write is the only thing standing between
> two concurrent updaters and a silently lost write. And a table provisioned for
> 10,000 writes/sec will still throttle you if one partition key is taking all of
> them. This project builds that data plane from the item up.

**Explicitly out of scope: distribution.** No rings, no gossip, no quorums, no
replication — those are projects **07** (consistent hashing, membership,
replication) and **09** (Raft, linearizable reads). Here there is exactly one node,
and every hard problem is still there.

## What it does (the easy part)
- `PutItem` / `GetItem` / `DeleteItem` / `UpdateItem` on an item addressed by its
  primary key (partition key, optionally + sort key).
- `Query` — every item under one partition key, ordered by sort key, with range
  conditions. `Scan` — the whole table, and you'll feel why that's the last resort.
- Secondary indexes: a **GSI** (its own partition key, eventually consistent) and an
  **LSI** (same partition key, alternate sort key, strongly consistent).
- Conditional writes, atomic counters, and all-or-nothing transactions.
- Every response reports **consumed capacity**; over-budget callers are throttled.
- `GET /streams/{table}` — the ordered per-partition change log of the table.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. The item model & primary key — *the key is placement, not an index*
An item is an attribute map; a table is items addressed by a **partition key** and
an optional **sort key**. The partition key is not a lookup optimisation you can
route around — it decides which partition the item is stored in, which is why
`Query` demands it and `Scan` exists as the expensive apology. Build the table, the
key encoding, and the two read paths in `src/dynamodb_core/table.py`.

**Done when ALL true:**
- [ ] `PutItem`/`GetItem`/`DeleteItem` round-trip an item addressed by its **full** primary key, and a partial key is rejected rather than guessed at.
- [ ] On a composite-key table, `Query` by partition key alone returns every item under it **ordered by sort key**, and supports range conditions (`begins_with`, `between`, `>`, `<`).
- [ ] A `Query` without a partition key is **rejected**; `Scan` is the only keyless read, and its cost is observably proportional to the **table**, not the result.
- [ ] Items sharing a partition key are **physically co-located** — a `Query` reads one partition's worth of data, provably not the whole table.
- [ ] The per-item size cap is enforced **before** storage, with a distinct error.
- [ ] Attribute types (`S`/`N`/`B`/`BOOL`/`NULL`/`L`/`M`/`SS`/`NS`) round-trip losslessly, and numbers keep their precision (a float is not "close enough").

**Proof:** unit tests for key encoding, sort-key ordering (including negative and
large numbers) and range conditions; a test asserting `Query` touches one
partition; `docs/23-design.md` records the key encoding you chose and why sort
order survives it.

*Concept to internalize:* why the partition key decides physical placement, and why
"just add an index" is a storage decision rather than a query-planner one.

### V2. Secondary indexes — *an index is a second table you pay for*
A **GSI** re-partitions your data under a different key: its own key space, its own
capacity, and — because it is maintained after the fact — its own *lag*. An **LSI**
keeps the partition key and only changes the sort key, so it lives in the same
partition and can be strongly consistent. Build index maintenance and the index read
path in `src/dynamodb_core/indexes.py`.

**Done when ALL true:**
- [ ] Writing to the base table makes the item queryable by a **GSI's** key, under that GSI's own partition/sort key.
- [ ] A projection is honoured: `KEYS_ONLY` / `INCLUDE` / `ALL` return exactly the attributes promised, no more.
- [ ] The GSI is **eventually consistent** — a test observes a window where base and GSI disagree, and then observes convergence. The window is bounded and documented.
- [ ] An **LSI** read is strongly consistent: an item written and immediately read back through the LSI is there.
- [ ] Updating or deleting a base item leaves **no orphaned index entry**, including when the update *changes* the indexed attribute (the old entry must go).
- [ ] An item missing the GSI's key attribute is simply **absent** from that index rather than erroring — sparse indexes work.

**Proof:** tests for projection contents, sparse indexing, the change-the-key case
and orphan-freedom; a convergence test measuring the lag window; `docs/23-design.md`
records sync-vs-async maintenance and the write amplification it costs.

*Concept to internalize:* why a GSI cannot be strongly consistent while an LSI can,
and why every index multiplies your write cost.

### V3. Conditional writes & atomic updates — *compare-and-set is the whole story*
Two callers read an item, both modify it, both write it back: one update is gone and
nobody got an error. A **ConditionExpression** is the fix, and it is the only
concurrency primitive a store like this offers cheaply. Build expression evaluation,
`UpdateExpression` application and transactions in
`src/dynamodb_core/conditions.py`.

**Done when ALL true:**
- [ ] `attribute_not_exists(pk)` makes a `PutItem` succeed exactly once — the second attempt fails with a **distinct, non-retryable** condition error (not a generic 500).
- [ ] Under N concurrent version-checked updates, **exactly one** wins per version and the losers are told; no update is silently lost.
- [ ] An **atomic counter** incremented by N concurrent writers lands on exactly N — no read-modify-write race.
- [ ] `UpdateExpression` supports `SET`/`REMOVE`/`ADD` on nested paths, and a failed condition **changes nothing** while still costing capacity (both observable).
- [ ] A transaction over multiple items **fully applies or not at all**, including when one item's condition fails — a partially applied transaction is never visible to a reader.
- [ ] Conflicting concurrent transactions do not deadlock: one aborts with a distinct error.

**Proof:** concurrency tests driving real parallel writers (not sequential calls);
an atomic-counter test; a transaction test asserting no partial state is ever
observed mid-flight; `docs/23-design.md` names your transaction protocol.

*Concept to internalize:* optimistic concurrency vs. locking, and why "read, modify,
write" is a bug in every distributed system that doesn't have compare-and-set.

### V4. Provisioned throughput & hot partitions — *the table is fine; you are throttled*
Capacity in DynamoDB is not a table-level budget — it is spread across partitions,
and a single overloaded key throttles while the table sits near-idle. This is the
most common DynamoDB production incident there is, and you can only really
understand it by building the governor. Build the capacity model and the
per-partition limiter in `src/dynamodb_core/throughput.py`.

**Done when ALL true:**
- [ ] Every operation reports **consumed capacity**, and the number matches a documented cost model derived from item size (and a strongly-consistent read costs more than an eventually-consistent one).
- [ ] Exceeding capacity throttles with a **distinct, retryable** error — clearly different from a condition failure.
- [ ] A workload concentrated on **one partition key** is throttled *while total table utilisation stays low* — the hot-partition failure, reproduced on demand.
- [ ] **Burst capacity** lets a short spike through; a sustained one does not.
- [ ] Spreading the identical load across many partition keys **removes** the throttling — measured, not asserted.
- [ ] A GSI has its **own** capacity: saturating the index throttles index writes without stopping unrelated base-table reads.

**Proof:** a bench scenario for each of uniform / skewed / single-key workloads with
throttle counts and table utilisation side by side; `docs/23-design.md` records the
cost model, bucket granularity and burst policy.

*Concept to internalize:* why capacity is per-partition, why key design **is** a
capacity decision, and what adaptive capacity can and cannot rescue.

### V5. Streams — *the ordered change log that makes the table integrable*
A store nobody can subscribe to is a dead end. A stream is the table's change log:
ordered **within a partition key**, unordered across them, retained for a window,
and read through a shard iterator. It is also the seam the next project plugs into.
Build it in `src/dynamodb_core/streams.py`.

**Done when ALL true:**
- [ ] Every mutation appends **exactly one** record with the correct event type (`INSERT` / `MODIFY` / `REMOVE`) — including a delete of a non-existent item appending **nothing**.
- [ ] Records for one partition key are **strictly ordered**; the ordering guarantee across different keys is documented (and no stronger than what you actually provide).
- [ ] A consumer can read from **`TRIM_HORIZON`** and replay the whole retained window, or from **`LATEST`** and tail only new changes.
- [ ] The view type is honoured: `NEW_IMAGE` / `OLD_IMAGE` / `NEW_AND_OLD_IMAGES` / `KEYS_ONLY` carry exactly what they promise, and a `MODIFY` carries both images.
- [ ] Records past the retention window are **trimmed**, and an iterator pointing into trimmed data fails with a distinct error rather than silently skipping.
- [ ] A write is not acknowledged until its stream record is durable — a crash mid-write never leaves a committed item with no record (or vice versa).

**Proof:** an ordering test under concurrent writes to the same and different keys;
a replay-from-trim-horizon test; a crash-injection test for the atomicity of
item+record; `docs/23-design.md` records the retention window and shard model.

*Concept to internalize:* change data capture as an integration seam, and why
per-key ordering is both the strongest guarantee a partitioned store can cheaply
make and exactly enough for a downstream consumer.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] The HTTP API mirrors the **DynamoDB operation shape** (one endpoint, operation selected by a target header, JSON request/response) so the mental model transfers to the real thing.
- [ ] Errors use DynamoDB's **named exception types** (`ConditionalCheckFailedException`, `ProvisionedThroughputExceededException`, `ResourceNotFoundException`, `ValidationException`) with correct status codes and a documented retryable/non-retryable split.
- [ ] **Pagination** via `LastEvaluatedKey` / `ExclusiveStartKey`: a paged `Query` or `Scan` returns every item exactly once across pages, even with concurrent writes.
- [ ] A **`ConsistentRead`** flag is honoured on reads that can offer it, and rejected on those that can't (a GSI).

### Storage & durability
- [ ] Writes are **durable before acknowledgement** (a write-ahead log), and the store recovers cleanly from a kill -9 mid-write.
- [ ] Item + index entries + stream record for one mutation apply **atomically** — recovery never surfaces a half-applied write.
- [ ] The on-disk format is **versioned**, so a future change can be detected rather than silently misread.

### Security
- [ ] Writes and admin operations require a signed/authenticated request; credentials never appear in logs or errors, and the comparison's timing-safety is a documented decision.
- [ ] **Input validation:** item size, attribute count and depth, key length and charset are all bounded — a pathological item cannot blow the node's memory budget.

### Observability
- [ ] A `tracing`-equivalent span per request with a request id, recording the operation, the table, and the **consumed capacity**.
- [ ] Metrics at `/metrics`: **consumed RCU/WCU, throttled request count, per-index write amplification, stream lag, and item/partition counts.**
- [ ] The **key distribution across partitions is observable** (per-partition item count and request rate) — you can *see* a hot partition forming before it throttles you.

### Python & runtime
- [ ] **`pyright` strict passes clean** — every `# type: ignore` carries a comment justifying it.
- [ ] **No blocking call on the event loop:** runs clean under `PYTHONASYNCIODEBUG=1`; disk I/O and any CPU-bound encoding are moved off the loop *deliberately*, with the reason recorded.
- [ ] **Bounded pools and buffers sized on purpose:** the stream buffer and any write queue have explicit limits, tuned together with the expected write rate.
- [ ] **Graceful shutdown** flushes the WAL and drains in-flight requests on SIGTERM — no acknowledged write is lost on restart.
- [ ] **The GIL's cost is measured, not assumed:** the contended-write benchmark states whether throughput scales with concurrency, and if not, why.

---

## Cross-cutting scale skills (every project carries these)
- **Backpressure & bounds:** capacity limiting is the backpressure; the stream buffer
  and WAL are bounded, and a slow consumer cannot grow memory without limit.
- **Graceful shutdown:** flush the log, drain in-flight requests, close the stream cleanly.
- **Benchmarks with numbers:** `bench/` + `docs/23-benchmarks.md` — throughput by
  workload skew, and the write amplification cost of each added index.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load test lives in `bench/`, the
   numbers in `docs/23-benchmarks.md`.
3. `docs/23-design.md` records the four decisions the SPEC grades: **key encoding &
   partition layout, index maintenance (sync vs async + projection), condition
   evaluation & transaction protocol, and the capacity model & throttle algorithm.**
4. `make verify` is green — `ruff` clean, `pyright` **strict** with zero errors, and
   `pytest` passing; no `NotImplementedError` remains on a checked path.
5. A **profile** is committed: a `py-spy` flamegraph and a `memray` run in
   `docs/23-benchmarks.md`, naming the top bottleneck.

## 🐉 Boss fight — The Hot Shard

> Black Friday. Your table is provisioned for 10,000 writes/sec and the dashboard
> says you are at 8% utilisation. Every write is being throttled anyway, because one
> product's item — one partition key — is taking all of them. The table is fine. The
> table has always been fine. The *shard* is on fire, and the only thing that puts it
> out is a key design you should have chosen months ago.

**Arena:** `bench/` load generator against `make run` at a **fixed provisioned
capacity**, over four workloads: uniform keys, Zipfian-skewed keys, a single hot key,
and a GSI-heavy write mix. Report throttle counts next to table utilisation for each.

**The boss falls when ALL true:**
- [ ] ≥ **20,000 writes/sec** sustained for 60s on the uniform-key workload with capacity provisioned to match.
- [ ] **p99 ≤ 15ms** for `GetItem` by full primary key during that run.
- [ ] The single-hot-key workload throttles at the documented **per-partition** ceiling while table-wide utilisation stays **< 15%** — the hot shard reproduced with numbers, not anecdote.
- [ ] Re-spreading that identical load across ≥ **100** partition keys raises accepted throughput by ≥ **20×**.
- [ ] Measured **write amplification** on the GSI-heavy mix matches the index count within **±20%** of your cost model's prediction.
- [ ] A stream consumer keeps up at the sustained write rate with **lag ≤ 500ms**, and replay from `TRIM_HORIZON` loses no record.

**Proof:** methodology + numbers in `docs/23-benchmarks.md` (hardware noted, commands
reproducible via `bench/`). Where CPython cannot reach a target, the **gap and its
cause** — GIL contention, GC pauses, allocation, or a blocking call on the loop — is
the finding, and it is written down rather than rounded away.

## Suggested order of attack
1. `PutItem`/`GetItem` over a plain dict keyed by (partition key, sort key). No indexes, no capacity.
2. Add sort-key ordering, `Query` range conditions, and the "no partition key, no Query" rule (V1).
3. Add conditional writes, atomic updates and transactions (V3) — **before** indexes, because index maintenance has to respect them.
4. Add GSI/LSI maintenance, projections and the lag window (V2).
5. Add the capacity model and per-partition token buckets, then reproduce a hot partition on purpose (V4).
6. Add streams and prove per-key ordering under concurrent writers (V5).
7. Add auth, metrics, partition-distribution observability; benchmark, document, tune.

## Run it
```bash
make setup && make sync    # .env from .env.example, then the venv
make run                   # single node on :8000

# put an item, then read it back
curl -XPOST localhost:8000/ -H 'X-Target: PutItem' \
  -d '{"TableName":"orders","Item":{"pk":{"S":"cust#1"},"sk":{"S":"order#1"}}}'
curl -XPOST localhost:8000/ -H 'X-Target: Query' \
  -d '{"TableName":"orders","KeyConditionExpression":"pk = :p",
       "ExpressionAttributeValues":{":p":{"S":"cust#1"}}}'
```
