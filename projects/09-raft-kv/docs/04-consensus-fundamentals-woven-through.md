# The Backend Fundamentals Woven Through This Project

> The four verticals get a doc each ([00](00-how-leader-election-works.md)–
> [03](03-how-snapshots-and-log-compaction-work.md)). This one covers the
> [SPEC](../SPEC.md)'s **horizontal checklist** — the disciplines that aren't a
> Raft rule but that Raft's correctness quietly *depends on*: what must hit disk
> before which reply, how one lock owns all consensus state without deadlocking
> the cluster, why a dead peer is Tuesday and not an error, who is allowed to
> talk to `/raft/*`, which three numbers tell you consensus is healthy, and what
> a jepsen-style harness proves that unit tests can't. No prior knowledge
> assumed beyond the vertical docs.
>
> Anchored to: [log.rs](../src/log.rs) (`persist`, `open`),
> [node.rs](../src/node.rs) (the `Mutex<Inner>` model), [peer.rs](../src/peer.rs)
> (transport, wired), [error.rs](../src/error.rs) (`Transport`, `NotLeader`),
> [routes.rs](../src/routes.rs), and the SPEC's horizontal boxes + Definition of
> done items 2–4.

---

## 0. The one sentence to hold onto

**Raft's proofs assume an environment — promises survive crashes, state
mutations are atomic with respect to each other, and peers fail silently rather
than fatally — and the horizontal checklist is you *building* that environment,
because the theorem is void wherever the assumptions don't hold.**

---

## 1. The persistence contract: durable *before* the reply leaves

Raft splits node state in two, and [log.rs](../src/log.rs)'s layout mirrors it:

| class | fields | on restart |
| --- | --- | --- |
| **persistent** | `current_term`, `voted_for`, the log entries (+ snapshot base) | reloaded — the node *is* this data |
| **volatile** | `commit_index`, `role`, `leader_id`, `next_index`/`match_index` | rebuilt from zero, safely |

Why is `commit_index` — the most important-sounding number in the system —
*volatile*? Because it's re-derivable: commitment is a majority fact (doc 01),
and a restarted node re-learns it from the next leader message. But
`voted_for` is a **promise to other nodes**, and the log entries are
**acknowledged data**. Promises and data must outlive the process that made
them.

The contract has a precise temporal shape, and it's the same shape as every
durability system you'll build (project 08's log, project 22's WAL):

> **Persist, *then* reply.** The fsync completes before the RPC answer is sent
> — never the other way around, never "eventually."

Each field has a named disaster if you get the order wrong — the SPEC's
"persisted **before** any RPC that depends on them is answered" box:

| field persisted late | the disaster | traced in |
| --- | --- | --- |
| `voted_for` | vote, crash, forget, vote again → **two leaders in one term** | doc 00 §5, step by step |
| `current_term` | reboot into an old term where the old vote no longer binds — same double-vote by another door | doc 00 §5 |
| log entries | follower says "I have it," crashes, forgets; leader counted it toward the majority → an **acked write on fewer nodes than quorum math believes** | doc 01 §4's overlap argument, voided |

The scaffold hands you the seam: [`RaftLog::persist`](../src/log.rs) is the
`todo!()`, and the recovery half lives in `open` (currently starts empty — the
restart tests will catch that immediately). Two decisions are deliberately
yours, called out in `persist`'s TODO:

- **Atomicity of the write itself** — a crash *mid-persist* must not leave a
  half-written file that recovery half-trusts. The SPEC's recovery box says it
  as an outcome: restart finds a **clean entry boundary**, "a torn tail is never
  read as real." Write-to-temp-then-rename and append-only-records-with-
  per-record-integrity are the two classic shapes; each pays differently.
- **The throughput dial** — fsync-per-write is the safe, slow corner; batching
  amortizes it but widens what one crash can lose *before any reply was sent*
  (losing un-acked work is legal; losing acked work never is). This is the same
  dial as project 08, now with a correctness floor under it.

Graceful shutdown (the Protocols box) is this contract's exit path: on SIGTERM,
drain in-flight requests and flush persistent state *before* the process dies —
a planned death should never look like a crash.

---

## 2. One lock, never held across an `.await`

[node.rs](../src/node.rs) puts *all* mutable consensus state behind a single
`Mutex<Inner>`, and its module docs state the discipline the SPEC's
"cross-cutting" box grades: **hold it to read or mutate, never across an
`.await`** — snapshot what you need, drop the guard, then do I/O.

