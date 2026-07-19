# SWIM: Gossip Membership and Failure Detection — From First Principles

> What this teaches: how a cluster of nodes agrees on *who is alive* with no
> coordinator, no ZooKeeper, no central registry — why all-to-all heartbeats
> collapse, how SWIM's randomized probing keeps per-node cost constant, and why
> "suspicion" and "incarnation numbers" exist at all. No prior knowledge assumed.
> Prepares you for **V3** in [SPEC.md](../SPEC.md) — the layer you'll build in
> [membership.rs](../src/membership.rs), where the probe loop and
> `handle_datagram`'s message dispatch are `todo!()` (the UDP socket, member
> table, and wire types are already scaffolded).

---

## The one sentence to hold onto

**SWIM splits "is X dead?" into two cheap, separately-tuned machines — randomized
probing (each node pings *one* random peer per round, asking others to double-check
before accusing) and gossip dissemination (news piggybacks on the pings) — so both
message load per node and detection time stay flat as the cluster grows.**

---

## 1. The problem: agreeing on the live set, with nobody in charge

The ring (V2) answers "who owns key k?" — *given the list of live nodes*. But who
maintains that list? In this project, nothing does. There's no registry service;
`docker-compose.yml` starts three equal peers where `cache-b` and `cache-c` know
exactly one fact: `SEEDS: "cache-a:7070"`. From that single address, every node
must discover the full cluster, notice deaths, and converge on the same view —
because a node whose view is wrong routes requests to a corpse.

The naive design: **everyone heartbeats everyone**. Each node pings all N−1
peers every interval T; no reply for a while → dead. Two failure modes:

| Failure mode | Why |
|---|---|
| **O(n²) messages per interval** | N nodes × (N−1) targets. 10 nodes → 90 pings/interval, fine. 1,000 nodes → ~1,000,000 pings/interval. The membership protocol becomes the load. |
| **False positives exactly when it hurts most** | One dropped UDP packet looks identical to a dead node. Packets drop *under load* — so the busiest moment is when healthy nodes get declared dead, their keys remap (V2!), the rebalance adds more load, more packets drop… a self-amplifying failure. |

The SPEC encodes both as criteria: per-round message load must be **bounded and
independent of cluster size**, and a single dropped packet must **not** evict a
healthy node.

## 2. Idea one: probe *one random* peer per round

SWIM's first move: stop pinging everyone. Each round (every `PROBE_INTERVAL`),
each node picks **one random alive peer** and pings it. Per-node send load:
exactly 1 ping/round, whether the cluster has 3 nodes or 3,000. Constant. Done.

But wait — if I only ping a random peer, a dead node isn't checked by *me* most
rounds. Does detection get slow? No, because *everyone* is probing randomly.
With N nodes each picking one of their N−1 peers uniformly, the probability that
a given dead node gets probed by *someone* in a round is

```
1 − (1 − 1/(N−1))^(N−1)   →   1 − 1/e  ≈ 63%       (for large N)
```

Computed: 70% at N=4, 64% at N=16, 64% at N=64 — the detection probability per
round is essentially *independent of cluster size*, which means expected
detection latency is a small constant multiple of the probe interval (≈1.6
rounds to first probe), no matter how big the cluster gets. That's the whole
magic: randomness converts "everyone must check everyone" into "someone checks
each node, soon, with high probability."

## 3. Idea two: get a second opinion before accusing (indirect probing)

Now the false-positive problem. I ping X, no ack. What do I actually know?
Honestly: almost nothing. Maybe X is dead. Maybe my outbound packet dropped.
Maybe X's ack dropped on the way back. Maybe *my* network link is congested.
Maybe X paused for 200 ms of GC. "I can't reach X" ≠ "X is dead" — reachability
is a property of the *pair*, not of X.

So before accusing, SWIM asks for second opinions. This is the `PingReq` message
already defined in [membership.rs](../src/membership.rs)'s `GossipMessage`:

```
me ──Ping──▶ X          ...no Ack (packet lost — X is actually fine)
│
├─PingReq{target:X}─▶ peer-1 ──Ping──▶ X ──Ack──▶ peer-1 ──relay──▶ me
└─PingReq{target:X}─▶ peer-2 ──Ping──▶ X ──Ack──▶ peer-2 ──relay──▶ me
                                                             │
                                              X survives: someone reached it
```

