# How Leader Election Works — From First Principles

> A ground-up guide to Raft leader election: how N equal machines agree that
> exactly one of them is in charge, and — the actually hard part — how they
> guarantee that **two of them never both think so**, even while machines crash,
> reboot with amnesia, and the network cuts them off mid-sentence. No prior
> distributed-systems knowledge assumed.
>
> This prepares you for **V1** of the [SPEC](../SPEC.md) and the three `todo!()`s
> in [election.rs](../src/election.rs): `handle_request_vote` (the voter),
> `start_election` (the candidate), and `become_leader` (the winner). The wired
> types you'll lean on live in [rpc.rs](../src/rpc.rs) (`RequestVoteArgs/Reply`),
> [node.rs](../src/node.rs) (`Role`, `quorum()`, `random_election_timeout()`,
> `become_follower`), and [log.rs](../src/log.rs) (`current_term`, `voted_for`,
> `last_index`/`last_term`, `persist`). It teaches the *ideas*; the decisions in
> the `todo!()`s stay yours — reach for `/hint` or `/quest` when you build.

---

## 0. The one sentence to hold onto

**An election is safe not because a leader is chosen, but because the rules make
it *arithmetically impossible* for two nodes to each collect a majority of votes
in the same term — and that impossibility survives crashes only if every vote is
on disk before it is spoken.**

Everything in V1 is in service of that sentence: terms exist to scope "the same
election", the one-vote-per-term rule makes majorities exclusive, `persist()`
makes the rule survive a reboot, and the up-to-date check makes sure the winner
is a node that can be trusted with history.

---

## 1. Why one leader at all — split-brain, concretely

Start with the disease before the cure. Suppose a 5-node KV cluster lets *any*
node accept writes, and the network splits:

```
   ┌─────────────┐        ╎        ┌──────────────────┐
   │   A     B   │   partition     │   C    D    E    │
   └─────────────┘        ╎        └──────────────────┘
 client1: PUT x=1 → A              client2: PUT x=2 → D
```

Both writes are accepted. Both clients got `200 OK`. When the partition heals,
the cluster holds `x=1` *and* `x=2`, each acknowledged, with no principled way to
pick a winner — timestamps lie (clocks drift), "last writer wins" silently
discards an acknowledged write either way. That is **split-brain**, and the
damage is silent: nothing errors; data is just wrong.

Raft's answer is structural: **all writes flow through one leader**, so
conflicting writes can never both be accepted in the first place. But that just
moves the problem — now *choosing the leader* is where split-brain can sneak in.
If two nodes can simultaneously believe "I am the leader," you've rebuilt the
disease one layer up. So the election protocol is not a liveness feature
("choose someone quickly"); it is a **safety** mechanism ("never choose two"),
and V1 is graded on the never.

---

## 2. Naive elections and how each one breaks

It's worth trying to invent this yourself, because each obvious design fails in
an instructive way:

| Naive design | How it breaks |
| --- | --- |
| **Lowest node ID is leader** | Node 1 hangs for 30 s (GC pause, disk stall). Is it dead or slow? Nobody can tell — and if node 2 takes over while node 1 is merely slow, you have two leaders when node 1 wakes up. |
| **First node to claim it** | Two nodes claim simultaneously. Both broadcast "I'm leader." Ties are the common case, not the corner case. |
| **Everyone votes; plurality wins** | 5 nodes, votes split 2/2/1. Nobody has a majority; a plurality winner and a rerun winner can coexist across the retry. |
| **Majority vote, one round** | Better — but a node that votes, crashes, and reboots *forgets its vote* and can vote again, letting two candidates each reach "majority" using the same voter twice (§5, traced below). |
| **Majority vote + memory, no log check** | Safe from two leaders — but a node that slept through the last 1,000 writes can win, become leader, and *overwrite committed history* with its stale log. |

Raft is the last row's design plus the two patches: **persist the vote** and
**refuse out-of-date candidates**. The rest of this doc builds those pieces in
order.

---

## 3. Terms: a logical clock everyone can agree on

Real clocks can't referee this (machines drift; a partitioned node's clock keeps
ticking). Raft instead numbers time itself: a **term** is a monotonically
increasing integer, and each term contains **at most one leader — possibly
none**. In the scaffold it's just [`pub type Term = u64`](../src/rpc.rs).