Both halves are deliberate, and the SPEC wants them *documented as* deliberate:

**Why one lock, not one per field?** Raft's invariants span fields: "term, vote,
role, and log move together." A candidate checks its term, appends to its log,
and flips role as *one* decision. With per-field locks, another task can
interleave between your term check and your role flip — and every such
interleaving is a potential two-leaders bug that no test reliably catches.
Coarse locking makes the invariant checkable at one `lock()` boundary.
(Contention is a non-issue at this scale: critical sections are microseconds of
pointer work — *if* you keep I/O out of them.)

**Why never across an `.await`?** Two failure modes, one worse than the other:

1. *Deadlock:* task A holds the lock and awaits a reply from a peer; handling
   that reply (or any inbound RPC) needs the lock → the node freezes. With a
   `std::sync::Mutex` in async code this is near-immediate.
2. *The subtler one:* even without deadlock, a lock held across a peer RPC means
   **a slow peer stalls your whole node** — your 500 ms-timeout call to a
   wedged follower ([peer.rs](../src/peer.rs)) blocks your election timer, your
   client writes, everything. You've imported a remote machine's latency into
   your local critical section, which un-does the entire point of
   fault-tolerance.

The discipline forces a pattern you'll use in every vertical (it's spelled out
in `start_election`'s and `broadcast_append_entries`'s TODOs): **lock →
snapshot the values the RPC needs → unlock → await the fan-out → re-lock →
re-validate before acting on replies.** That last step is the part people skip:
the world may have moved while you were awaiting (a higher term arrived, you
were deposed, the log grew) — a reply is evidence about the *past*, and the
re-validation is what keeps stale evidence from driving current decisions.

---

## 3. A down peer is a missing answer, not an error

[peer.rs](../src/peer.rs)'s module docs say it and the SPEC's partition box
grades it: every `PeerClient` call can fail — connection refused, 500 ms
timeout, mid-flight partition — and that is **the normal case Raft exists for**,
not an exceptional one. [`AppError::Transport`](../src/error.rs) even
annotates itself: "expected and normal in a cluster."

