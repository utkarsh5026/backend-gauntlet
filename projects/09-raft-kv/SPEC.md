<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 09 — Distributed KV Store with Raft

> A replicated key-value store sounds like "a HashMap, but on three machines."
> The trap is the word *replicated*: the moment more than one node can accept a
> write, you have to answer "which write won?", "what if a node was offline for
> it?", and "what if two nodes both think they're in charge?" — while machines
> crash and the network drops, delays, and reorders messages at will. **Raft** is
> one carefully-proven answer: elect a single leader, funnel every write through
> its append-only log, and only call an entry *committed* once a majority has it.
> This project builds that consensus core from scratch — the part you'd normally
> reach for `openraft` or etcd to provide. It's Tier 4 because the hard parts
> (leader election that never produces two leaders, log repair after a partition,
> a commit rule that never loses an acknowledged write) are exactly the parts a
> library hides, and getting them subtly wrong looks like it works until the one
> partition that corrupts your data.

## What it does (the easy part)
- `PUT /kv/{key}` `{value}` → a replicated write; succeeds only via the leader.
- `GET /kv/{key}` → a **linearizable** read (served by a confirmed leader).
- `DELETE /kv/{key}` → a replicated delete.
- `GET /status` → this node's role, term, leader, and commit/apply progress.
- Internal `/raft/*` endpoints carry `RequestVote` / `AppendEntries` /
  `InstallSnapshot` between nodes. A cluster is N processes sharing one `PEERS`
  map, each with a distinct `NODE_ID`.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the
> system must do*, never *how*; figuring out the how is the entire point. A box
> only flips to ✅ when its Proof exists. Consensus is unusually easy to *appear*
> correct while being wrong, so the Proofs here lean hard on adversarial tests
> (partitions, crashes, restarts), not just happy-path round-trips.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Leader election — *one leader per term, or none*
Every node starts as a **follower**. Hearing nothing from a leader within a
*randomized* election timeout, it becomes a **candidate**: bumps the term, votes
for itself, and asks every peer for a vote. A majority makes it leader; a split
vote makes everyone retry at a fresh random interval. Two rules keep it *safe*,
not merely live: a node grants **at most one vote per term** (and remembers that
across a crash), and it refuses any candidate whose log is **less up-to-date**
than its own. Build it in `src/election.rs`.

The trap is that "elect a leader" is easy; "**never** elect two leaders for one
term, even across crashes and partitions" is the whole job. A node that forgets
its vote after a reboot, or an over-eager up-to-date check, silently permits two
leaders — and two leaders means divergent writes.

**Done when ALL true:**
- [ ] With all nodes healthy, the cluster **elects exactly one leader** and the others become followers — observable via `/status` converging on one `leader`.
- [ ] **At most one leader exists per term** — no two nodes report `role=leader` for the same term, ever, in any test run.
- [ ] A node grants **at most one vote per term**, and that vote **survives a restart** (a rebooted node does not vote again in a term it already voted in).
- [ ] A candidate whose log is **less up-to-date** (older last-entry term, or same term but shorter) **loses** — it cannot win a quorum.
- [ ] **Randomized timeouts**: a split vote resolves on its own within a few election cycles rather than deadlocking — repeated ties are self-correcting.
- [ ] Killing the leader triggers a **new election** and a new leader emerges within a bounded time; the old term is never reused.

**Proof:** a multi-node integration test asserting a single leader converges;
a "one-leader-per-term" invariant check across a randomized/partitioned run; a
restart test proving a persisted vote is honored (no double-vote); a
leader-kill test showing re-election. `docs/09-design.md` states the persisted
state and the up-to-date comparison.

*Concept to internalize:* why the term is the linchpin, why the up-to-date rule
is what protects committed history, and why randomized timeouts (not cleverness)
are Raft's answer to split votes. **Stretch:** pre-vote to avoid term inflation
from a flapping/partitioned node.

### V2. Log replication & commit — *a majority, or it didn't happen*
The leader is the only writer. A client command becomes a log entry, which the
leader pushes to followers via `AppendEntries`. Each such message names the
`(prev_log_index, prev_log_term)` the new entries must follow; a follower accepts
only if it holds that exact entry, else it rejects and the leader walks back and
retries — repairing a diverged tail. An entry is **committed** once a majority
stores it, and only then is it applied to the state machine. Build it in
`src/replication.rs`.

The subtle, data-losing trap is the commit rule: a leader may advance its commit
index only for an entry **from its own term**. Committing a previous term's entry
by replica-count alone (Raft §5.4.2) can erase an acknowledged write — which is
why a fresh leader appends a no-op in its term first.