Three rules give the term its power, and they appear in *every* RPC handler you
write (which is why [node.rs](../src/node.rs) centralizes `become_follower`):

1. **Every message carries the sender's term.** There is no message in Raft
   without one — look at `RequestVoteArgs`, `AppendEntriesArgs`,
   `InstallSnapshotArgs` in [rpc.rs](../src/rpc.rs).
2. **Higher term always wins.** A node seeing a higher term than its own adopts
   it immediately and reverts to follower — whatever it was doing. A leader that
   learns of a higher term is *deposed by that one integer comparison*.
3. **Lower term is always rejected.** A message from an older term is answered
   with "no, and here's my term" so the stale sender learns it's been left
   behind (`RequestVoteReply.term` exists for exactly this).

Why this works as a clock: a term is only ever *entered* by a candidate
incrementing it, so "term 7" globally names one specific election attempt. Two
nodes disagreeing about who leads term 7 is the disaster; two nodes being *in
different terms* is routine and self-resolving (rule 2 collapses them to the
higher one). Terms are never reused: a rebooted node reloads its persisted
`current_term` rather than restarting at 0 — one of the reasons
[`RaftLog::persist`](../src/log.rs) covers the term, not just the vote.

---

## 4. One election, traced end to end

A healthy 3-node cluster, using the scaffold's real defaults from
[main.rs](../src/main.rs): heartbeat every **50 ms**, election timeout drawn
uniformly from **150–300 ms** per attempt (`random_election_timeout()`).

```
 time →
 Node 1 (leader, term 4):  ♥──♥──♥──✗ crash
 Node 2 (timeout: 212ms):  ...........(silence).........[212ms: timeout!]
 Node 3 (timeout: 281ms):  ...........(silence)..................│
                                                                 │
 Node 2 becomes CANDIDATE:                                       │
   term 4 → 5, votes for itself, persists, then asks everyone:   │
   RequestVote{term:5, candidate_id:2,                           │
               last_log_index:9, last_log_term:4} ──────────► Node 3
                                                                 │
 Node 3 (still a follower, term 4):                              │
   term 5 > 4 → adopt term 5                                     │
   voted_for this term? None. Candidate's log ≥ mine? Yes.       │
   → record voted_for=2, PERSIST, reset election timer,          │
     reply {term:5, vote_granted:true}                           │
                                                                 │
 Node 2: grants = {self, node 3} = 2 ≥ quorum() = 2  → LEADER (term 5)
   → immediately heartbeats, so node 3's 281ms timer never fires
```

Note what each mechanism did: the *randomized* timeouts meant node 2 moved first
and node 3 was still a follower (able to vote) when the request arrived; the
*persist-before-reply* on both sides pinned the term bump and the vote to disk;
and the winner's *immediate heartbeat* is what suppresses further elections.
This whole dance is the two-timer `select!` loop described in
[`RaftNode::run`](../src/node.rs)'s TODO — the driver you'll write.

Also note the timing constraint hiding in the numbers: the heartbeat interval
(50 ms) must sit comfortably below the election-timeout floor (150 ms), or
followers time out *under a healthy leader* and elect over it forever. That
relationship is why [`RaftConfig`](../src/node.rs) keeps both knobs side by side.

---

## 5. Safety rule 1: one vote per term — *and the disk remembers*

Majorities are exclusive by arithmetic: in a cluster of N, a quorum is
`N/2 + 1` ([`RaftNode::quorum`](../src/node.rs)), and two quorums must overlap —
for N=5, two sets of 3 drawn from 5 nodes share at least one member
(3 + 3 = 6 > 5). If every node votes **at most once per term**, that shared
member voted for at most one of the two candidates, so at most one candidate can
reach a quorum. That's the entire two-leaders proof. One overlap voter, one vote,
one winner.

Now watch a reboot destroy it. Five nodes A–E, term 5, candidates B and D:

| step | event | A's `voted_for` (in memory only) |
| --- | --- | --- |
| 1 | B requests A's vote; A grants | `Some(B)` |
| 2 | **A crashes and reboots** | `None` — *forgotten* |
| 3 | D requests A's vote in the *same* term 5; A checks `voted_for`: none → grants | `Some(D)` |
| 4 | B counts {A, B, C} = 3 = quorum → **leader, term 5** | |
| 5 | D counts {A, D, E} = 3 = quorum → **leader, term 5** | |

