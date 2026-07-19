# Concept Bank — Project 17: Global WebRTC Conferencing (cascaded SFU)

> This is the map of what this capstone should leave in your head. The region-local SFU is project 15; the concepts here are the *federation* layer — consensus placement, the relay mesh, demand routing, and recording. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 0 — Why cascade *(the framing idea)*

**The problem.** Fifty people in Tokyo, Frankfurt, São Paulo — one SFU somewhere fails twice. **Latency**: Tokyo→Tokyo media hairpinning through Frankfurt pays two ocean crossings for a packet that should never leave the building. **Uplink**: one origin SFU fans every stream to every viewer on Earth from a single NIC — the O(subscribers) egress the SFU was invented to distribute, re-bottlenecked in one rack.

**The idea.** One SFU per region; participants connect to the nearest; SFUs relay to *each other*. A stream crosses each ocean once per interested region; the remote SFU does local fan-out. The mental model shift: fan-out becomes a **tree** — internal edges carry one copy of the union of downstream demand; leaves do per-subscriber work.

- [ ] You can quantify both failures of the single-SFU design and show how the cascade's origin egress becomes O(regions × layers), not O(subscribers).
- [ ] You can state why Tokyo→Tokyo never leaving Tokyo is a *designed invariant* (hairpin count = 0), not a happy accident.

**In the wild:** Zoom's multi-datacenter routing, LiveKit Cloud's mesh, Jitsi's Octo ("cascaded bridges"), Google Meet's regional media servers.

---

## 🧠 Card 1 — Room placement is a consensus problem *(V1 · `src/placement.rs`)*

**The problem.** Two people create room `standup-42` at the same instant — one via Tokyo, one via Frankfurt. If each SFU independently declares itself home, you get two disjoint conferences under one name: a **split room**, conferencing's split-brain — participants in the "same" meeting who can never see each other, with no principled merge. A cache, a "first-writer-wins" race, or a gossip rumor can all silently produce it.

**The idea.** "At most one home region per room, cluster-wide, forever" is a **safety property**, and safety under concurrency + partitions is what consensus (project 09) is *for*: placement decisions go through a replicated log — a leader commits them, every node applies the same sequence, and a node that can't reach a quorum *refuses to place* (it may serve committed rooms read-only). Region membership ("Frankfurt has ≥1 participant in room R") rides the same log, so every node derives the *same* cascade topology. Control plane: strongly consistent. Media plane: lossy best-effort. Two consistency tiers, chosen on purpose.

**In the wild:** etcd/ZooKeeper as placement brains for storage systems; Spanner's directory placement; every multi-region system that must assign "one owner" (shard leases, primary election) faces exactly this.