**Done when ALL true:**
- [ ] A write acknowledged to the client is present, in the **same order**, on a **majority** of nodes — and readable after the leader that took it is killed.
- [ ] **Log Matching holds**: if two logs contain an entry with the same index and term, their entries are identical at every preceding index (checked, not assumed).
- [ ] A follower with a **conflicting tail** (from a deposed leader) has it **overwritten** to match the leader — divergence is repaired, never appended past.
- [ ] Commit respects the **current-term rule**: an entry from a prior term is *not* considered committed on replica-count alone — verifiable by a constructed §5.4.2 scenario that does not lose a committed write.
- [ ] Every node applies committed entries to its state machine **in index order, exactly once**, so all nodes reach **identical** state.
- [ ] A write attempted on a **non-leader** is refused with a redirect to the current leader (not silently dropped, not served locally).

**Proof:** a replication test (write, kill leader, read survives on a new
leader); a Log-Matching property test; a conflicting-tail repair test; a
§5.4.2 regression test; a determinism test (identical applied state across
nodes after a mixed workload). `docs/09-design.md` records the commit rule and
the no-op-on-election decision.

*Concept to internalize:* quorum intersection (why any two majorities overlap,
so a committed entry survives any single leader change), the consistency check
as the log-repair mechanism, and why commit is a *majority* fact, not a *leader*
fact. **Stretch:** batching + pipelining `AppendEntries` for throughput without
breaking ordering.

### V3. The replicated state machine + linearizable reads — *the log becomes state*
Consensus produces an agreed, ordered command sequence; this is what consumes it.
`apply` folds each committed command into the KV map, in order, exactly once, on
every node — which is why every node's map is identical. Reads are the other half
and are deceptively hard: a naive `GET` off any node can return **stale** data (a
lagging follower, or a leader that was just deposed and doesn't know yet). A
linearizable read must be served by a leader that has **confirmed it still leads**
(read-index / lease) and has applied up to the read point. Build the machine in
`src/store.rs` (the read path is enforced in `replication.rs::read`).

**Done when ALL true:**
- [ ] Applying the same committed log on two fresh nodes yields **byte-identical** state — the apply step is deterministic and order-strict.
- [ ] `apply` advances `last_applied` by **exactly one per entry** and refuses a gap — entries are never applied out of order or twice.
- [ ] A **linearizable `GET`** never returns a value older than the most recent committed write that completed before it — no reads from stale/deposed leaders.
- [ ] A read issued to a **deposed leader** (partitioned away) does **not** silently return stale data — it fails or redirects once the node learns it lost leadership.
- [ ] A `GET` for an **unset key** is a clean `404`, not an error or a hang.

**Proof:** a determinism test (two Stores, same log, equal maps); a gap-rejection
unit test; a linearizability test — a partitioned old leader must not serve a
value that a new leader has already overwritten; `docs/09-design.md` names the
read technique chosen (read-index vs. lease) and its assumption.

*Concept to internalize:* the log is the source of truth and the map is a
*reduction* of it; why linearizable reads need a leadership check (not just a
local lookup); and the latency/assumption tradeoff between read-index and a lease.
**Stretch:** per-client request dedup (a monotonic client sequence number) so a
retried command — Raft is at-least-once — applies at most once.

### V4. Snapshots & log compaction — *the log can't grow forever*
An append-only log grows without bound and replaying it from index 1 on every
restart only gets slower. The fix: snapshot the state machine (far smaller than
its history) and discard every entry the snapshot covers, recording the
`last_included_(index, term)` so the consistency check still aligns at the seam.
This also forces `InstallSnapshot`: a leader that has compacted past what a slow
follower needs ships the whole snapshot instead of the missing entries. Build it
in `src/snapshot.rs`.

**Done when ALL true:**
- [ ] Once the log passes a threshold, the node **snapshots and compacts** — retained entry count drops, while applied state and all subsequent reads are unchanged.
- [ ] A node **restarting from a snapshot + tail** recovers **identical** state to one that replayed the full log — compaction is transparent to correctness.
- [ ] Compaction only ever discards entries **at or below `last_applied`** — never an un-applied or un-committed entry.
- [ ] A follower **too far behind** the leader's compacted log is caught up via **`InstallSnapshot`**, then resumes normal `AppendEntries` from `last_included_index + 1`.
- [ ] The snapshot is made **durable before** the log is truncated — a crash mid-compaction loses no committed state.

**Proof:** a compaction test (log shrinks, reads unchanged); a
restart-from-snapshot test (state equals full-replay); an `InstallSnapshot`
catch-up test (revive a far-behind follower); a crash-ordering argument/test in
`docs/09-design.md` for snapshot-before-truncate.

*Concept to internalize:* why state (not history) is what you persist long-term,
how compaction changes replication (entries can be *gone*), and the ordering
hazard between snapshotting and truncating. **Stretch:** chunked snapshot
transfer so a multi-MB snapshot doesn't block the heartbeat.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] **Leader redirect:** a write/linearizable-read to a follower returns a redirect (or a typed "not leader" with the leader's address), never a silent local answer — the client can always find the leader.
- [ ] **Peer RPC shape documented:** the `RequestVote` / `AppendEntries` /
  `InstallSnapshot` request & reply shapes are written down, including the
  `conflict_index` fast-backup hint's meaning. *(Stretch: a length-prefixed binary
  or gRPC transport instead of HTTP/JSON.)*