The two "majorities" overlap only at A — and A voted twice, so the overlap
argument collapses. Two leaders, same term, split-brain with extra steps. This is
why the SPEC's V1 criteria include *"that vote survives a restart"*, why
[log.rs](../src/log.rs) groups `voted_for` and `current_term` with the entries as
**persistent** state, and why the `handle_request_vote` TODO says
`persist()` **before** replying: once the "yes" leaves your mouth, the promise
must already be on disk. A persist-after-reply has a crash window in which the
promise was heard but not remembered — exactly step 2 above.

(`current_term` needs the same treatment for the same reason: a node that forgets
its term reboots into an old term, where its old vote no longer binds it.)

---

## 6. Safety rule 2: the up-to-date check — protecting committed history

One-vote-per-term stops *two* leaders. It does not stop the *wrong* leader: a
node that was partitioned away for the last 1,000 committed writes can still time
out, campaign, and win — and a leader's log is the law (V2 makes followers
conform to it), so a winner with a stale log would erase committed entries.

The fix: a voter **refuses any candidate whose log is less up-to-date than its
own**. "Up-to-date" compares the pair `(last_log_term, last_log_index)`
lexicographically — term first, length as tiebreak. The candidate ships its pair
in `RequestVoteArgs`; the voter reads its own from
[`log.last_term()` / `log.last_index()`](../src/log.rs). Worked comparisons,
voter's last entry = **(term 3, index 7)**:

| candidate's last entry | voter's decision | why |
| --- | --- | --- |
| (term 2, index 9) | **refuse** | older last term — length can't save it |
| (term 3, index 6) | **refuse** | same term, shorter log |
| (term 3, index 7) | grant | at least as up-to-date (equal) |
| (term 4, index 1) | grant | newer last term — length irrelevant |

Row 1 is the counterintuitive one: a *longer* log loses to a *newer* one. Length
measures how much a node wrote; the last entry's term measures how *recent* the
leader it followed was — and recency is what correlates with holding committed
history.

Why this protects committed data — quorum overlap again, used a second way: an
entry is only *committed* once a **majority** stores it (V2). A candidate needs a
**majority** of votes to win. Any two majorities intersect, so at least one voter
in any winning coalition *holds every committed entry* — and that voter's
`(last_log_term, last_log_index)` is ≥ the committed entry's. If the candidate is
missing committed history, that voter's log beats the candidate's, the vote is
refused, and the coalition falls short of quorum. Stale candidates
arithmetically cannot win. (The precise version is Raft §5.4.3; the intuition
above is what you should be able to reproduce at a whiteboard.)

Notice the check is *"at least as up-to-date"*, not *"more"* — a candidate equal
to the voter is fine. Over-tightening this check is a real bug class: it can make
a perfectly qualified candidate unelectable and stall the cluster.

---

## 7. Split votes: solved by dice, not cleverness

Two followers time out near-simultaneously, both become candidates in term 6,
and each grabs half the remaining votes: nobody reaches quorum. Term 6 elects
**no one** — which is *allowed* ("one leader per term, **or none**"). The
question is only how fast term 7 fixes it.

A **fixed** timeout would never fix it: both candidates time out again at the
same instant, collide again, forever. Raft's answer is embarrassingly simple:
each attempt draws a **fresh random timeout** ([`random_election_timeout()`](../src/node.rs)
redraws from 150–300 ms every time). A collision needs both nodes to fire within
about one round-trip of each other; with a 150 ms spread and a ~10 ms RTT window,
that's roughly a 13% chance per round (`1 − (140/150)² ≈ 0.129`) — so the
probability of *k consecutive* ties decays as `0.129^k`: about 1.7% for two
rounds, 0.2% for three. Ties aren't prevented; they're made **self-healing**,
with no coordination and no extra protocol. That's the V1 criterion "repeated
ties are self-correcting", and it's also why the loser of a randomization race
must gracefully return to follower when it sees the winner's heartbeat.

---

## 8. The leaderless gap — CAP, lived

