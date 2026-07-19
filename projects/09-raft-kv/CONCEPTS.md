# Concept Bank — Project 09: Distributed KV Store with Raft

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted. Consensus rewards precision — vague versions of these answers are how data gets lost.

---

## 🧠 Card 1 — Leader election: one leader per term, or none *(V1 · `src/election.rs`)*

**The problem.** If more than one node accepts writes, two nodes can accept *conflicting* writes during a partition, and there is no principled way to merge them later — that's split-brain, and it corrupts data silently. So Raft funnels all writes through one leader. But now electing that leader *is* the safety problem: "elect a leader" is easy; "**never** elect two for the same term, across crashes, restarts and partitions" is the entire job.

**The idea.** Time is divided into **terms**. A follower hearing no leader within a randomized timeout becomes a candidate: increments the term, votes for itself, requests votes. Majority wins. Two rules make it safe rather than merely live: each node grants at most one vote per term *and persists that vote to disk before answering* (a rebooted node must not vote twice), and a node refuses any candidate whose log is less up-to-date than its own (so a winner is guaranteed to hold everything committed). Split votes aren't prevented — they're made *rare and self-healing* by randomized timeouts.

**In the wild:** etcd (Kubernetes' brain), Consul, TiKV, CockroachDB — all Raft; ZooKeeper's ZAB and Paxos are the same problem with different proofs.

**You own it when you can explain:**
- [ ] Why exactly one writer is the design goal — what two simultaneous leaders in one term would allow, concretely.
- [ ] The term as a logical clock: what any node does on seeing a higher term (step down, adopt it) and why terms are never reused.
- [ ] The persisted-vote rule: the exact reboot-and-revote sequence that elects two leaders if `voted_for` lives only in memory.
- [ ] The up-to-date comparison (last entry's term, then log length) and *what it protects*: a leader missing committed entries would overwrite them.
- [ ] Why randomized timeouts resolve split votes without coordination — and why a fixed timeout would deadlock retries forever.
- [ ] What happens during the leaderless gap (writes fail/queue; the cluster is briefly unavailable — the price of consistency, CAP made concrete).

**Depth probes:**
- Why does a *majority* specifically matter for votes? (Any two majorities intersect — so two candidates can't both win one term.)
- A partitioned node keeps timing out and inflating its term. What damage does it do on rejoin, and how does pre-vote prevent it?

**Trap:** treating election as a liveness feature ("we elect fast"). Election is a *safety* mechanism; speed matters only after "never two leaders per term" is unbreakable under adversarial timing.

---

## 🧠 Card 2 — Replication & the commit rule *(V2 · `src/replication.rs`)*

**The problem.** The leader has the write — now it must be on enough machines that *no single failure, including the leader's, can lose it*. Followers may hold stale or divergent tails from deposed leaders; those must be repaired, not appended past. And the subtlest data-loss bug in Raft hides here: counting replicas of an *old term's* entry as commitment (§5.4.2) can let a future leader erase an entry a client was told succeeded.

**The idea.** Every `AppendEntries` names the `(prev_log_index, prev_log_term)` the new entries must follow; a follower missing that exact entry rejects, and the leader walks back until they agree — mechanically repairing divergence (the **Log Matching** property does the bookkeeping). An entry is **committed** when a majority holds it — and, critically, the leader only advances commit for entries *from its own term* (a fresh leader appends a no-op to get one). Commitment is a majority fact, not a leader's opinion: any future majority overlaps this one, so a committed entry survives every future election.

**In the wild:** every Raft implementation's hardest test cases are exactly these (etcd's regression suite has §5.4.2 scenarios by name); the same "quorum write, quorum intersection" logic underlies Paxos and quorum replication generally.

**You own it when you can explain:**
- [ ] The consistency check as induction: why matching at `(prev_index, prev_term)` implies matching at every earlier index.
- [ ] Tail repair: a concrete divergence (deposed leader appended uncommitted entries) and how the walk-back overwrites it — and why overwriting *uncommitted* entries is always safe.
- [ ] Quorum intersection as the heart of everything: why majority-committed ⇒ present on at least one member of *any* future electing majority ⇒ up-to-date rule preserves it.
- [ ] The §5.4.2 scenario, step by step, and how the current-term commit rule + no-op closes it.
- [ ] Why "leader said OK" must mean majority-durable, not leader-durable — what acking early would risk.
- [ ] Why writes to a non-leader redirect rather than serve locally.

**Depth probes:**
- Batching and pipelining `AppendEntries`: what ordering constraint must survive the optimization?
- Why can a 5-node cluster tolerate 2 failures but not a clean 2/3 split — where does the majority requirement bite?

**Trap:** testing replication only with clean sequential failures. The §5.4.2 bug *requires* a specific multi-election interleaving to appear — this is why consensus code that "works" can be wrong for years.

---

## 🧠 Card 3 — The state machine & linearizable reads *(V3 · `src/store.rs`)*

**The problem.** Consensus produced an agreed, ordered command log — but clients want a *map*, and they want reads that don't lie. The tempting shortcut — serve `GET` from any node's local map — returns stale data from lagging followers, and worse: from a **deposed leader that doesn't know it yet** (partitioned away, still confidently serving yesterday's values). "I read it from the leader" is not enough when leadership itself is stale.

**The idea.** The map is a deterministic **reduction** of the log: every node applies committed entries in index order, exactly once, so every node's map is byte-identical — state is derived, never authoritative. A **linearizable** read must be served by a leader that has *confirmed it still leads* at read time: either **read-index** (exchange a heartbeat round with a majority, then read once applied past that point) or a **lease** (rely on bounded clock drift to skip the round-trip — faster, with an assumption you must be able to name).

**In the wild:** etcd's linearizable-by-default reads (read-index) vs its `serializable` fast path; the replicated-state-machine framing is the standard model for all consensus systems; Raft's at-least-once client retries → per-client dedup is how etcd/TiKV handle it.

**You own it when you can explain:**
- [ ] The log-is-truth/map-is-cache inversion, and why apply must be deterministic and gap-refusing for "identical state everywhere" to hold.
- [ ] What linearizability promises, in one sentence with a timeline (a read never returns older than the latest write that *completed* before it began).
- [ ] The deposed-leader stale read, end to end: partition, new leader elected, old leader serves a `GET` — and which check stops it.
- [ ] Read-index vs lease: the latency each costs and the assumption each makes (majority round-trip vs bounded clock drift).
- [ ] Why Raft delivers commands at-least-once to the state machine (client retries after leader failover) and how a client sequence number makes apply-at-most-once.

**Depth probes:**
- Why is "read from a majority of followers" not a substitute for read-index? What could still be wrong?
- Which of your KV commands are naturally idempotent (SET) and which aren't (INCR would not be) — and what that implies for dedup?

**Trap:** the local-read shortcut on the leader. It passes every test until the one partition where the old leader hasn't heard the news — the bug is invisible without an adversarial test that *creates* a deposed leader and reads from it.

---

## 🧠 Card 4 — Snapshots & log compaction *(V4 · `src/snapshot.rs`)*

**The problem.** The log grows forever; replaying from index 1 on each restart gets slower every day. But the log is your only truth — you can't just delete old entries, and a follower that fell behind your deletion point can no longer be caught up by `AppendEntries` (the entries it needs are *gone*).

**The idea.** Persist the *state* instead of re-deriving it: snapshot the state machine (far smaller than its history), record `last_included_(index, term)` so the consistency check still aligns at the seam, then discard covered entries. Ordering is the safety: snapshot must be durable *before* truncation, and only entries at or below `last_applied` are ever discarded. For the fallen-behind follower, the leader ships the whole snapshot (`InstallSnapshot`) and resumes normal replication after it.

**In the wild:** etcd snapshots (and its compaction-tuning lore), Redis RDB vs AOF-rewrite is the same state-vs-history trade, project 21's workflow "continue-as-new" is the application-level cousin.

**You own it when you can explain:**
- [ ] Why state (a fold) is smaller than history (every step) — and when it isn't (append-mostly state), which is why compaction is a policy, not a law.
- [ ] The seam problem: what `last_included_index/term` lets the very next `AppendEntries` consistency check verify.
- [ ] The crash-ordering argument: what's lost if you truncate before the snapshot is durable, vs the harmless overlap if you snapshot before truncating.
- [ ] Why compaction must never pass `last_applied` — discarding an unapplied entry discards state you haven't derived yet.
- [ ] When `InstallSnapshot` fires and how the follower rejoins normal replication at `last_included_index + 1`.

**Depth probes:**
- A multi-GB snapshot blocks the leader's heartbeat while transferring. What breaks (election timeout!) and how does chunking fix it?
- How does this same state-vs-history decision appear in project 21 (replay cost) and project 22 (WAL checkpointing)?

**Trap:** compacting by log *size* alone without checking apply progress. A stuck state machine (apply loop wedged) plus size-triggered compaction equals discarding history you never consumed.

---

## ⚡ Rapid-fire round

- [ ] The persistence contract: `current_term`, `voted_for`, log entries — each persisted *before* the RPC answer that depends on it, or name the double-vote/lost-entry that follows.
- [ ] Why the consensus lock is never held across a peer-RPC await (deadlock + a stalled peer stalls the node).
- [ ] A down peer is "no answer this round", never an error path — the cluster runs on any majority.
- [ ] The observability of consensus health: term (stability), commit−applied lag (apply health), per-follower match_index lag (replication health).
- [ ] Why `/raft/*` endpoints are a trust boundary (they can drive term inflation and inject entries) — private network or mTLS assumption, stated.
- [ ] What a jepsen-style harness does that unit tests can't: injects partitions/kills *while* checking a linearizability invariant across the whole run.

## 🔗 Connects to

- The replicated log is project 08's log + a quorum; the state machine is project 21's event-sourced replay — Raft is those two ideas fused.
- Project 17 (V1) *reuses* this: room placement is a Raft-lite replicated map — you'll be glad you built it once already.
- The consistency-over-availability choice here is the mirror image of project 07's cache — together they're the CAP spectrum, lived.