I ask `k` random peers (the **fan-out** — one of the tunables `docs/07-design.md`
asks you to record): "ping X for me and relay what you hear." Only if the direct
ping *and all k indirect routes* fail does X become **suspect**. A single lost
packet now needs k+1 independent path failures to cause a false accusation —
that's the V3 criterion "a single dropped packet does not evict a healthy node",
and the SPEC's Proof demands a test for exactly this.

Note k is a constant (typically 3), so the load bound survives: worst case per
round is 1 ping + k ping-reqs — still independent of N.

## 4. Idea three: suspicion is an accusation, not a verdict

Even after indirect probes fail, SWIM still doesn't declare death. The target
enters **Suspect** — the middle state of the `MemberState` lifecycle already
scaffolded (`Alive → Suspect → Dead`):

```
            probe + k indirect probes all fail        suspicion timeout expires
   Alive ─────────────────────────────────────▶ Suspect ─────────────────────▶ Dead
     ▲                                             │
     └────────── refutation: the accused ──────────┘
                 re-announces itself Alive
                 with a HIGHER incarnation number
```

Why the grace period? Because the accused might be *alive and merely slow* — GC
pause, CPU spike, transient partition. The suspicion is gossiped (next section),
so it eventually reaches X itself. A node that hears "you are suspect" gets to
object: it re-broadcasts itself as `Alive` — but with its **incarnation number**
incremented.

**Why incarnation numbers must exist.** Without them, you get an unresolvable
argument. Node A gossips "X is suspect"; X gossips "I'm alive". Both messages
circulate through the cluster in arbitrary order, forwarded and re-forwarded.
Which is *newer*? Wall-clock timestamps don't work — clocks across machines
aren't comparable. SWIM's answer: a per-node counter that **only the node itself
may increment** (this is the crucial rule — CONCEPTS.md's card asks exactly
"who increments them"). The merge rule, which the `handle_datagram` TODO in
[membership.rs](../src/membership.rs) describes, becomes purely mechanical:

| You know | Gossip arrives | Winner |
|---|---|---|
| X alive, incarnation 4 | X suspect, incarnation 4 | suspect (accusation beats alive *at the same incarnation*) |
| X suspect, incarnation 4 | X alive, incarnation **5** | alive — only X can have produced inc 5, so this is X provably speaking *after* hearing the accusation |
| X alive, incarnation 5 | X suspect, incarnation 4 | ignore — a stale rumor still bouncing around the cluster; without the counter this zombie accusation would re-kill X (**flapping**) |

Refutation is authoritative because a higher incarnation could only come from
the accused itself, after the accusation. That's the V3 criterion "the
suspect → dead lifecycle uses incarnation numbers so a wrongly-suspected node
can refute and stay alive (no flapping)". The scaffold's `Member.incarnation`
field carries this.

The **suspicion timeout** is the knob balancing the two errors: shorter = faster
detection of real deaths, but less time for a slow-but-alive node to refute =
more false evictions. CONCEPTS.md's trap is worth engraving: tune it too short
and a GC pause gets nodes evicted, the ring churns, rebalance load causes more
pauses — *the failure detector causes the failures it detects*. Record your
probe interval, suspicion timeout, and fan-out in `docs/07-design.md`.

## 5. Idea four: news travels by piggybacking (gossip dissemination)

So one node now knows "X is dead". How do the other N−2 find out? Broadcasting
would reintroduce O(n) messages per event. SWIM's answer: **don't send any new
messages at all**. Every `Ping`, `Ack`, and `PingReq` already flying around
carries a small batch of recent membership updates — the `updates:
Vec<MemberUpdate>` field already present on every variant of the scaffold's
`GossipMessage`. Each node keeps recent news and attaches it to whatever it was
sending anyway.

This spreads epidemically: round 1, I tell my probe target; round 2, two of us
each tell someone; 4, 8, 16… Informed count roughly doubles per round until
saturation, so full dissemination takes **O(log n) rounds** — that's why the V3
criterion says a join must converge "within a *bounded number of gossip
rounds*". Simulated with fan-out 1 (each informed node infects one random peer
per round): n=8 → ~6 rounds, n=32 → ~10, n=128 → ~13. Quadrupling the cluster
adds ~3 rounds — logarithmic growth in time, while per-node traffic never rises.