**You own it when you can explain:**
- [ ] Why placement can't be a cache or a race: construct the concurrent-create timeline that yields a split room, and show where the log serializes it.
- [ ] Safety vs liveness in this context: what a minority partition is *allowed* to do (serve committed placements) vs forbidden (invent a home).
- [ ] Why idempotent placement matters (re-request returns the existing home, no epoch churn) and how a rejoining node catches up (learn the committed log, don't invent).
- [ ] The two-tier consistency argument: why the room map needs Raft while the media riding on it happily drops packets — and what would break if you swapped the tiers.
- [ ] Why hot-path reads (which region is home?) hit the locally-applied map, never a cross-region leader round-trip.

**Depth probes:**
- What exactly did you reuse from project 09 (election, replication, commit) vs remodel (the state machine: placement + membership entries)?
- Why does enforcing `max_rooms` through the log work where per-node counters don't?

**Trap:** "we'll place rooms in Redis and take the tiny race". The race isn't tiny — it's proportional to create-concurrency, and the failure (split room) is invisible until two colleagues insist they're both in the standup, alone.

---

## 🧠 Card 2 — The relay mesh: forward once, loop never *(V2 · `src/cascade.rs`)*

**The problem.** A Frankfurt subscriber wants a Tokyo publisher's stream. Naively the Tokyo SFU sends one copy per Frankfurt viewer across the ocean — the exact per-subscriber egress the cascade exists to kill. And once SFUs forward to each other, a new correctness hazard appears that single-SFU systems never face: **loops**. A relayed packet forwarded onward (or back) circulates a 3-region mesh forever, amplifying itself into a backbone-melting storm.

**The idea.** One relay **leg** per (origin region → interested region), carrying a *single* copy of each forwarded stream; the far SFU fans out locally through its own per-subscriber rewriters (project 15, composed on the far side). Loop-freedom comes from **provenance**: a packet is either origin media (relayable, once) or a relay copy (deliver locally, never re-relay) — the tree stays a tree because internal nodes never forward sideways. Legs are demand-driven (exist only while the region has ≥1 subscriber), torn down on last-leave, and capped.

**In the wild:** Jitsi Octo's bridge-to-bridge forwarding, LiveKit's relay, IP multicast trees (the same forward-once-per-edge idea, one layer down), CDN origin-shield hierarchies.

**You own it when you can explain:**
- [ ] The two invariants and their proofs-by-counter: one-copy-per-region-pair (relay egress counter reads 1 per region regardless of K subscribers) and doubling-remote-subscribers-adds-zero-backbone-packets.
- [ ] The loop mechanism: trace one packet around a 3-region cycle without provenance, then show where the relay/local distinction cuts the cycle.
- [ ] Why "local stays local" (no hairpin) falls out of the same rule — origin media relays outward only to *other* regions' legs.
- [ ] Leg lifecycle as demand-driven resource management: what opens one, what closes one, and why an unbounded leg set is an attack surface.
- [ ] Continuity across the relay: why a remote subscriber's stream is as gapless as a local one (whose rewriter absorbs the hop?).

**Depth probes:**
- Your mesh is fully connected (every region peers with every region). When would a deeper tree (relay through an intermediate region) win, and what does it cost in latency and complexity?
- What authenticates a relay packet as coming from a real peer SFU, and what could a forged one inject?

**Trap:** loop prevention by TTL/hop-count "like IP does". A hop count bounds the damage; provenance *eliminates* it — and in a media mesh, even two extra circulations of every packet is a storm.

---

## 🧠 Card 3 — Cross-region layer routing: the union of demand *(V3 · `src/routing.rs`)*

**The problem.** Frankfurt has a fibre viewer (wants high) and a mobile viewer (wants low). What does the Tokyo→Frankfurt leg carry? One layer starves somebody; all three wastes an ocean crossing on the mid layer nobody there watches. Project 15 answered "which layer per *subscriber*"; the cascade asks the same question one tier up — per *internal edge of the tree* — and the answer is different.

**The idea.** Each region **aggregates** its local subscribers' selections (project 15's per-subscriber choices) into a per-leg demand set and sends it upstream; the origin forwards down each leg exactly the **union of that region's demand** — the minimal set satisfying everyone downstream. New-higher-demand propagates up as **one** keyframe request (the p15 PLI rule, lifted a level); dropped demand shrinks the leg (backbone bandwidth reclaimed); **hysteresis** damps a flapping viewer so the ocean link doesn't toggle layers per oscillation.

**In the wild:** this is multicast group membership (IGMP's "someone downstream still wants this") reborn at the application layer; Jitsi/LiveKit cascades do exactly this per-layer demand propagation.

**You own it when you can explain:**
- [ ] Why internal tree edges carry the *union* — argue both failure directions (single-layer starvation, all-layers waste).
- [ ] The aggregation flow: local subscriber changes → region's demand set recomputes → leg updates — and why it must track joins, leaves, *and* layer changes.
- [ ] The keyframe economics across the tree: why exactly one upstream PLI per newly-demanded layer, and what a per-packet PLI storm would do to the publisher.
- [ ] Hysteresis at the leg level: why per-subscriber flapping must be absorbed *before* it reaches the backbone.
- [ ] How this composes invisibly with V2's continuity (adding/removing a layer on a leg never breaks a downstream subscriber's stream).

**Depth probes:**
- IGMP/multicast comparison: what does "prune when no downstream interest" map to in your leg/layer teardown?
- The measure→estimate→select loop now spans regions. Where does added estimation latency show up (slower up-switches for remote viewers), and does it matter?

