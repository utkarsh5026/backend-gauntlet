# Consumer Groups & the Delivery Contract

> What this teaches: where a consumer's "how far have I read" bookmark lives,
> how many consumers share work without stepping on each other, and the
> single decision — *when do you commit?* — that determines whether a crash
> loses messages or replays them. No prior knowledge of consumer groups or
> delivery semantics assumed.
>
> Prepares you for **V4** in [SPEC.md](../SPEC.md) ("Consumer groups & durable
> offset commits"). Anchored to [src/group.rs](../src/group.rs) — the
> `commit`, `committed`, `join`, and `leave` `todo!()`s — and the group routes
> in [src/routes.rs](../src/routes.rs). Builds on all three previous docs:
> the log's offsets (00), the fetch path (01), and partitions (02).

---

## 0. The one sentence to hold onto

**The broker never deletes a record when it's read — consumption is just a
durable per-group cursor moving forward — and *when* you advance that cursor
relative to processing is the entire difference between "may lose messages"
and "may repeat messages."**

---

## 1. The problem: where does "how far have I read" live?

A queue you've seen before (project 04) *removes* a job when a worker takes
it. This broker can't do that — the log is immutable (V1), and, more deeply,
*it mustn't*: the same stream feeds many independent readers. Concretely, a
`payments` topic is read by the billing service *and* the analytics team *and*
a fraud detector, each at its own pace. Delete-on-read would make the first
reader steal the stream from the other two.

So reads don't consume. A consumer just fetches from an offset — which turns
"consumption" into pure bookkeeping: *some offset number, per partition,
saying how far this reader has gotten.* Now interrogate where that number
could live:

| Cursor lives… | What breaks |
| --- | --- |
| Nowhere (consumer starts at 0 every boot) | Every restart reprocesses all of history. |
| Consumer's memory | Crash → same as nowhere. |
| Consumer's local disk | Restart *on another machine* (deploys, autoscaling) loses it; two replicas of the consumer each keep their own, and double-process everything. |
| **Broker, durably, keyed by group** | Survives consumer crashes, machine moves, *and* broker restarts; every replica of a service shares one cursor, while unrelated services keep their own. |

The last row is V4. The unit of "shared cursor" is the **consumer group**: a
named set of cooperating consumers (all replicas of the billing service join
as group `billing`). Per `(group, topic, partition)` the broker durably stores
one **committed offset** — that's `GroupCoordinator`'s
`committed: HashMap<(String, u32), Offset>` in
[group.rs](../src/group.rs#L47-L53), persisted under `groups/` on disk.

Two groups over one topic are then *automatically* independent — each has its
own bookmark, and since reads don't mutate the log, neither can feel the
other. That's a Done-when box, and it's the fan-out property that makes a log
more than a queue:

```
topic "payments", partition 0:   [0] [1] [2] [3] [4] [5] [6] [7]   log-end = 8
                                              ▲               ▲
group "billing"  committed = 3 ───────────────┘               │
group "analytics" committed = 7 ──────────────────────────────┘
```

(`billing`'s **lag** here is 8 − 3 = 5 records; `analytics`'s is 1. Hold that
thought — lag is the health metric of doc 04.)

---

## 2. The decision that *is* the delivery guarantee

A consumer's loop: **fetch** a batch from its committed offset → **process**
it (write to a DB, call an API…) → **commit** the new offset
(`POST /groups/{group}/offsets`, → `GroupCoordinator::commit`). Now ask the
only question that matters: *what if it crashes between the two last steps?*
There are exactly two orderings, and the crash decides everything:

**Commit AFTER processing** — crash window is *processed-but-not-committed*:

```
fetch [3] ──▶ process [3] ──▶ 💥 crash ──▶ restart ──▶ fetch from committed=3
                                                        └▶ record 3 processed AGAIN
```

Redelivery. Record 3 is seen *at least once* — duplicates possible, loss
impossible. Every fetched record is either processed-and-committed, or will
be fetched again.

**Commit BEFORE processing** — crash window is *committed-but-not-processed*:

```
fetch [3] ──▶ commit 4 ──▶ 💥 crash ──▶ restart ──▶ fetch from committed=4
                                                     └▶ record 3 processed NEVER
```

Silent loss. Record 3 is seen *at most once* — no duplicates, but a crash
swallows whatever was in flight.

That's the whole theorem, provable from the two timelines: **commit ordering ⇒
delivery guarantee.** You choose duplicates (at-least-once) or loss
(at-most-once). *"Neither" is not on the menu* — not from a broker alone —
because the crash between two distinct operations can't be made atomic from
the broker's side. This SPEC (like nearly every real system) picks
**at-least-once**: commit after processing, deliberately, documented in
`docs/08-design.md`. Loss is silent and unfixable; duplicates are loud and
handleable — which leads to:

**How the industry spells "exactly-once":** at-least-once delivery + an
**idempotent consumer**. If processing record 3 twice has the same effect as
once — because the DB write is keyed on the record's `(partition, offset)`, or
an `INSERT … ON CONFLICT DO NOTHING`, or a dedup table — redelivery becomes
harmless. CONCEPTS.md's depth probe ("consumer writes to its DB, crashes
before committing, record is redelivered — design the consumer so this is
harmless") is answered exactly there: make the *effect* idempotent, and
at-least-once upgrades to effectively-exactly-once end to end. Flink's and
Kafka Streams' "exactly-once" are this plus transactions underneath — never a
magic third delivery mode.

The trap ([CONCEPTS.md](../CONCEPTS.md)): committing on a *timer*
("auto-commit, for simplicity"). A timer can fire after records were fetched
but while they're still being processed — committing *past* work not yet done.
You've silently reordered commit-before-processing, and your at-least-once
becomes at-most-once precisely when a crash happens, which is the only time it
matters.

---

## 3. Assignment: splitting partitions without double-reads

The second thing a group does: **parallelism**. Ten replicas of the billing
service join group `billing`; the topic has 6 partitions (V3 fixed that
number). Who reads what?

The invariant, a Done-when box: **within a group, each partition is owned by
at most one member at a time.** Two members on one partition would share one
committed offset while interleaving fetches — each skipping records the other
processed and double-processing others; the cursor becomes meaningless.
Partitions are the unit of parallelism *because* they're the unit of ordering
(V3), and exclusive ownership is what keeps per-partition processing ordered
too.

So `join` ([group.rs:119](../src/group.rs#L119)) must hand each member a
*disjoint* slice, covering all partitions:

```
6 partitions, group "billing":

1 member :  m1 → {0,1,2,3,4,5}
2 members:  m1 → {0,1,2}   m2 → {3,4,5}
3 members:  m1 → {0,1}     m2 → {2,3}     m3 → {4,5}
7 members:  six get one each; the 7th gets ∅  ← partition count (V3)
                                                caps parallelism, visibly
```

Membership changes force a **rebalance**: a member joining means others must
*shrink* their share; a member leaving (`leave`,
[group.rs:127](../src/group.rs#L127)) means its partitions must be re-covered
by survivors. The Done-when criterion: while any member is present, **no
partition goes unowned** (records pile up unread) and **none is double-owned**
(cursor chaos). This coupling — the group is simultaneously the unit of
parallelism *and* the unit of shared progress — is exactly what makes
rebalancing tricky: moving a partition between members mid-flight has to
respect the not-yet-committed work of the member losing it. (The SPEC keeps
mid-flight rebalance-under-load as a *stretch* goal; V4 proper needs
join/leave reassignment with the invariant held.)

---

## 4. The cursor is broker state — so it gets V1's discipline

One more Done-when box closes the loop: committed offsets **survive a broker
restart**. A commit the broker acknowledged and then forgot after a reboot
sends every consumer back to offset 0 — reprocessing days of history (or,
with a "start at end" policy, silently skipping everything unread). So
`commit` ([group.rs:87](../src/group.rs#L87)) must persist to disk — fsync'd,
per your V1 fsync reasoning, *before* acknowledging — and
`GroupCoordinator::open`'s recovery TODO
([group.rs:63-66](../src/group.rs#L63-L66)) must load it all back on startup.

The scaffold stores each group's offsets as files under `groups/` — the same
durability discipline as the log, in miniature. (Kafka does something
prettier: commits are *produced into an internal topic*,
`__consumer_offsets` — the broker eating its own dog food. The file-per-group
stand-in teaches the same lesson without the bootstrap knot.)

Also yours to define: what does a group with *no* commit for a partition get?
`committed` returns `Option<Offset>` ([group.rs:101](../src/group.rs#L101)) —
`None` means "never committed", and whether a fresh group starts from 0
(replay all history) or from the log end (only new records) is a real policy
choice both Kafka (`auto.offset.reset`) and your design doc must name.

---

## 5. The design space — decisions the SPEC leaves to you

- **Commit persistence format** under `groups/` — one file per group?
  Per (group, topic, partition)? Rewritten atomically or appended? Each is
  workable; the graded property is that an acknowledged commit survives a
  crash-and-restart.
- **Monotonicity.** Should a commit at offset 3 be able to *lower* a stored 7?
  The scaffold's TODO flags it — decide, and know which consumer bugs each
  answer forgives or amplifies.
- **The assignment function.** Contiguous ranges vs round-robin dealing;
  deterministic from the member list or stateful. The invariant (disjoint,
  covering) is fixed; the strategy is yours.
- **How reassignment reaches the *other* members.** `join` returns the calling
  member's share — the scaffold pointedly notes a rebalance changes everyone
  else's too ([group.rs:116-117](../src/group.rs#L116-L117)). Model that
  however you expose it (members re-poll? a generation/epoch number?) — this
  is the doorway to how real group protocols work.
- **Fresh-group start policy** (§4): 0 or log-end, written down.

`/hint` for graduated nudges; `/quest V4` for the guided build — the
crash-before-commit acceptance test is the one that makes this vertical real.

---

## 6. Mental model summary

| Idea | One-line takeaway |
| --- | --- |
| Reads don't consume | The log is immutable; "consumption" is a cursor, so any number of groups fan out over one topic freely. |
| Consumer group | Named set of cooperating consumers sharing one durable cursor per partition — the unit of parallelism *and* of progress. |
| Committed offset | The broker-side bookmark per (group, topic, partition); durable, restart-proof, loaded back on open. |
| Commit ordering ⇒ guarantee | After processing = at-least-once (duplicates, never loss); before = at-most-once (loss, never duplicates). Two timelines, whole proof. |
| "Exactly-once" | At-least-once + idempotent consumer effects — never a third broker mode. |
| Assignment invariant | Within a group: every partition owned, none owned twice; join/leave triggers rebalance to keep it true. |
| Parallelism cap | ≤ 1 member per partition — V3's partition count is the ceiling, visible here. |

**Where you'll build this:** [src/group.rs](../src/group.rs) — `commit`
([line 87](../src/group.rs#L87)), `committed`
([line 101](../src/group.rs#L101)), `join`
([line 119](../src/group.rs#L119)), `leave`
([line 127](../src/group.rs#L127)), plus the recovery TODO in `open`
([line 63](../src/group.rs#L63)). The HTTP surface is already wired in
[routes.rs](../src/routes.rs). It unlocks all five **V4 Done-when** boxes:
durable commits, exclusive ownership, independent groups, at-least-once with
documented commit ordering, and join/leave reassignment.

**In the wild:** Kafka consumer groups + `__consumer_offsets`; Kinesis
checkpointing; SQS in spirit (visibility timeout ≈ the redelivery window).
Project 05's JetStream consumer was doing all of this under you —
ack-after-processing, redelivery, durable cursors. Now you know what it was.