The join flow ties it together (the `Join` variant + `SEEDS` env var): a new
node sends `Join` to one seed; the seed records it and starts gossiping
"cache-b is alive, inc 0"; within O(log n) rounds every `/cluster` view
(served by `Membership::snapshot`, already wired into
[routes.rs](../src/routes.rs)) includes the newcomer. One seed address is
enough — the epidemic does the rest. That's V3's first criterion, and it's
observable today: `curl localhost:8073/cluster` on the compose cluster.

## 6. Wiring it to the ring: membership drives ownership

The last V3 criterion closes the loop with V2: **a membership change updates the
hash ring**. This is why the scaffold puts the member table and the `Ring`
inside one `View` struct behind one `RwLock` — the module doc comment in
[membership.rs](../src/membership.rs) explains they must change *together*:

```
gossip: "cache-c is Dead"
        │  (one lock, one atomic transition)
        ▼
members[cache-c].state = Dead     AND     ring.remove_node("cache-c")
        │
        ▼
next request for a key cache-c owned → ring routes it to the next node clockwise
```

If the two could diverge — member marked dead but still on the ring — the
coordinator (V4) would route requests to a corpse. Detection → ring update →
ownership shift, automatically, with no human and no coordinator anywhere in the
chain. (`Membership::bind` has a TODO marking where self joins the ring once
V2's `add_node` exists.)

Two transport facts round out the design (details in
[04-fundamentals-woven-through.md](04-fundamentals-woven-through.md)): gossip
rides **UDP** deliberately — the protocol is built to tolerate loss, so it
doesn't need TCP's retransmits or connection state, and a lost ping is exactly
the event the indirect-probe machinery absorbs. And a node shutting down
gracefully gossips **its own departure** so peers learn instantly instead of
paying the full probe + suspicion timeout to find out.

## 7. Mental model summary

| Question | Answer to hold onto |
|---|---|
| Why not all-to-all heartbeats? | O(n²) messages, and one dropped packet = one false death, exactly when the network is busiest |
| Why does random probing work? | P(someone probes a dead node per round) ≈ 1−1/e ≈ 63%, *independent of N* — constant cost, constant detection latency |
| What is indirect probing for? | It's false-positive insurance: "I can't reach X" is a fact about *our pair*, so ask k others before accusing |
| Why a Suspect state? | Grace period for a slow-but-alive node to object before the cluster acts on the accusation |
| Who bumps an incarnation number? | Only the node itself — that's what makes a refutation provably newer than the accusation it answers |
| Why piggyback gossip? | Dissemination for free on existing probe traffic; epidemic spread reaches n nodes in O(log n) rounds |
| What does membership feed? | The ring: every liveness transition rebuilds it, so key ownership always tracks the live set |

## 8. Where you'll build this

Everything lands in [membership.rs](../src/membership.rs) — the socket,
`Member`/`MemberUpdate`/`GossipMessage` wire types, and the read-side helpers
(`snapshot`, `is_alive`, `resolve`, `replicas`) are scaffolded; yours are:

- the **probe ticker** inside `run` (the `TODO(V3)` there sketches the two
  concurrent halves: receive loop + periodic prober, plus sending `Join` to the
  seeds on startup),
- the **message dispatch + update merge** in `handle_datagram` (currently
  `todo!()`): react to `Join`/`Ping`/`Ack`/`PingReq`, merge updates by
  incarnation, refute suspicions about yourself, rebuild the ring on any
  live-set change.

This doc unlocks the six **Done when ALL true** boxes of **V3** in
[SPEC.md](../SPEC.md). The decisions you own (→ `docs/07-design.md`): probe
interval, ack timeout, suspicion timeout, and fan-out k — and the reasoning
about what each buys and breaks. The 3-node compose cluster is your integration
arena: boot it, `docker compose kill cache-c`, and watch the survivors'
`/cluster` views converge to `dead`. For the build itself, `/hint` and `/quest`
pick up where this doc stops — and the original SWIM paper (Das, Gupta,
Motivala, 2002) is a genuinely readable classic if you want the source.