**Trap:** routing each remote subscriber's layer individually across the backbone ("it's simpler"). You've silently rebuilt per-subscriber ocean egress — the exact O(subscribers) failure the cascade exists to prevent, hidden behind a working demo.

---

## 🧠 Card 4 — Recording: a durable subscriber, not a side-channel *(V4 · `src/recording.rs`)*

**The problem.** "Record the meeting" tempts you toward a special tap inside the SFU — a second media path with its own bugs, its own scaling, its own blind spots. And recording has a genuinely new sub-problem: each publisher's RTP timestamps sit on their *own* clock with arbitrary origin, so laying N tracks on one playback timeline is impossible from RTP timestamps alone.

**The idea.** The recorder is **just another subscriber**: it joins the room, receives forwarded RTP (over a cascade leg if the publisher is remote — it *counts as demand*), and writes instead of renders. That framing means recording inherits everything for free: the cascade, layer selection, no-transcode (persist encoded RTP; CPU is O(tracks), not O(pixels)). The recorder's own work: **wall-clock alignment** via RTCP Sender Reports (each SR pins an RTP timestamp to an NTP time — the bridge between per-track media clocks and one shared timeline), and **segmented durable output** with an index, so a crash loses at most the open segment, and stop/shutdown finalizes cleanly and idempotently.

**In the wild:** LiveKit Egress, Jitsi Jibri (which literally joins as a participant), Twitch VODs — production recorders are consumers of the normal media path, not taps inside it.

**You own it when you can explain:**
- [ ] The design argument for recorder-as-subscriber: every property it inherits (cascade routing, demand accounting, no-transcode) vs every property a side-channel would need rebuilt.
- [ ] The clock problem precisely: why two tracks' RTP timestamps are mutually meaningless, and how an SR's (RTP-ts ↔ NTP) pair per track anchors both to wall clock.
- [ ] What alignment tolerance you can honestly promise, and what bounds it (SR frequency, clock drift between SRs).
- [ ] The segment+index durability model: what a crash mid-segment loses, what finalize guarantees, why start/stop are idempotent.
- [ ] Why recorded CPU stays flat as resolution rises — and what that proves about the whole no-transcode chain.

**Depth probes:**
- The recorder wants the *high* layer but no human viewer in its region does. What does that do to the leg's demand set — and is that correct?
- Turning segmented RTP into a normal MP4/MKV afterward: which project's machinery does that post-processing resemble (11/12)?

**Trap:** aligning tracks by server arrival time. Network jitter bakes permanently into the recording as A/V and cross-participant skew — arrival time is the one clock that's *guaranteed* noisy; the SR mapping exists precisely to bypass it.

---

## ⚡ Rapid-fire round

- [ ] The two consistency tiers, one sentence each: placement = Raft-strong (a room has one home, always); media = best-effort (drop and move on) — and why mixing them up fails in both directions.
- [ ] Locality as a measured property: hairpin count 0, one backbone hop max, per-region forwarding p99 — designed and asserted, not hoped.
- [ ] Cascade auth: relay packets accepted only from known peer addresses; `/cluster/*` behind a shared secret/mTLS — the backbone is a trust boundary.
- [ ] Graceful shutdown in a mesh: relinquish leadership, drain legs, finalize recordings — no split room, no half-open leg, no truncated index.
- [ ] The reuse ledger: p09 → placement consensus, p14 → transport + SR clock mapping, p15 → rewriter/selector/BWE — the capstone's own value is the federation glue surviving partition.
- [ ] Boss-fight proof shapes: "double remote subscribers → 0 extra backbone packets", per-leg layer-set traces, partition timeline with a single home region throughout.

## 🔗 Connects to

- Project 09's consensus, applied: the placement map is your Raft with a different state machine.
- Project 15's per-subscriber loop composes into a per-region loop here — selection and routing as two levels of one tree.
- The one-copy-per-edge tree is the same shape as CDN origin shielding (project 16's edge) and IP multicast — fan-out trees are one idea wearing three uniforms.