The design consequence runs through every vertical: quorum logic is written as
**"did I hear K yeses?"**, never "did everyone answer?". An erroring peer this
round is indistinguishable from a slow one — and must be treated identically:
no vote from them, no `match_index` advance, carry on. The cluster runs on any
majority; `?`-propagating a peer error out of an election or a broadcast turns
a tolerated fault into a self-inflicted outage. (Contrast with the *client*
path, where errors are real errors — the split is visible in
[error.rs](../src/error.rs)'s variants.)

---

## 4. Trust boundaries: two doors, two rules

The scaffold serves two very different audiences on one port
([routes.rs](../src/routes.rs)), and the SPEC's Security boxes ask you to treat
them differently:

| door | who should call it | what an attacker gets otherwise |
| --- | --- | --- |
| `/kv/*` (client API) | authenticated clients | an open KV endpoint **is** an open datastore — read, overwrite, delete everything |
| `/raft/*` (peer RPC) | the other nodes, *only* | the ability to **drive consensus itself** |

The second row deserves the pause. `/raft/*` handlers *obey the protocol*: an
unauthenticated caller can POST a `RequestVote` with `term: 10^15` (every node
steps down and the cluster thrashes — term inflation as a DoS), or craft
`AppendEntries` to **inject arbitrary entries** into the replicated log —
writes that bypass the client API entirely. Raft's rules authenticate *terms*,
not *senders*; the protocol assumes every message comes from a cluster member.

That assumption is fine — etcd runs the same way — but it must be **stated,
not accidental**: the SPEC requires the design doc to name the trust model
(private network between nodes, or mTLS) plus, at minimum, key/size validation
on client input. The horizontal box is satisfied by an *honest sentence*, and
failed by silence.

Client-side, the bar is the usual one: writes (and reads) behind a credential,
and keys never logged — the never-log-secrets rule from the repo's CLAUDE.md
applies to *user data* here too.

---

## 5. Watching consensus: the three lags that tell the story

A consensus system's failures are *quiet* — no 500s, just a cluster that
stopped agreeing. The SPEC's Observability boxes pick the signals that make the
quiet visible; each maps to a specific pathology, and
[`status()`](../src/node.rs) already exposes most of the raw numbers:

| signal | healthy looks like | when it moves, suspect |
| --- | --- | --- |
| **current term** | flat for hours | rising = elections are happening: a flapping node, timeouts too tight vs. real network latency, or a partitioned node inflating terms (doc 00 §8) |
| **role** (per node) | one stable leader | churn = same causes, seen from the other side |
| **commit − last_applied lag** | ~0 | growing = the *apply loop* is wedged — consensus agrees, the state machine isn't consuming; doc 03's compaction-trap precondition |
| **per-follower `match_index` lag** (leader's view) | ~0 | one follower growing = it's down/slow/partitioned — and predicts an `InstallSnapshot` (doc 03 §5) once compaction passes it |

Note the division of labor: term/role watch **election health**, commit-applied
watches **local apply health**, match_index watches **replication health** —
three different subsystems, three different on-call stories. The structured-log
events the SPEC lists (election started, became leader/follower, term change,
snapshot taken, `InstallSnapshot` sent) are these same transitions as a
narrative; `tracing` spans with request ids come free via `common-telemetry`.

---

## 6. The jepsen-lite harness: testing the *claims*, not the code

The Definition of done's item 4 is a different *kind* of test, and the
distinction is the concept. Unit and integration tests check *mechanisms* under
*chosen* scenarios — the §5.4.2 regression checks one interleaving you thought
of. But every vertical doc had a bug whose signature was "passes every test
that doesn't manufacture exactly the right chaos": the double-vote needs a
crash *between* vote and re-request; the stale read needs a partition that
isolates a *leader*; §5.4.2 needs three elections in a specific order.

A jepsen-style harness inverts the approach: **inject faults randomly while
checking the *claim* continuously.**

```
   ┌─ nemesis ────────────────┐      ┌─ workload ───────────────┐
   │ kill leaders, partition,  │  +   │ concurrent clients:       │
   │ heal, restart — randomly  │      │ PUT/GET/DELETE, recording │
   └──────────────────────────┘      │ every op + ack + result   │
                                     └────────────┬──────────────┘
                                                  ▼
                    checker: does the recorded history satisfy
                    linearizability? was any ACKED write ever lost?
```

The checker doesn't know *which* interleaving happened — it verifies the
system's promise held across *whatever* happened. That's why it catches the
bug class you didn't think to write a test for: it explores the space instead
of sampling your imagination. (The real Jepsen found acked-write loss in etcd,
MongoDB, and most systems it touched — almost always via an interleaving no
author had enumerated.) Your version is "lite": the same shape — nemesis +
recorded concurrent history + a no-lost-acked-writes / linearizability check —
at single-process-cluster scale. It's also the natural arena for the SPEC's
failover-time benchmark: the leader-kill distribution falls out of the same
harness.

---

## 7. Mental model summary

| Discipline | The rule | What breaks without it |
| --- | --- | --- |
| Persistence contract | fsync before the reply that depends on it | double votes; acked writes under quorum (docs 00/01) |
| Clean recovery boundary | a torn tail is never read as real | a half-entry resurrected as history |
| One lock | invariants that span fields check at one boundary | interleavings between check and act |
| Never lock across `.await` | snapshot → drop → I/O → re-lock → re-validate | deadlock; a slow peer stalls the node |
| Peer failure = missing answer | quorum asks "K yeses?", never "everyone?" | a tolerated fault becomes an outage |
| Trust boundary stated | `/raft/*` assumes cluster-member senders — say how | term-inflation DoS; log injection past the API |
| Three lags | term/role, commit−applied, match_index | quiet failures with no page |
| Jepsen-lite | inject chaos, check the claim, not the scenario | the interleaving you didn't imagine |

## 8. Where this lands

No single module — that's the point of "horizontal." The persistence contract
lands in [`RaftLog::persist`/`open`](../src/log.rs) and in *every* RPC handler's
reply ordering; the lock discipline in every vertical's fan-out; peer-failure
tolerance in `start_election` and `broadcast_append_entries`; auth and
validation at [routes.rs](../src/routes.rs); metrics beside
[`status()`](../src/node.rs); the harness in the Definition of done's item 4.

The boxes this doc unlocks are the SPEC's entire **horizontal checklist**
(Protocols, Durability & recovery, Security, Observability, Cross-cutting) plus
Definition-of-done items 3 (the design-doc decisions) and 4 (jepsen-lite).

That's the full set — docs 00–04. Read them, then start the SPEC's suggested
order of attack: the single-node boring path first, then `/quest` V1.
