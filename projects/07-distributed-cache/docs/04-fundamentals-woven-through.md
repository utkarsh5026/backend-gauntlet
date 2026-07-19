# The Backend Fundamentals Woven Through This Project

> What this teaches: the five horizontal-checklist ideas from
> [SPEC.md](../SPEC.md) that aren't owned by a single vertical — why gossip rides
> UDP while data rides TCP, why a clean shutdown *announces itself*, why TTLs get
> jitter, why the internal RPC path is a cache-poisoning vector until it's
> authenticated, and what observability proves the ring actually works. No prior
> knowledge assumed. These map to the SPEC's **Protocols / Caching / Security /
> Observability** checklists and the ⚡ rapid-fire round in
> [CONCEPTS.md](../CONCEPTS.md).

---

## The one sentence to hold onto

**The verticals make the cluster work; these fundamentals make it operable —
transport chosen per traffic shape, exits that inform the survivors, expiries
that don't synchronize, internal trust that's earned not assumed, and metrics
that let you *see* the ring do its job.**

---

## 1. Why gossip rides UDP while the data path rides TCP

This project deliberately runs two transports side by side (see
[node.rs](../src/node.rs): every `Node` carries an `http_addr` *and* a
`gossip_addr`). That's not an accident to smooth over — it's a lesson in
matching transport to traffic.

What TCP actually buys you, and what it charges:

| TCP gives | The price |
|---|---|
| delivery guaranteed (retransmits) | a lost packet stalls everything behind it until retransmitted — **head-of-line blocking** |
| ordering | per-connection state (handshakes, buffers, timers) on both ends |
| streams of any size | connection setup latency before byte one |

The **data path** (`PUT`/`GET /cache/{key}` and the forwarded
`/internal/cache/{key}` calls) wants exactly those guarantees: a value must
arrive complete, correct, and correlated with its response. HTTP over TCP. Easy.

