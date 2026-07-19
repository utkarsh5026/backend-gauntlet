# How Snapshots & Log Compaction Work — From First Principles

> A ground-up guide to the problem every append-only design eventually meets:
> the log grows forever, and "replay from index 1" gets slower every day. The
> fix — persist the *state* and throw away the *history it summarizes* — sounds
> like housekeeping but is riddled with safety cliffs: truncate a microsecond
> too early and a crash loses committed data; compact past the wrong index and
> you discard state you never derived; and once entries can be *gone*,
> replication itself needs a new RPC. No prior knowledge assumed beyond docs
> [00](00-how-leader-election-works.md)–[02](02-how-the-state-machine-and-linearizable-reads-work.md).
>
> This prepares you for **V4** of the [SPEC](../SPEC.md): `maybe_snapshot`,
> `handle_install_snapshot`, and `send_snapshot` in
> [snapshot.rs](../src/snapshot.rs), plus `Store::snapshot`/`restore` in
> [store.rs](../src/store.rs). The wired index math that makes a compacted log
> addressable lives in [log.rs](../src/log.rs) (`snapshot_last_index`,
> `term_at`, `get`, `truncate_from`, `snapshot_point`), and the trigger knob is
> [`RaftConfig::snapshot_threshold`](../src/node.rs) (default 1000, from
> [main.rs](../src/main.rs)). Ideas here, decisions yours — `/hint` and
> `/quest` for the build.

---

## 0. The one sentence to hold onto

**A snapshot replaces a log *prefix* with the state it folds to — safe only
because the fold is deterministic (doc 02) — and the entire safety of
compaction is two orderings: snapshot durable *before* truncation, and never
truncate past `last_applied`.**

---

## 1. The problem, with real numbers

Doc 02 crowned the log the source of truth. Take that seriously and do the
arithmetic. A modest cluster taking **1,000 writes/s** at ~100 bytes per entry:

| quantity | value |
| --- | --- |
| log growth | 100 KB/s ≈ **8.64 GB/day** |
| entries after one day | 86.4 million |
| restart replay at 500k applies/s | **~173 s** — and doubling daily |
| the *map* those entries fold to (say 1M live keys × 100 B) | **~100 MB**, flat |

Two separate pains, same cause:

1. **Unbounded disk.** The log records every step ever taken; the map only
   records where you *are*. History grows linearly forever; state grows with
   your working set.
2. **Unbounded recovery.** A restarted node replays the whole log through
   `apply` to rebuild its map (doc 02's fold). Day-one restarts are instant;
   day-100 restarts take hours. Same for a brand-new follower joining.

And you can't "just delete old entries" — the log is the only truth, and (as
§5 shows) other nodes may still *need* those entries from you.

---

## 2. The insight: state is a fold; persist the fold's result

The fix falls out of doc 02's inversion. The map **is** the log, reduced — so
the map is a *lossless summary of a log prefix* as far as future state is
concerned. Fold entries 1–1000 into `{x:7, y:2, …}` and, for computing any
*later* state, the 1000 entries and the map are interchangeable:

```
[1..=1000] ++ [1001..]  ──fold──►  S
snapshot(S₁₀₀₀) ++ [1001..] ──fold──►  S      (same S, provably — determinism)
```

So: serialize the state machine (`Store::snapshot` — `data` +
`last_applied`), persist that blob, and discard entries 1–1000. That's
**compaction**. What's lost is only the ability to *re-watch history* — the
intermediate states — which Raft never needs; it only ever folds forward.

