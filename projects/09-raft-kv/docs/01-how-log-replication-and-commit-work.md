# How Log Replication & Commit Work — From First Principles

> A ground-up guide to the second half of Raft: how a leader gets its log onto
> every follower **byte-for-byte identical**, how a diverged follower gets
> *repaired* rather than corrupted, and what the word **committed** precisely
> means — including the famous §5.4.2 trap where the "obvious" commit rule loses
> acknowledged data. No prior knowledge assumed beyond
> [00-how-leader-election-works.md](00-how-leader-election-works.md).
>
> This prepares you for **V2** of the [SPEC](../SPEC.md) and the five `todo!()`s
> in [replication.rs](../src/replication.rs): `handle_append_entries` (follower),
> `broadcast_append_entries` (leader), `maybe_advance_commit` (the commit rule),
> `apply_committed` (log → state seam), and `propose` (the client write path).
> Wired support: [rpc.rs](../src/rpc.rs) (`AppendEntriesArgs/Reply`, `LogEntry`,
> `Command::Noop`), [log.rs](../src/log.rs) (`term_at`, `entries_from`, `append`,
> `truncate_from`), [error.rs](../src/error.rs) (`AppError::NotLeader`). Ideas
> here, decisions yours — `/hint` and `/quest` for the build.

---

## 0. The one sentence to hold onto

**A write exists once a *majority* of nodes hold it at the same log position —
not when the leader has it, not when the leader *says* so — because any future
leader must win a majority of votes, and any two majorities share a member who
can testify.**

Everything in V2 either moves entries toward that majority (`AppendEntries`),
detects that a follower's log disagrees and repairs it (the consistency check),
or decides when the majority threshold has *truly* been crossed (the commit
rule, where the one subtle data-loss bug in all of Raft hides).

---

## 1. The problem: "just broadcast it" and four ways it dies

V1 gave us one leader. Naive plan: leader gets `PUT x=1`, sends it to everyone,
acks the client. Each failure below is a real interleaving, not paranoia:

| # | Scenario | What goes wrong |
| --- | --- | --- |
| 1 | Leader acks the client, *then* crashes before any follower got the entry | The write is acknowledged and **gone**. No other node ever heard of it. |
| 2 | Follower was down for entries 5–9, receives entry 10 | It appends 10 after 4. Its log now *lies about history* — same positions, different contents than everyone else. |
| 3 | A deposed leader (term 3) wrote entries 8–9 locally before losing leadership; the new leader (term 4) has different entries 8–9 | Two logs *disagree at the same index*. Appending past the disagreement bakes the corruption in forever. |
| 4 | Old leader, partitioned but alive, keeps broadcasting | Followers that already joined term 4 must not accept term-3 entries. |