The **gossip path** wants the opposite. A SWIM ping is a tiny, self-contained
datagram whose *loss is already handled by the protocol itself* — a missed ack
triggers indirect probes; a missed update arrives via another peer next round
(that's what epidemic dissemination *means*). Paying TCP's costs here buys
nothing and actively hurts:

- **Retransmitting a stale ping is worse than dropping it.** By the time TCP
  retries, the probe round is over; SWIM has moved on.
- **Head-of-line blocking would couple unrelated probes** — one slow peer delays
  news about every other peer sharing the connection.
- **Per-peer connections reintroduce O(n) state** on a path whose whole design
  goal (V3) is constant per-node cost.
- And subtly: TCP's own retries would *mask the packet loss that SWIM is trying
  to measure* — a failure detector wants to see the failures.

Hence `UdpSocket` in [membership.rs](../src/membership.rs): fire-and-forget
datagrams, no connection state, loss-tolerant by design. One real constraint
comes with it: a UDP datagram must fit in a packet — beyond the path MTU
(1500-byte Ethernet − 20 IPv4 − 8 UDP = **1472 bytes** of safe payload) it gets
fragmented, and a fragmented datagram is lost if *any* fragment is. That's why
the SPEC's input-validation item mentions the "UDP MTU for gossip": piggybacked
update batches must be bounded, or a chatty round silently stops converging.
Say all this in `docs/07-design.md` — the Protocols checklist explicitly asks
for the why.

## 2. Graceful shutdown: gossip your own departure

V3 detects deaths in roughly `probe interval + suspicion timeout` — seconds, by
design, because accusing quickly means false-positive evictions (doc 02). But a
*planned* exit (deploy, scale-down, `docker compose stop`) shouldn't pay the
detection tax at all: during those seconds, peers still route reads and
replicated writes to a node that's gone, eating timeouts and failovers on every
affected key.

The fix costs one datagram. On SIGTERM, before exiting:

1. finish in-flight HTTP requests (standard drain),
2. **gossip your own departure** — announce yourself as leaving/dead so peers
   update their member table and rebuild the ring *now*, not a suspicion-timeout
   from now.

Note the elegance: departure is just another membership update flowing through
machinery you already built — the same `MemberUpdate` dissemination, the same
membership → ring wiring. A deliberate exit is the one death that doesn't need
detecting, because the dying node is still alive enough to say goodbye. (This
also echoes doc 02's incarnation rules: peers must accept the departure as
authoritative and not "refute" it back to life with stale gossip.) SPEC items:
the Protocols "Graceful shutdown" box and the cross-cutting drain requirement.

## 3. TTL jitter: don't let expiries synchronize

Project 01 taught this at the Redis layer; here it's node-local. The failure
shape: a deploy warms the cache — 50,000 keys written in one burst, all with
`?ttl=300`. Five minutes later they expire **on the same tick**. Every request
that touched them misses *simultaneously*; the database that had been idling
behind a 95% hit ratio absorbs the whole read load in one spike — a thundering
herd you scheduled for yourself, and it recurs every TTL period as the refill
re-synchronizes.

The fix is one line of arithmetic wherever TTLs are applied: perturb each TTL by
a small random factor (e.g. ±10%), so `ttl=300` becomes a spread across
270–330 s and expiries smear over a minute instead of detonating on one tick.
The interesting part is having a reason for the *amount*: more jitter = smoother
DB load, but more deviation from the TTL the client asked for. The SPEC's
Caching checklist grades the presence of jitter; your `docs/07-design.md` states
the spread. (Where to apply it — coordinator vs store — is yours; just make sure
replicas of one key don't disagree wildly about its lifetime.)

## 4. Trust boundaries: the internal path is a poisoning vector

[routes.rs](../src/routes.rs) exposes two trees, and the split (already
scaffolded — the Protocols checklist requires it) is a *trust* statement:

| Path | Meant for | Currently trusts |
|---|---|---|
| `/cache/{key}` | clients | anyone (TODO: token on writes) |
| `/internal/cache/{key}` | peer coordinators only | **anyone — that's the hole** |

Walk the attack: `/internal/cache/{key}` PUT writes straight into the local
store — no routing, no policy, because it exists to receive *forwarded* writes
from trusted peers. If it's reachable without proof of peerhood, an outsider
can inject a value for any key onto any node directly: **cache poisoning**. If
the thing behind this cache is HTML fragments, sessions, or authz decisions,
that's not stale data — it's an attacker choosing what your app serves. The
same logic covers admin/cluster control surfaces.

The SPEC's Security checklist therefore requires a shared token on writes *and*
on the whole internal tree (the `TODO(security)` comments in routes.rs mark the
spots), plus two disciplines that come with any secret:

- **Never log it** — auth headers must not appear in traces or error messages
  (the repo-wide rule in CLAUDE.md).
- **Compare it in constant time** — a naive `==` on strings returns early at the
  first mismatched byte, so response timing leaks how many leading bytes an
  attacker got right, turning a 2¹²⁸ search into a byte-by-byte one. Whether
  that's exploitable over a noisy network is debatable — which is exactly why
  the SPEC asks for the timing-safety call to be a **documented decision**
  rather than an unexamined `==`.

Input validation is the same budget-protection instinct: `MAX_KEY_LEN` and the
`max_value_bytes` cap already in [routes.rs](../src/routes.rs) exist so one
pathological request can't blow the node's memory (a giant value counts as one
entry to V1's capacity but eats unbounded bytes) or the gossip MTU (§1).

## 5. Observability: *seeing* the ring work

A distributed cache has emergent behavior no log line shows. The SPEC's
Observability checklist picks three windows into it, each answering a question
you'll genuinely ask while debugging:

| Signal | Question it answers |
|---|---|
| Per-request `tracing` span with request id + **served locally vs forwarded, and to whom** | "why was this GET slow?" — one hop or two? which peer? (via `common-telemetry`, per CLAUDE.md; the `TraceLayer` is already on the router) |
| `/metrics`: hit/miss ratio, entries & bytes vs capacity, evictions | "is the cache *working*?" — a capped store with a bad policy shows up here as evictions churning and hit ratio sagging (V1's `len()` feeds this) |
| membership size + gossip/suspicion events | "is the cluster stable?" — suspicion-event spikes are the early warning that your timeouts are mistuned (doc 02's trap) before flapping starts |
| **per-node key counts** | "did the ring actually rebalance?" — see below |

The last one is the payoff metric for the entire project. It's how V2 stops
being a claim and becomes an observation: record per-node counts, join a node,
and watch ≈1/N of the keys' ownership shift while the rest hold still:

```
before join:   cache-a: 20,072   cache-b: 19,506   cache-c: 20,422
join cache-d ─▶ each survivor sheds ≈¼ of its keys; nothing else moves
after:         cache-a: ~15,000  cache-b: ~14,700  cache-c: ~15,300  cache-d: ~15,000
```

(Those "before" numbers are the real measured 128-vnode distribution from
[doc 01](01-consistent-hashing-and-virtual-nodes.md).) This same number is what
`docs/07-benchmarks.md` needs for the Definition of done's remap measurement —
observability and the bench requirement are the same work here.

## 6. Mental model summary

| Question | Answer to hold onto |
|---|---|
| Why UDP for gossip? | SWIM already tolerates loss; TCP's retransmits, ordering, and per-peer state cost O(n) and mask the very failures the detector measures |
| Why TCP for data? | Values must arrive complete and correlated with a response — exactly the guarantees TCP sells |
| Graceful shutdown in one line | The one death that needs no detection is the one you announce yourself — one departure datagram saves peers a full suspicion timeout |
| TTL jitter in one line | Same-tick expiry of a warmed batch is a scheduled thundering herd; ±10% smears it out |
| Why authenticate `/internal`? | It writes to the store with no routing or policy — unauthenticated, it's a direct cache-poisoning API for outsiders |
| Timing-safe comparison in one line | Early-exit `==` leaks match length through response time; decide and document, don't default |
| The payoff metric | Per-node key counts across a join — the ring's ≈1/N promise, watched live |

## 7. Where you'll build this

Unlike the verticals, these land *across* the codebase rather than in one
module: the departure gossip in [membership.rs](../src/membership.rs)'s shutdown
path, jitter where TTLs are computed ([store.rs](../src/store.rs) /
[coordinator.rs](../src/coordinator.rs) — your call), auth middleware and the
`TODO(security)` sites in [routes.rs](../src/routes.rs), and metrics threaded
through store, membership, and coordinator (per CLAUDE.md, Prometheus/OTel get
added per-project on top of `common-telemetry`).

This doc unlocks the **Protocols, Caching, Security, and Observability**
checklist boxes in [SPEC.md](../SPEC.md) (those not already owned by V1–V4) and
the cross-cutting shutdown/backpressure items. Build them *after* the verticals
they instrument exist — the suggested order of attack in the SPEC puts them at
step 6 — and record the whys (UDP rationale, jitter spread, timing-safety call)
in `docs/07-design.md`. As ever: `/hint` for nudges, `/quest` to build one
vertical properly.
