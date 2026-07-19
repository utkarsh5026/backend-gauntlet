<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 17 — Global WebRTC Conferencing *(cascaded SFU)*

> Project 15 gave you **one** SFU: a server that forwards a publisher's encoded RTP to many
> subscribers in the same room, no transcode, one upload → one tailored download per viewer. It
> makes a 50-person call in **one** data centre possible. This capstone asks the question that
> turns a regional SFU into a *global* conferencing system: what happens when the 50 people are
> in Tokyo, Frankfurt and São Paulo at once? The naive answer — point everyone at a single SFU —
> fails twice. First on **latency**: a Tokyo viewer watching a Tokyo publisher whose media
> **hairpins** through Frankfurt pays two ocean crossings for a packet that should never have
> left the building. Second on **uplink**: that one origin SFU has to fan a publisher's stream
> out to every viewer on Earth from a single machine's NIC — the exact `O(subscribers)` egress
> the SFU was supposed to spread out, now bottlenecked in one region.
>
> The answer the whole industry settled on is **cascade** (a.k.a. SFU federation / relay mesh):
> run **one SFU per region**, let each participant connect to their **nearest** one, and let the
> SFUs forward to *each other* over the backbone. Now a publisher's stream crosses each
> ocean **once** — one copy per region-pair that has interest — and the remote SFU does the local
> fan-out. Tokyo→Tokyo never leaves Tokyo; Tokyo→Frankfurt is a single relay hop, not one copy
> per Frankfurt viewer. That single idea — *forward once between regions, fan out locally* — is
> why a globally-distributed all-hands is possible.
>
> The reason a cascade is hard — and worth building — is everything hiding behind "let the SFUs
> forward to each other". **Who decides** which region anchors a room, so two people creating the
> same room from opposite sides of the planet don't spin up two disjoint conferences under one id?
> That's a **consensus** problem, and it's why this capstone leans on your Raft work from
> **project 09** (V1). **How** does an SFU become a relay peer of another SFU — a subscriber of a
> remote origin *and* a publisher to its own locals — without forwarding a packet in a loop
> forever (V2)? Given that Frankfurt has a fibre viewer who wants 1080p and a mobile viewer who
> wants 240p, which simulcast layers does the backbone leg to Frankfurt actually carry — all of
> them, one of them, or exactly the ones somebody there needs (V3)? And when the boss wants the
> meeting recorded, who is the recorder — a magic side-channel, or just **another subscriber** to
> the cascade that happens to write to disk instead of a screen (V4)? None of this is a library
> call; it's exactly the part you'd hand to a managed conferencing platform, and it's where "just
> run more SFUs" stops being simple.

## What it does (the easy part)
- One process is **one regional SFU**. It has a `REGION` (e.g. `us-east`) and a `NODE_ID`, and it
  knows its peer SFUs (`PEERS`) — the other regions in the mesh.
- Binds a participant-facing **muxed UDP** socket on `MEDIA_PORT` (default `7000`) for STUN/RTP/
  RTCP (as in project 15) and a separate backbone **cascade UDP** socket on `CASCADE_PORT`
  (default `7100`) that carries relayed media *between* SFUs.