- [ ] **Graceful shutdown:** on SIGTERM, in-flight requests drain and the persistent Raft state (term/vote/log) is flushed before exit — a restart finds a consistent, torn-tail-free state.

### Durability & recovery
- [ ] `current_term`, `voted_for`, and the log are **persisted before** any RPC that depends on them is answered — and reload on restart (the backbone of V1/V2 safety).
- [ ] Recovery reopens at a **clean entry boundary** — a crash mid-write to the persistent state never yields a half-entry read as real.

### Security
- [ ] **Auth on the client API:** writes (and reads) sit behind a credential — an open KV endpoint is an open datastore; keys are never logged.
- [ ] **Peer trust boundary is stated:** the `/raft/*` endpoints let a caller drive consensus (force a term bump, inject entries) — the design doc states the trust assumption (private network / mTLS between nodes) and, at minimum, key/size validation on client input.

### Observability
- [ ] `tracing` span per request (via `common-telemetry`), with a request id.
- [ ] Structured logs on the state transitions that matter: **election started**, **became leader/follower**, **term change**, **snapshot taken**, **`InstallSnapshot` sent**.
- [ ] Metrics at `/metrics`: **current term**, **role**, **commit index vs. last-applied lag**, and **per-follower replication lag** (leader's view of `match_index`) — the numbers you watch to see consensus health.

### Cross-cutting scale skills
- [ ] The consensus state has **one clear ownership/locking model** — mutations serialize, the lock is never held across a peer RPC `await`, and that discipline is deliberate (documented), not incidental.
- [ ] A peer being **down or partitioned is tolerated, not fatal**: a failed `RequestVote`/`AppendEntries` is a missing answer this round — the cluster keeps working with a majority present.

---

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. `bench/` contains real numbers in `docs/09-benchmarks.md`: **write throughput
   & latency** (committed ops/s, p50/p99) for a 3-node cluster; **failover time**
   (leader kill → new leader serving) as a distribution; and **read throughput**
   for linearizable vs. (if implemented) follower/stale reads.
3. `docs/09-design.md` records the decisions the SPEC grades: the **persisted
   state + durability ordering** (V1/V2), the **commit rule + no-op-on-election**
   (V2), the **linearizable-read technique** (V3), and the **snapshot/compaction
   ordering** (V4) — plus the peer-trust assumption.
4. A **jepsen-lite** harness exists: an automated run that injects leader kills
   and network partitions under a concurrent write load and checks a
   linearizability / no-lost-committed-write invariant — and passes.
5. `cargo clippy --workspace -- -D warnings` and `cargo test -p raft-kv` are
   green; no `todo!()` remains on a checked path.

## Suggested order of attack
1. Boring path: a single-node "cluster" — `propose` appends to the local log,
   commits immediately (majority of 1), applies to the map, and `GET` reads it.
   Prove PUT/GET/DELETE round-trip with one node.
2. **V1:** the driver's two-timer loop, `RequestVote` both sides, randomized
   timeouts, persisted term/vote. Prove one leader converges across 3 nodes and
   survives a leader kill.
3. **V2:** `AppendEntries` both sides, the consistency check + tail repair, the
   majority commit rule (with the current-term guard), and the apply loop. Prove
   a write survives a leader kill and logs match.
4. **V3:** deterministic apply + the linearizable read path (read-index/lease).
   Prove no stale read from a deposed leader.
5. **V4:** snapshot + compaction + `InstallSnapshot`; prove restart-from-snapshot
   and far-behind-follower catch-up.
6. Auth, metrics (term/role/lag), graceful shutdown; then the jepsen-lite harness.
7. Benchmark, document, tune.

## Run it
```bash
cp .env.example .env        # then set NODE_ID / PEERS per node
# One node (default single-node cluster):
cargo run -p raft-kv
# A 3-node cluster (three terminals) — no external deps, disk IS the durable state:
NODE_ID=1 cargo run -p raft-kv
NODE_ID=2 cargo run -p raft-kv
NODE_ID=3 cargo run -p raft-kv
```