Note the shape: #1 is about *when to ack*, #2–3 about *keeping logs identical*,
#4 about *stale authority*. Raft answers all four with a single RPC —
`AppendEntries` — carrying three safeguards: the sender's **term** (kills #4,
same higher-term-wins rule as V1), a **consistency check** (kills #2 and #3),
and the **majority commit rule** (kills #1). The rest of this doc is those three,
in order of increasing subtlety.

---

## 2. The unit of agreement: `(index, term)` pairs, not values

Nodes don't agree on "the value of x" — they agree on a **log**: an ordered
sequence of commands, and the goal is that every node's sequence is identical.
[`LogEntry`](../src/rpc.rs) is a command stamped with its `index` (position,
1-based) and the `term` of the leader that created it:

```
index:   1        2        3        4        5
       ┌────────┬────────┬────────┬────────┬────────┐
term:  │   1    │   1    │   2    │   4    │   4    │
cmd:   │ x=1    │ y=2    │ x=7    │ Noop   │ y=9    │
       └────────┴────────┴────────┴────────┴────────┘
```

Why stamp the term? Because *index alone is ambiguous*: scenario #3 above put
two different entries at the same index (a deposed leader's entry 8 vs the new
leader's entry 8). The pair disambiguates: entry (8, term 3) and entry (8,
term 4) are visibly *different claims about history*, and terms tell you which
leader made each claim. This pays off as the **Log Matching property**:

> If two logs contain an entry with the same index *and* the same term, then the
> logs are identical in all entries up through that index.

That's not an assumption — it's an invariant the consistency check *enforces
inductively*, and one of V2's Done-when boxes ("checked, not assumed").

---

## 3. The consistency check: repair by walking backwards

Every [`AppendEntriesArgs`](../src/rpc.rs) names the entry the new ones must
*follow*: `(prev_log_index, prev_log_term)`. The follower's rule is brutal and
simple: **accept only if my log holds exactly that entry** (checkable with
[`log.term_at(prev_log_index)`](../src/log.rs)); otherwise reject and let the
leader try again from further back.

Trace it. A leader in term 4 and a follower that briefly followed a term-3
leader whose writes never committed:

```
                 index:    1     2     3     4     5
Leader  (term 4):        [t1]  [t1]  [t2]  [t4]  [t4]     next_index[F] = 6
Follower:                [t1]  [t1]  [t3]                  ← diverged at 3
```

The leader tracks `next_index[F]` — its guess at where F's log ends
(see [`Inner`](../src/node.rs)) — and walks:

| round | leader sends | follower checks | result |
| --- | --- | --- | --- |
| 1 | `prev=(5, t4)`, entries: [] | do I hold (5, t4)? — no entry at 5 | **reject** → `next_index[F] = 5` |
| 2 | `prev=(4, t4)`, entries: [5] | entry at 4? — none | **reject** → `next_index[F] = 4` |
| 3 | `prev=(3, t2)`, entries: [4, 5] | entry at 3 is **t3 ≠ t2** | **reject** → `next_index[F] = 3` |
| 4 | `prev=(2, t1)`, entries: [3, 4, 5] | entry at 2 is t1 ✓ | **accept**: truncate from 3, append [3,4,5] |

After round 4 the follower's log equals the leader's, and its bogus `(3, t3)`
entry is gone — **overwritten, not appended past**. That's V2's "conflicting
tail is repaired" box. Two things to internalize:

- **Why the induction works.** The follower matched at `(2, t1)`. When *that*
  entry was first accepted, the same check matched at index 1. Every accepted
  append extends a verified prefix, so matching at `(prev_index, prev_term)`
  implies matching at every earlier index — Log Matching, by induction. The
  check is O(1) per message but certifies the whole prefix.
- **Why overwriting is safe here.** The follower's `(3, t3)` was never on a
  majority (its term-3 leader died before replicating it) — so it was never
  committed, no client was ever acked, and discarding it loses nothing. The
  commit rule below is what guarantees this reasoning: *committed entries never
  need repair*, because a leader missing them can't get elected (V1's
  up-to-date check). Only uncommitted junk is ever truncated.

Round-by-round decrement is correct but slow when a follower is far behind —
that's what the [`conflict_index`](../src/rpc.rs) reply hint is for: the
follower volunteers where the leader should retry, collapsing many rounds into
one. What exactly the hint should say (first index of the conflicting term? the
follower's log length?) is a design choice the SPEC asks you to document.

One more wired-in duty of this same RPC: an `AppendEntries` with **empty**
`entries` is the **heartbeat**. Same consistency check, same term rules — it
just also resets the follower's election timer (the V1 seam). That's why
`handle_append_entries` is both "replication" and "the thing that suppresses
elections."

---

## 4. Commit: a majority fact, not a leader opinion

Now scenario #1: when may the leader tell the client "your write succeeded"?

The rule: an entry is **committed** once it is stored on a **majority** of
nodes. The reason is the same overlap arithmetic as V1, pointed the other way:
a committed entry lives on ≥ `quorum()` nodes; any future leader must win votes
from ≥ `quorum()` nodes; the two sets intersect (for N=5: 3+3=6 > 5), so **at
least one voter in any future election holds the entry** — and V1's up-to-date
check makes that voter refuse any candidate missing it. A committed entry
therefore survives *every* possible future election. An entry on fewer than a
majority has no such guarantee — some electable majority knows nothing of it.

This is why acking at "leader has it" (scenario #1) or even "leader + 1
follower of 5" is a lie: the write can be legitimately erased by a legitimately
elected leader. It's also why a 5-node cluster tolerates **2** failures (3
survivors still form a quorum) but a clean 2/3 partition of 3 nodes leaves the
2-side writable and the 1-side frozen — no majority, no progress, by design.

Mechanically, the leader learns "who has what" from `AppendEntries` successes:
`match_index[peer]` ([`Inner`](../src/node.rs)) records the highest index known
replicated on each peer. Sort those (counting yourself), and the median-ish
value — the highest index on a quorum — is your candidate commit point. Then
`commit_index` flows to followers in the next message (`leader_commit` in
`AppendEntriesArgs`), and every node applies up to it, in order (V3).

Except for one thing.

---

## 5. §5.4.2 — the commit rule's trap, traced

Here is the scenario that makes "majority = committed" *almost* right, and why
[`maybe_advance_commit`](../src/replication.rs)'s TODO insists the entry be
**from the leader's own term**. Five nodes S1–S5. Follow index 2 the whole way:

```
(a) S1 leads term 2. Appends idx2(t2); replicates it to S2 only. Crashes.

      S1: [1][2t2]   S2: [1][2t2]   S3: [1]   S4: [1]   S5: [1]

(b) S5 wins term 3 (votes: S3,S4,S5 — their logs end at idx1, S5's does too;
    equal is electable). Appends idx2(t3) LOCALLY. Crashes before replicating.

      S1: [1][2t2]   S2: [1][2t2]   S3: [1]   S4: [1]   S5: [1][2t3]

(c) S1 restarts, wins term 4 (votes: S1,S2,S3 — S5 is down or slow; S3's log
    ends at idx1 so S1's (t2, idx2) is more up-to-date). S1 resumes replicating
    idx2(t2) — it reaches S3. Now idx2(t2) is on {S1,S2,S3}: a MAJORITY.

      S1: [1][2t2]   S2: [1][2t2]   S3: [1][2t2]   S4: [1]   S5: [1][2t3]

    ⚠ The naive rule says: majority holds idx2 → commit it → ack the client.

(d) S1 crashes. S5 comes back and runs for term 5. Its last entry is (t3, idx2);
    S2/S3/S4's best is (t2, idx2) or (t1, idx1) — S5 is MORE up-to-date than all
    of them. S5 wins with votes {S2,S3,S4,S5} and, as leader, repairs everyone's
    log to match its own:

      ALL: [1][2t3]        ← idx2(t2) is GONE. Everywhere.
```

If step (c) committed idx2(t2), a client was told "success" for a write that
step (d) *legally* erased — every step above follows the V1/V2 rules perfectly.
The flaw isn't in election or repair; it's in (c)'s commit decision: **counting
replicas of an *old term's* entry proves it's popular, not that it's safe.**
S5's ability to win in (d) was never blocked, because idx2(t2) being on a
majority doesn't make anyone's *last term* newer than t3.

**The fix** (and the SPEC's V2 criterion): a leader only advances
`commit_index` to entries **from its own term**. In (c), S1 (term 4) may not
commit idx2(t2) directly. Instead it appends a new entry in term 4, replicates
it, and when *that* reaches a majority, everything before it — including
idx2(t2) — commits transitively (Log Matching: a node holding idx3(t4) provably
holds idx2(t2) beneath it). Now re-run (d): idx3(t4) is on {S1,S2,S3}, so S5's
last entry (t3) is *older* than theirs — S5 can no longer assemble a quorum. The
committed write is safe, arithmetically.

And that closes the loop on a V1 leftover: **why a fresh leader appends a
[`Noop`](../src/rpc.rs)** the moment it wins (`become_leader`'s TODO). Without
any new client write, a fresh leader would have *no* current-term entry, so
nothing — including perfectly-majority-replicated old entries — could commit,
and linearizable reads (which need a committed read point, V3) would stall. The
no-op is a free current-term entry that unjams commitment immediately.

Why this bug is infamous: it needs a *specific multi-election interleaving* to
fire. Code without the current-term guard passes every clean test, every
kill-one-leader test, and runs in production for years — until it doesn't. This
is why the SPEC demands a *constructed* §5.4.2 regression test as the Proof, and
why the [CONCEPTS](../CONCEPTS.md) card calls testing-with-clean-failures the
trap.

---

## 6. The client's view: `propose`, waiting, and redirects

The write path ([`propose`](../src/replication.rs)) ties it together: only the
leader accepts; the command becomes a log entry in the current term; the ack
waits until the entry is **committed and applied** — where V2's machinery meets
V3's state machine.

Two contract points the SPEC grades:

- **Non-leaders redirect, never serve.** A follower answering a write locally
  would fork history; silently dropping it lies. The scaffold's
  [`AppError::NotLeader`](../src/error.rs) carries the leader's address (via
  [`leader_hint()`](../src/node.rs)) so the handler can return a real redirect —
  "the client can always find the leader."
- **Leadership loss while waiting surfaces as `NotLeader`, not a hang.** The
  client then retries against the new leader — which means a command can be
  proposed *twice* (the old leader may have committed it just before dying).
  Raft is **at-least-once**; making it exactly-once at the client is V3's dedup
  stretch. Keep that thread for the next doc.

---

## 7. The design space you'll navigate (not the answers)

- **The follower's accept path ordering** — term check, heartbeat/step-down
  side effects, consistency check, conflict-suffix deletion, append,
  `commit_index = min(leader_commit, last_new_index)` (why the `min` matters is
  worth working out), and `persist()` before replying `true`. The interleaving
  is the challenge; the pieces are listed in `handle_append_entries`'s TODO.
- **`conflict_index` semantics** — how much the follower tells the leader, and
  what the leader does with it (the SPEC's Protocols box wants it documented).
- **When exactly to advance commit** — turning `match_index` + `quorum()` +
  the current-term guard into `maybe_advance_commit` without off-by-ones.
- **Lock discipline under fan-out** — `broadcast_append_entries` sends to all
  peers concurrently but must never hold the [node.rs](../src/node.rs) lock
  across an `.await`; replies mutate `next_index`/`match_index` under races
  with other replies, elections, and new proposals.
- **Batching & pipelining (stretch)** — multiple in-flight `AppendEntries` per
  peer buys throughput; the ordering constraint that must survive is the
  interesting question.

---

## 8. Mental model summary

| Mechanism | Question it answers | Failure it prevents |
| --- | --- | --- |
| Term on every message | "is this leader current?" | deposed leaders replicating (§1, #4) |
| `(prev_log_index, prev_log_term)` | "may these entries follow?" | gaps and silent divergence (#2) |
| Walk-back + truncate | "how do wrong logs heal?" | appending past a conflict (#3) |
| Log Matching | "how much does one match certify?" | O(n) verification per message |
| Majority commit | "when is a write durable?" | acked-then-lost writes (#1) |
| Quorum intersection | "why does majority suffice?" | any future leader lacking committed entries |
| **Current-term commit rule** | "when is majority *not* enough?" | §5.4.2: old-term entries erased after ack |
| No-op on election | "how does a quiet leader commit?" | stalled commits/reads after failover |
| `NotLeader` redirect | "what do non-leaders do?" | forked or dropped writes |

## 9. Where you'll build this

All five `todo!()`s in [replication.rs](../src/replication.rs), plus the no-op
half of [`become_leader`](../src/election.rs) and the durable half of
[`RaftLog::persist`](../src/log.rs). This doc unlocks V2's **Done when ALL
true** ([SPEC](../SPEC.md)):

- [ ] an acked write is on a majority, in order — and survives killing the leader
- [ ] Log Matching holds (checked, not assumed)
- [ ] a conflicting tail is overwritten to match the leader
- [ ] commit respects the current-term rule (a constructed §5.4.2 scenario loses nothing)
- [ ] committed entries apply in index order, exactly once, on every node
- [ ] writes to a non-leader redirect, never serve locally

Proofs: the replication/kill-leader test, the Log-Matching property test, the
tail-repair test, the §5.4.2 regression, the determinism test — and
`docs/09-design.md` records the commit rule + no-op decision.

Next: [02-how-the-state-machine-and-linearizable-reads-work.md](02-how-the-state-machine-and-linearizable-reads-work.md) —
turning the agreed log into a map, and reads that don't lie.