- Exposes a small **signaling** HTTP API on `HTTP_PORT` (default `8080`), region-aware:
  - `POST /rooms/:room/publish` — a publisher announces its simulcast layers; the room is
    **placed** (a home region is chosen via consensus if it's new) and this region is registered
    as active. Returns ICE creds + the **local** media address to connect to.
  - `POST /rooms/:room/subscribe` — a subscriber names a publisher; if the publisher's media
    lives in another region, a **cascade relay leg** to that region is ensured. Returns ICE creds
    + the stable SSRC (reuses project 15's per-subscriber rewrite locally).
  - `GET /rooms` — the global topology: each room's home region + its active regions.
- Exposes an inter-SFU **cluster control** API (node-to-node, not for clients) on the same HTTP
  server: `POST /cluster/vote` and `POST /cluster/replicate` — the Raft-lite RPCs that keep the
  room-placement map consistent across the mesh (V1), the same node-to-node-over-HTTP shape you
  used in project 09.
- Shares that HTTP server with the **admin/observability** surface: `GET /healthz` / `GET
  /readyz`, `GET /status`, and `GET /metrics` (Prometheus).

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** — observable
> criteria you can check off — and a **Proof**: the test/bench/doc that *demonstrates* it (not
> "I think it works"). The criteria describe *what the system must do*, never *how*; figuring out
> the how is the entire point. A box only flips to ✅ when its Proof exists.

> **A note on scope.** This is a **capstone**: no new media primitive. The *within-a-region* SFU
> — ICE/STUN reachability, per-subscriber RTP rewriting, simulcast layer selection, bandwidth
> estimation — is **project 15**, and the reliable RTP transport under it is **project 14**. You
> **reuse** those; a vertical here that tempts you to re-solve the STUN codec or the per-subscriber
> rewriter means you've drifted — call into that project's idea instead. The verticals below are
> the **federation glue** those projects don't give you: the *consensus* that places a room
> globally, the *relay mesh* that forwards one copy per region-pair, the *cross-region routing*
> that decides which layers each backbone leg carries, and the *recorder* that joins the cascade
> as a durable subscriber. There is **no database and no docker-compose** here (as in project 15):
> the control plane is consensus you build (not an external etcd), the media plane is SFU↔SFU over
> UDP, and recordings are written to local disk. You run the mesh by launching **several
> instances** with different `REGION`/`NODE_ID`/`PEERS`, and simulate the backbone with `tc netem`.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Global room placement via consensus — *decide the home region, once, for everyone*
In `src/placement.rs`, build the **replicated room-placement map** — the control plane that every
SFU in the mesh agrees on. When a room is first created, *some* region has to be chosen as its
**home** (the anchor where a publisher's origin media lives and where the cascade tree roots), and
that decision has to be **the same on every node** — otherwise two people creating `standup-42`
from Tokyo and Frankfurt at the same instant get **two disjoint conferences** that can never see
each other (a *split room*, the conferencing equivalent of split-brain). This is a **consensus**
problem, and it's exactly what your Raft work in **project 09** is for: a small replicated log of
placement decisions, a leader that commits them, and followers that apply the committed entries to
their local map. The membership half rides the same log — "region `X` now has ≥1 live participant
in room `R`" is a replicated fact, so every node builds the **same** cascade topology (V2/V3) from
the same map.

You are not rebuilding Raft from scratch here — you're **leaning on** the ideas (and, if you kept
it, the code) from project 09: randomized election timeouts to elect a leader, append-entries to
replicate, a commit index, and idempotent apply. The scope that's *yours* is modelling **room
placement + region membership** as the replicated state machine and getting the safety property
right: at most one home region per room, cluster-wide, forever.

**Done when ALL true:**
- [ ] **A room has exactly one home region, cluster-wide:** two `publish`/`join` requests for the
  *same new room* arriving at *different* SFUs concurrently converge on **one** home region —
  never two — and every node reports the same one.
- [ ] **Placement is idempotent + durable across the log:** re-requesting placement for an
  already-placed room returns the existing placement (no second home, no epoch churn), and a
  node that (re)joins the mesh learns the committed placements rather than inventing its own.
- [ ] **Membership is replicated:** when a region registers interest in a room (a local
  participant joined), that fact is committed and **visible on every node**, so all SFUs derive
  the same set of active regions for that room; dropping the last local participant retires the
  region from the set.
- [ ] **Leadership is single + stable:** at most one leader per term; a leader loss triggers an
  election that converges to a new single leader, and placement requests made during the gap
  are served once a leader exists (not lost, not double-applied).
- [ ] **A minority partition cannot place a room:** an SFU that can't reach a quorum **refuses** to
  invent a new home region (it may still serve already-committed rooms read-only) — no split
  room is ever created, even under partition.
- [ ] **`max_rooms` is enforced through the log**, not per-node, so the cluster-wide cap holds even
  when placements are proposed at different nodes.

**Proof:** consensus tests — a concurrent-placement race asserting a single home region
(`concurrent_placement_has_one_home`), an idempotent-replacement test, a leader-loss election test,
and a partition test asserting a minority refuses to place (`minority_cannot_place`); `docs/17-design.md`
records what's reused from project 09 vs. new here, the replicated state-machine shape (placement +
membership entries), and the quorum/partition rule.

*Concept to internalize:* why global room placement is a **consensus** problem (not a cache), how a
replicated log gives you a single cluster-wide decision, and why "at most one home region per room"
is a *safety* property a cache or a race-y "first writer wins" can silently violate.

### V2. Inter-SFU cascade transport — *forward once between regions, fan out locally*
In `src/cascade.rs`, build the **relay mesh** — the transport that makes an SFU a peer of another
SFU. This is the heart of "cascaded". A publisher's origin media lives in its home region (V1). A
subscriber in a *different* region must receive it — but the origin SFU must **not** send one copy
per remote subscriber across the ocean. Instead it opens **one relay leg per remote region that has
interest** and sends a **single** copy of each forwarded stream down that leg; the remote SFU
receives it and does the **local** fan-out (reusing project 15's per-subscriber rewriter for its own
locals). So the inter-region cost is `O(regions with interest)`, not `O(subscribers)` — that's the
whole win.

The subtleties are all about being a relay *without a loop*. A relayed packet arriving from region
`A` must be fanned out to region `B`'s **local** subscribers, but it must **never** be relayed
onward to another region (or back to `A`) — otherwise a three-region mesh forwards a single packet
in circles forever. So a relayed packet carries provenance (which region it originated in / that
it's already a relay copy), and the SFU relays **only origin media**, fans out **relay media
locally**, and drops anything that would close a loop. And the legs are **bounded**: a fixed set of
peer regions, one leg each, torn down when the last remote subscriber in that region leaves.

**Done when ALL true:**
- [ ] **One copy per region-pair, not per subscriber:** with `K` subscribers in a remote region for
  the same origin stream, the origin SFU sends **one** relay copy of that stream to that region
  (the remote SFU fans out to all `K` locally) — proven by the relay-egress counter, not vibes.
- [ ] **Doubling remote subscribers adds zero backbone packets:** adding more subscribers in an
  already-active remote region adds **no** additional packets on the origin's leg to that region
  — the marginal remote viewer is free on the backbone.
- [ ] **Local stays local (no hairpin):** a subscriber in the publisher's **home** region receives
  the origin media directly and it is **never** relayed out and back — no same-region packet
  traverses another region.
- [ ] **Loop-free:** a packet that arrived as a relay copy from region `A` is fanned out to the
  local subscribers only and is **never** re-relayed onward (to `A` or any third region) — a
  3+-region mesh never forwards a packet in a cycle.
- [ ] **Legs are demand-driven + bounded:** a relay leg to a region **exists only while** that
  region has ≥1 subscriber for a stream there, is torn down when the last one leaves, and the
  set of legs is capped (`MAX_RELAY_LINKS`) — a chatty or hostile peer can't grow it unbounded.
- [ ] **Continuity across the relay:** a remote subscriber's received stream is as continuous
  (stable SSRC, gapless sequence, monotonic timestamp) as a local one — the extra relay hop is
  invisible downstream (this is project 15's rewriter composed on the far side of the leg).

**Proof:** a fan-out test asserting the origin relay-egress counter reads **1 per remote region**
regardless of that region's subscriber count (`one_copy_per_region`), a loop-prevention test on a
3-region mesh (`relay_never_loops`), and a leg-lifecycle test (leg opens on first remote sub, closes
on last); `docs/17-design.md` records the relay provenance/loop-prevention scheme and the leg
lifecycle + bounds.

*Concept to internalize:* the **relay/fan-out tree** as the answer to global fan-out — why one copy
per region-pair beats both the hairpin (all media through one region) and the mesh (every SFU to
every SFU per stream), and why loop prevention is a *correctness* property once SFUs forward to each
other.

### V3. Cross-region simulcast routing — *carry the union of demand, no more*
In `src/routing.rs`, build the **per-backbone-leg layer router** that decides *which simulcast
layers* each relay leg (V2) actually carries. Inside one region, layer selection is **per
subscriber** (project 15's `LayerSelector`). Across the cascade it's a different question, one tier
up: Frankfurt has a fibre viewer who wants the **high** layer and a mobile viewer who wants the
**low** layer — so the backbone leg from the origin to Frankfurt must carry **both** (the union),
because forwarding only one would starve one of them and forwarding **all three** would waste the
layer nobody there watches. So each remote SFU **aggregates** its local subscribers' demand into a
per-region layer request, sends that up the tree, and the origin forwards down each leg the **union
of layers demanded by that region** — the minimal set that satisfies everyone downstream, and no
more.

The keyframe subtlety from project 15 lifts to the cascade too: when a region **newly** demands a
higher layer (its best local subscriber up-switched), that demand propagates upstream and the
origin must request a **keyframe (PLI/FIR)** from the publisher on that layer — exactly once per
up-switch, not once per packet — before the layer can start flowing on the leg. And demand must
have **hysteresis** so a viewer flapping between layers doesn't thrash the backbone leg on and off.

**Done when ALL true:**
- [ ] **Union per leg, minimal:** a region with subscribers wanting low **and** high gets a leg
  carrying **exactly** {low, high} — not just one (nobody starved), not all three (the unused
  mid layer is not sent across the backbone).
- [ ] **Demand aggregates upward:** each region's per-leg demand is the aggregate of its **local**
  subscribers' selected layers (project 15's per-subscriber choice), recomputed as locals join,
  leave, or change layer — the leg tracks the region's true need.
- [ ] **New higher demand triggers exactly one upstream keyframe request:** when a region first
  demands a layer higher than the leg currently carries, the origin requests a keyframe on that
  layer **once**, keeps sending the current set until the keyframe arrives, then adds the layer
  — the keyframe-owed flag clears (not one PLI per packet).
- [ ] **Dropped demand shrinks the leg:** when the last subscriber in a region needing a layer goes
  away, that layer stops being sent on that leg (the backbone reclaims the bandwidth).
- [ ] **Hysteresis, no thrash:** a subscriber oscillating around a layer boundary does not toggle
  the backbone leg's layer set on every packet — changes are damped by a documented margin/hold.
- [ ] **Composes with V2 continuity:** adding/removing a layer on a leg is invisible to every
  downstream subscriber's stream continuity (V2 rewriter on the far side absorbs it).

**Proof:** unit tests `leg_carries_union_of_demand`, `aggregates_local_demand`,
`new_demand_requests_one_keyframe`, `dropped_demand_shrinks_leg`, and a hysteresis test; `docs/17-design.md`
records the demand-aggregation policy (max? union set? hysteresis margin) and the upstream
keyframe-request mechanism reused from project 15.

*Concept to internalize:* **demand aggregation up a relay tree** — why the right thing to forward on
an internal edge of a fan-out tree is the *union of downstream demand*, not per-leaf selection and
not everything; and how per-subscriber selection (p15) and per-region routing (here) compose into
one measure→estimate→select→route loop.

### V4. Server-side recording — *the recorder is just another subscriber*
In `src/recording.rs`, build the **conference recorder**. A recorded meeting is not a magic
side-channel bolted onto the SFU — the clean design is that the recorder is **another subscriber**:
it joins the room like any viewer, receives each publisher's forwarded RTP, and writes it to disk
instead of a screen. That framing is the learning — it means recording rides the exact same cascade
you built (the recorder subscribes in *some* region and pulls origin media over a relay leg if
needed), and it inherits the SFU's "no transcode" property (you persist the encoded RTP, you don't
re-encode a pixel).

The parts that are genuinely the recorder's own: **cross-track wall-clock alignment** — each
publisher's RTP timestamp is on its **own** clock with an arbitrary offset, so to lay N tracks on
one timeline you map each track's RTP timestamp → wall clock using its **RTCP sender reports** (the
SR carries the RTP-ts ↔ NTP-time correspondence), the same clock-mapping idea from project 14.
And **durable, segmented output** — the recording is written in segments (so a crash loses at most
the open segment, not the meeting) with an index, and finalizing on stop (or graceful shutdown)
flushes cleanly and resumes correctly on restart.

**Done when ALL true:**
- [ ] **Recorder is a first-class subscriber:** starting a recording for a room subscribes it to
  **every** publisher in the room (pulling remote publishers over a cascade leg if they're in
  another region), and it counts as demand for those streams (the leg exists because the
  recorder wants it) — no special media path.
- [ ] **No transcode:** recorded media is the **encoded RTP** written through (payload byte-identical),
  not a re-encode — CPU to record is `O(tracks)`, independent of resolution.
- [ ] **Tracks share one timeline:** the per-track outputs carry a **common wall-clock reference**
  derived from each track's RTCP sender reports, so two tracks recorded from different origin
  clocks can be aligned on playback within a documented tolerance.
- [ ] **Durable + segmented:** output is written in segments with an index; a recorder crash loses
  **at most the currently-open segment**, and the finalized segments before it are playable.
- [ ] **Clean finalize:** `stop` (and graceful shutdown) flushes and finalizes open segments — a
  cleanly-stopped recording has a complete, closed index; both `start` and `stop` are idempotent
  (double-start doesn't fork a recording, double-stop is harmless).

**Proof:** a test asserting a started recording registers as a subscriber (and demand) on every
publisher (`recorder_subscribes_all`), a wall-clock-alignment test on two synthetic tracks with
skewed RTP clocks (`tracks_align_on_wallclock`), a segment-durability test (kill mid-segment →
prior segments intact + indexed), and a finalize/idempotency test; `docs/17-design.md` records the
recording model (subscriber, not side-channel), the SR-based alignment, and the segment/index format.

*Concept to internalize:* recording as a **durable subscriber** to the cascade (reuse, not a new
path), why persisting encoded RTP keeps recording transcode-free, and how RTCP sender reports let
you place independently-clocked tracks on a single wall-clock timeline.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols / API
- [ ] **Interoperates with a real WebRTC stack per region:** a browser (`RTCPeerConnection` +
  simulcast `sendEncodings`) or `gstreamer webrtcbin` completes ICE against its **regional** SFU
  and its media reaches a subscriber in **another** region over the cascade — not just against a
  hand-rolled client. The signaling shape and its region-routing (which SFU a client is told to
  use) are documented in `docs/17-design.md`. *(Reuses project 15's ICE/RTCP; the cascade is new.)*
- [ ] **Inter-SFU control is a real RPC:** the `/cluster/*` node-to-node calls (Raft-lite vote /
  replicate) work across separate processes/hosts, the same node-to-node-over-HTTP transport as
  project 09 — a placement committed on one node is visible on the others.
- [ ] **Graceful shutdown:** on SIGTERM the SFU stops admitting new participants, drains in-flight
  signaling/admin HTTP, **relinquishes leadership** if it holds it (so the mesh re-elects
  quickly), tears down relay legs cleanly, and **finalizes open recordings** — no split room, no
  half-open relay leg, no truncated recording index.

### Caching / delivery
- [ ] **One copy per region-pair (V2)** is the delivery invariant — the backbone carries the union
  of demand (V3), fan-out is done at the edge (regional) SFU, and the origin's egress is
  `O(regions)`, not `O(global subscribers)`.
- [ ] **Placement map is the read cache:** hot routing reads (which region is home, which regions
  are active) come from the locally-applied replicated map, not a cross-region round-trip per
  request — a subscribe in-region does not block on the leader. *(Proof: note in `docs/17-design.md`.)*

### Security / abuse protection
- [ ] **Every wire parser is bounds-checked** so a malicious sender on an open UDP port (participant
  *or* backbone) can't OOM/panic: STUN/RTP/RTCP lengths (reused from p15) **and** the relay
  framing added in V2 are range-checked before indexing/allocating; oversized/truncated datagrams
  are dropped.
- [ ] **Cascade legs are authenticated between SFUs:** a relay packet is only accepted from a
  **known peer region's** address (from `PEERS`), and the `/cluster/*` control RPCs are
  authenticated (a shared cluster secret / mTLS), so a stranger can't inject media onto the
  backbone or forge a placement entry. The scheme (and the SRTP/DTLS scope, reused-or-not from
  p15) is stated in `docs/17-design.md`.
- [ ] **Bounded everything:** rooms, peers-per-room, relay legs (`MAX_RELAY_LINKS`), the replicated
  log, and per-recording buffers are all capped; a join flood, a partition flap, or a chatty
  peer degrades itself, never the process.

### Observability
- [ ] A `tracing` span/context per participant (region + room + peer) and per relay leg, with
  structured logs for lifecycle events (room placed, leader elected, relay leg opened/closed,
  layer added to a leg, recording started/finalized) — never log media payload bytes.
- [ ] Counters at `/metrics`: **relay copies out (per region-pair)** — the fan-out amplification
  that proves one-copy-per-region — **relay copies in, backbone bytes, placement commits,
  elections, keyframe requests upstream, recorded bytes/segments.**
- [ ] Gauges: **rooms placed, active regions per room, relay legs (by peer region), layers carried
  per leg, leader/term, recordings active** — enough to watch a room's cascade tree form and a
  leader election settle in real time.

---

## Cross-cutting scale skills
- **Fan-out is a tree, not a star:** you reason about the cascade as a fan-out tree rooted at the
  origin region — internal edges carry one copy of the union of downstream demand, leaves do the
  per-subscriber work — and prove origin egress is `O(regions)`, not `O(subscribers)`.
- **Consensus for control, best-effort for media:** the placement/membership map is **strongly
  consistent** (a room has one home, always), while the media plane is **lossy/best-effort** (drop
  a packet, keep moving) — you deliberately put the two on different consistency tiers.
- **Locality is a first-class goal:** "nearest SFU wins" and "a packet crosses each ocean once" are
  properties you design for and measure (added latency per hop, hairpin count = 0), not accidents.
- **Reuse over rebuild:** the region-local SFU (p15) and RTP transport (p14) are dependencies you
  compose; the capstone's value is the *integration* surviving global scale + partition, not
  re-deriving the rewriter.
- **Bounded + loop-free on an open mesh:** relay legs, log size, and recording buffers are capped,
  and relay provenance makes the mesh loop-free — a hostile peer or a partition flap degrades
  itself, never melts the mesh.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the global fan-out / partition load test lives in
   `bench/`, the numbers in `docs/17-benchmarks.md`.
3. `docs/17-design.md` records the decisions the SPEC grades: the **placement consensus model +
   what's reused from p09** (V1), the **relay provenance + loop-prevention + leg lifecycle** (V2),
   the **cross-region demand-aggregation + keyframe policy** (V3), the **recording model + SR-based
   alignment + segment format** (V4), and the **cascade-auth / SRTP scope** call.
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p global-conferencing` are green; no
   `todo!()` remains on a checked path.

## 🐉 Boss fight — The Hairpin

> A company-wide all-hands. One presenter in **Frankfurt**; **thousands** of employees joining in a
> 30-second burst from **Tokyo, São Paulo, and Frankfurt** at once. The naive design routes every
> viewer's media back through the presenter's SFU — so a Tokyo viewer's stream **hairpins** across
> two oceans, arriving a full second late, while the Frankfurt origin's uplink tries to fan one
> stream out to the entire planet from a single NIC and melts. Your cascade has to keep each
> viewer on their **nearest** SFU, cross each ocean **exactly once** per demanded layer, and hold
> a single consistent room the whole time — *including* when a transcontinental backbone link sags
> and a region briefly can't reach the leader. Beat the hairpin: forward once, fan out locally, and
> never split the room.

**Arena:** `bench/` runs **≥ 3 regional SFU instances** (release builds, `cargo run --release`,
different `REGION`/`NODE_ID`/`PEERS`) with the inter-region backbone shaped by `tc netem` (add
realistic one-way delay + a "sagging" profile that drops one region's backbone to a fraction of its
capacity for 60 s and recovers). One publisher in the home region publishes **3 simulcast layers**;
a load harness spins up **≥ 50 subscribers per region** on a spread of downlink profiles. The run
lasts **≥ 5 minutes**; efficiency + latency + consistency are measured from the SFUs' metrics and
the subscribers' received streams, not vibes.

**The boss falls when ALL true:**
- [ ] **Forward once, not per viewer:** for each remote region the origin sends **≤ 1 relay copy
  per demanded layer** regardless of that region's subscriber count — **doubling** a remote
  region's subscribers adds **0** origin backbone packets — and origin egress packet-rate stays
  `O(regions × layers)`, not `O(total subscribers)`.
- [ ] **No hairpin, nearest SFU:** every participant connects to their own region's SFU; **0**
  packets destined for a same-region pair traverse another region; a cross-region path is
  **exactly one** backbone hop, and per-region forwarding p99 stays **≤ 10 ms** (ingress →
  egress `send_to`, as in project 15).
- [ ] **Right layers per leg:** each region's backbone leg carries **exactly** the union of layers
  its local subscribers demand (capped/mobile regions get low only; a mixed region gets its
  union) — no leg carries a layer nobody there watches, and no subscriber is starved.
- [ ] **Survives a wobble without splitting:** during the 60 s backbone sag / region partition, the
  other regions keep forwarding (no global stall), the mesh keeps (or re-elects to) a **single**
  leader, the room keeps its **one** home region, and on recovery membership reconverges — **no
  split room** ever forms and no already-placed room flips home.
- [ ] **Bounded memory:** RSS on every SFU stays **flat** across the full run and the join storm (all
  subscribers arriving within a few seconds) — a 5-minute meeting and a 5-hour meeting use the
  same RAM; relay legs and the log don't grow without bound.

**Proof:** methodology + the per-region-pair relay-copy counts (with the "double the subscribers →
0 extra backbone packets" experiment), the per-region forwarding-latency + hairpin-count (=0) trace,
the per-leg layer-set trace, the partition timeline (leader/term + room home region stayed single),
and the flat RSS plots in `docs/17-benchmarks.md` (region layout + `tc netem` profiles + the
subscriber-harness commands reproducible via `bench/`).

## Suggested order of attack
1. **Get one region working end-to-end first** — this is just project 15: one SFU, signaling
   creates rooms/peers, a client publishes and a local subscriber receives. No cascade yet. (Reuse
   your p15 code; the federation is what's new.)
2. **V1** — stand up the placement consensus across **≥ 3** instances: leader election (randomized
   timeout from p09), replicate a room-placement entry, apply it, and prove a concurrent-placement
   race yields **one** home region. This is the spine — the cascade topology is derived from this map.
3. **V2** — make an SFU a relay peer: open a leg to a remote region, forward **one** copy of an
   origin stream down it, fan out locally on the far side, and make it **loop-free** on a 3-region
   mesh. Prove one-copy-per-region with the counter.
4. **V3** — aggregate each region's demand and carry the **union** on its leg; propagate a new
   higher demand as **one** upstream keyframe request; add hysteresis so it doesn't thrash.
5. **V4** — add the recorder as a subscriber: subscribe it to every publisher (over legs as needed),
   write segmented encoded RTP, align tracks via RTCP SR, finalize on stop/shutdown.
6. Add the wire-parser bounds + cascade/cluster auth + metrics + graceful shutdown (relinquish
   leadership, finalize recordings); then run the multi-region mesh under `tc netem` and defeat the
   Hairpin.

## Run it
```bash
cp .env.example .env          # set REGION / NODE_ID / PEERS / MEDIA_PORT / CASCADE_PORT / HTTP_PORT

# One region (this is essentially project 15 — signaling + admin work immediately):
cargo run -p global-conferencing
#   curl localhost:8080/healthz
#   curl localhost:8080/rooms                 # global topology (empty until a room is placed)
#   curl -XPOST localhost:8080/rooms/all-hands/publish \
#        -H 'content-type: application/json' \
#        -d '{"layers":[{"rid":"q","ssrc":111,"bitrate_bps":150000},
#                        {"rid":"h","ssrc":222,"bitrate_bps":500000},
#                        {"rid":"f","ssrc":333,"bitrate_bps":2000000}]}'
#   The first publish tries to PLACE the room — that hits the V1 Placement::place_room todo!().
#   That panic is your worklist.

# A 3-region mesh on one host (three terminals, distinct ports + peer lists), e.g.:
#   REGION=eu-west  NODE_ID=n1 HTTP_PORT=8080 MEDIA_PORT=7000 CASCADE_PORT=7100 \
#     PEERS='us-east=http://127.0.0.1:8081|127.0.0.1:7101,ap-south=http://127.0.0.1:8082|127.0.0.1:7102' \
#     RUN_BACKGROUND=true cargo run -p global-conferencing
#   (…and the symmetric commands for us-east:8081/7001/7101 and ap-south:8082/7002/7102)

# Shape the backbone for the boss fight (Linux):
sudo tc qdisc add dev lo root netem delay 120ms 20ms      # ~inter-region RTT
```