When is this a win? The [CONCEPTS](../CONCEPTS.md) card's honest caveat: state
is smaller than history when keys are **overwritten** (1M writes to 1k keys →
a 1k-entry snapshot). For append-mostly state — every write a fresh key — the
snapshot approaches the log's own size and compaction buys only replay speed,
not much disk. That's why the trigger is a *policy knob*
(`snapshot_threshold`), not a law. (Redis meets the same fork as RDB vs
AOF-rewrite; project 22's WAL checkpointing is this again one level down.)

---

## 3. The seam: what `last_included_(index, term)` holds together

Delete entries 1–1000 naively and the machinery of docs 00–01 starts dying at
the edges, because three mechanisms assume they can *ask about the past*:

| mechanism | what it asks | breaks how |
| --- | --- | --- |
| consistency check (doc 01 §3) | "do you hold `(prev_log_index=1000, prev_log_term)`?" | entry 1000 is gone — a correct follower can't answer |
| election up-to-date check (doc 00 §6) | "what's your `(last_log_term, last_log_index)`?" | an empty post-compaction log would answer (0, 0) — a fully-caught-up node suddenly looks maximally stale |
| log addressing | "entry at index 1001?" | it's now at vec position 0 — every index is shifted |

The repair is one record: the snapshot carries the **index and term of the last
entry it covers** — `last_included_index` / `last_included_term` — and the log
keeps them as its new *base*. The scaffold's [log.rs](../src/log.rs) already
speaks this dialect, worth reading as the concept made concrete:

- physical position = `index − snapshot_last_index − 1` (`get`) — logical
  indices never change meaning; only where they live does;
- `term_at(index)` answers **at** the boundary from `snapshot_last_term` — so
  the consistency check for `prev_log_index == 1000` still gets its answer even
  though entry 1000's *body* is gone;
- `last_index`/`last_term` fall back to the snapshot point when the retained
  vec is empty — a compacted-and-idle node still reports its true position to
  voters.

So the seam is airtight *at* the boundary. It is **only** airtight at the
boundary — a question about index 999 has no answer anymore. What happens when
someone genuinely needs 999 is §5.

---

## 4. The two orderings that are the entire safety story

Compaction is two mutations — write snapshot, truncate log — and a crash can
land between them. Both orders "work" crash-free; only one survives crashes.
This is the SPEC's "durable **before** the log is truncated" box, and the
design-doc argument it demands:

| order | crash lands between | node restarts with | verdict |
| --- | --- | --- | --- |
| truncate, then snapshot | log prefix **gone**, snapshot **absent** | neither history nor state — committed, acked entries unrecoverable on this node | **data loss** |
| snapshot durable, then truncate | snapshot **present**, log prefix **still present** | state *and* the history it covers — redundant, not wrong | **harmless overlap** |

The asymmetry is the lesson: one order's failure mode is *loss*, the other's is
*waste*. Recovery from the overlap is trivial — restore the snapshot, ignore
retained entries at or below `last_included_index` (the `last_applied + 1` gate
from doc 02 refuses them anyway), fold the tail. And "durable" means durable:
the blob must actually be on disk (fsynced — same bar as
[`RaftLog::persist`](../src/log.rs)) before `truncate` runs, not sitting in a
page cache that dies with the machine.

The second ordering rule is the *ceiling*: **only entries at or below
`last_applied` may be discarded.** A snapshot is a photo of applied state; an
entry above `last_applied` hasn't been folded in yet, so no snapshot covers it
— discard it and that command is simply gone from the universe. The
[CONCEPTS](../CONCEPTS.md) trap makes it concrete: trigger compaction on log
*size* alone while the apply loop is wedged, and you'll happily "compact" past
entries you never consumed. The trigger reads `log.len()` against the
threshold; the *ceiling* must come from apply progress. (Which is also why
`maybe_snapshot` is called *from the apply path* — see its TODO.)

Note what's *pleasantly* absent: no coordination. Snapshotting is a **local**
decision — each node compacts its own log on its own schedule, because the
snapshot only summarizes entries that are already committed and applied, i.e.
already agreed. Consensus governs the log's *content*, not its *storage*.

---

## 5. `InstallSnapshot`: when the entries a follower needs are gone

Now the distributed consequence. Doc 01's repair loop walks `next_index[F]`
backwards until leader and follower agree. But suppose F was down for a week:
`next_index[F]` walks back to 800 — and the leader compacted through 1000.
Entries 800–1000 **no longer exist**. `AppendEntries` has nothing to send;
`entries_from(800)` starts at 1001; the seam answers questions *at* 1000 only.
The repair loop is stuck by design.

The escape is the third RPC ([`InstallSnapshotArgs`](../src/rpc.rs)): if you
can't send the follower the *steps*, send it the *result*.

```
leader                                      follower F (stuck at 799)
  │  next_index[F] ≤ snapshot_last_index?  │
  │  → can't AppendEntries. Ship state:    │
  │───InstallSnapshot{term, data,         ─►  term check (same rule as ever)
  │     last_included_index: 1000,         │  store.restore(data)        ← doc 02's fold, replaced wholesale
  │     last_included_term:  t7}           │  log := empty, based at (1000, t7)
  │                                        │  commit_index = last_applied = 1000
  │◄──reply{term}──────────────────────────│  persist
  │  next_index[F] = 1001                  │
  │  …AppendEntries resumes from 1001 ────►│  normal replication again
```

Three things to notice, each a Done-when box or its edge:

- **The trigger is precise:** `next_index[F]` fell at-or-below the leader's
  `snapshot_last_index` (`broadcast_append_entries`'s TODO names this exact
  branch). Not "follower seems slow" — *the entries it needs are gone.*
- **The follower's state machine is replaced, not folded.** `Store::restore`
  overwrites `data` + `last_applied` wholesale — the one legal violation of
  "apply one at a time," legal because the snapshot *is* `apply(1..=1000)` by
  determinism. Its log base becomes `(1000, t7)`, so the very next consistency
  check (`prev = (1000, t7)`) matches at the seam. The handoff between RPCs is
  seamless because both speak `(index, term)`.
- **Term discipline never sleeps:** a stale leader's `InstallSnapshot` is
  rejected; a valid one resets the election timer like any heartbeat (the
  `handle_install_snapshot` TODO), and a snapshot *older* than the follower's
  own state is a no-op — messages can arrive late and out of order.

The stretch goal hides a real operational cliff worth doing the math on: the
default timings give a **50 ms** heartbeat and a **150–300 ms** election
timeout. A 100 MB snapshot on a gigabit link takes ~0.8 s to transfer — if
sending it blocks the leader's heartbeat loop, every follower times out and
elects a new leader *mid-transfer*, repeatedly. That's why real
implementations chunk the transfer (and why the SPEC stages it as a stretch:
correctness first, then concurrency).

---

## 6. The design space you'll navigate (not the answers)

- **Trigger policy** — the scaffold gives you entries-count
  (`snapshot_threshold = 1000`); bytes, wall-clock, or apply-lag-aware policies
  are all defensible. What you must get right regardless: the *ceiling* is
  `last_applied`, never the threshold.
- **Snapshot durability mechanics** — how the blob becomes crash-safe before
  truncation (the same write-to-temp/rename-vs-append question as
  `persist()`'s TODO), and how snapshot + retained log stay mutually consistent
  in your on-disk format — recovery must find a matched pair.
- **Serialization format** — `Store::snapshot` encodes `data` +
  `last_applied`; the format is yours (the restart-from-snapshot test only
  cares that `restore(snapshot())` round-trips exactly).
- **Concurrency around the freeze** — what is locked while serializing the
  map, what the apply loop does meanwhile, and how a snapshot-in-progress
  interacts with an inbound `InstallSnapshot`. The never-hold-the-lock-across-
  `.await` rule ([node.rs](../src/node.rs)) bites hardest here.
- **Chunked transfer (stretch)** — the heartbeat-starvation math above, solved
  without letting two half-installed snapshots interleave.

---

## 7. Mental model summary

| Mechanism | Question it answers | Failure it prevents |
| --- | --- | --- |
| snapshot = persisted fold result | "how can truth be deleted?" | unbounded disk + unbounded replay |
| determinism (doc 02) | "why is the swap lossless?" | snapshot ≠ what replay would have built |
| `last_included_(index, term)` | "how does the seam still answer?" | consistency/election checks dying at the boundary |
| logical-vs-physical index split | "where is entry i now?" | every index shifting meaning after compaction |
| snapshot-durable-then-truncate | "which crash order is safe?" | committed state lost mid-compaction |
| ceiling at `last_applied` | "how far may compaction reach?" | discarding commands never folded into any snapshot |
| `InstallSnapshot` | "what if the entries are *gone*?" | a lagging follower stranded forever |
| resume at `last_included_index + 1` | "how do the two RPCs hand off?" | gaps or overlap after a snapshot install |

## 8. Where you'll build this

[snapshot.rs](../src/snapshot.rs): `maybe_snapshot` (trigger + ordering),
`handle_install_snapshot` (follower side), `send_snapshot` (leader side) —
plus [`Store::snapshot`/`restore`](../src/store.rs) and the snapshot branch in
[`broadcast_append_entries`](../src/replication.rs). The base-index plumbing in
[log.rs](../src/log.rs) is already wired underneath you.

This doc unlocks V4's **Done when ALL true** ([SPEC](../SPEC.md)):

- [ ] past the threshold, the node snapshots and compacts — reads unchanged
- [ ] restart from snapshot + tail ≡ full replay
- [ ] compaction never passes `last_applied`
- [ ] a too-far-behind follower is caught up via `InstallSnapshot`, then resumes normal `AppendEntries`
- [ ] snapshot durable **before** truncation — a mid-compaction crash loses nothing

Proofs: the compaction test, the restart-equivalence test, the far-behind
catch-up test, and the crash-ordering argument in `docs/09-design.md`.

Next: [04-consensus-fundamentals-woven-through.md](04-consensus-fundamentals-woven-through.md) —
the horizontal checklist: durability ordering, the lock rule, trust boundaries,
and how you *watch* a consensus system.