Between a leader's death and its successor's election, the cluster serves **no
writes**. With the default timings that window is bounded by roughly one
election timeout plus a vote round — a few hundred milliseconds — and the SPEC's
Definition of done asks you to *measure* it (failover-time distribution).

This is not a flaw to engineer away; it is the **choice**. A system that kept
accepting writes during the gap (each partition electing its own leader) would be
available and wrong. Raft picks consistency: briefly unavailable, never
two-leadered. Sit with the contrast to project 07's cache, which picks the other
side — together they're the CAP spectrum as lived experience, and the SPEC's
`in the wild` list (etcd, Consul, TiKV, CockroachDB) all made this same call.

One liveness wart worth knowing before you hit it: a node partitioned *away*
keeps timing out and inflating its term (it can never win — no quorum — but it
keeps incrementing). When the partition heals, its inflated term deposes the
healthy leader (rule: higher term wins) and forces a gratuitous re-election. The
committed data is safe (up-to-date rule), but availability hiccups. The SPEC's
**pre-vote** stretch exists to close exactly this: a would-be candidate first
asks "*would* you vote for me?" without bumping any terms, and only campaigns
for real if a quorum says yes.

---

## 9. The design space you'll navigate (not the answers)

The concept is fixed; these decisions in the `todo!()`s are yours:

- **Voter logic order** — `handle_request_vote` interleaves four concerns (term
  comparison, prior-vote check, up-to-date check, persist-then-reply). The
  ordering and the "already voted *for this same candidate*" re-grant case are
  where the bugs live.
- **Candidate concurrency** — `start_election` must canvass all peers *in
  parallel* without holding the state lock across an `.await`
  ([node.rs](../src/node.rs)'s locking rule): snapshot what you need, drop the
  guard, fan out, re-check you're *still* a candidate *in the same term* before
  crowning yourself. What can change between snapshot and reply, and what you
  must re-validate, is the interesting part.
- **Timer reset discipline** — which events reset the election timer (a valid
  heartbeat? a granted vote? a *rejected* vote?) decides both liveness and
  safety. Resetting too eagerly can let a disruptive node suppress elections;
  too lazily causes spurious ones.
- **What "failed peer" means** — [`PeerClient`](../src/peer.rs) calls return
  errors routinely (down peer, 500 ms timeout). An error is a missing vote this
  round, never a fatal error — the cluster runs on any majority.

When you're ready to build, that's `/quest` V1; if you wedge on one of these,
`/hint`.

---

## 10. Mental model summary

| Mechanism | Question it answers | Failure it prevents |
| --- | --- | --- |
| Single leader | "who may write?" | conflicting concurrent writes (split-brain) |
| Term | "which election is this?" | ambiguity about *when* someone led |
| Higher-term-wins step-down | "how do stale nodes learn?" | deposed leaders acting on old authority |
| One vote per term | "can two win?" | two quorums in one term (overlap voter votes once) |
| `persist()` before reply | "does a crash erase promises?" | the reboot double-vote (§5 trace) |
| Up-to-date check | "can a stale node win?" | a leader that would overwrite committed history |
| Randomized timeout | "how do ties end?" | infinite split-vote loops |
| Quorum = N/2 + 1 | "why a majority?" | any two majorities share a member — the root of every proof above |

## 11. Where you'll build this

Everything lands in [election.rs](../src/election.rs) —
`handle_request_vote`, `start_election`, `become_leader` — driven by the
two-timer loop you'll write in [`RaftNode::run`](../src/node.rs), persisting via
[`RaftLog::persist`](../src/log.rs) (also a `todo!()`, and part of the deal).

This doc unlocks V1's **Done when ALL true** ([SPEC](../SPEC.md)):

- [ ] all healthy → exactly one leader; others follow (`/status` converges)
- [ ] at most one leader per term, ever, in any run
- [ ] at most one vote per term, and the vote survives a restart
- [ ] a less-up-to-date candidate cannot win a quorum
- [ ] split votes self-resolve within a few cycles (randomized timeouts)
- [ ] killing the leader yields a new leader in bounded time; terms never reused

Proofs live in the integration tests V1 names, and the persisted-state +
up-to-date-comparison decisions get written down in `docs/09-design.md`.

Next: [01-how-log-replication-and-commit-work.md](01-how-log-replication-and-commit-work.md) —
what the winner actually *does* with its authority.
