<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 15 — WebRTC SFU (Selective Forwarding Unit)

> Project 14 gave you the hard-real-time toolkit for **one** stream over lossy UDP — RTP,
> jitter buffers, NACK, congestion control. This project asks the question that turns a
> transport into a *conferencing system*: how does one publisher reach **many** viewers, each
> on a different link, without the whole thing collapsing? The two obvious answers both fail.
> A **mesh** (everyone sends everyone a copy) makes each publisher upload N−1 streams — the
> uplink dies at ~4 people. An **MCU** (a server that decodes everyone, composites one picture,
> and re-encodes it per viewer) burns a CPU core per call and adds a decode→encode latency hop
> to every frame. The answer the whole industry settled on is the **SFU**: a server that sits
> in the middle and **forwards the publisher's already-encoded RTP packets, unchanged payload,
> to each subscriber** — one upload from the publisher, one tailored download per subscriber,
> *no transcoding*. That single idea is why a 50-person call is possible.
>
> The reason an SFU is hard — and worth building — is everything hiding behind "forward". To
> forward, the SFU first has to be **reachable** at all: browsers are behind NAT, so before a
> byte flows the two sides run **ICE**, firing **STUN** checks at each other until a working
> path is found and nominated (V1). Then "forward" isn't a memcpy: each subscriber must see one
> **continuous** RTP stream (stable SSRC, gapless sequence numbers, monotonic timestamp) even
> as the SFU drops packets under them and switches which origin feeds them — so the SFU rewrites
> headers per subscriber and can **translate their NACKs back** to the origin packet (V2).
> Because subscribers have wildly different links, the publisher sends the same video at several
> qualities at once (**simulcast**), and the SFU picks, *per subscriber*, the highest layer that
> fits — switching only at keyframes so the decoder never chokes (V3). And to pick, it has to
> **estimate each subscriber's downlink bandwidth** from feedback and drive the layer choice
> with it (V4). None of this is a library call here — it's exactly the part you'd hand to
> `webrtc-rs`/`libwebrtc`, and it's where "just relay the packets" stops being simple.

## What it does (the easy part)
- Binds a **muxed UDP** socket on `MEDIA_PORT` (default `7000`) that carries STUN, RTP and RTCP
  together (WebRTC bundles them; a one-byte RFC 7983 demux — wired for you — sorts them out).
- Exposes a small **signaling** HTTP API on `HTTP_PORT` (default `8080`) that builds the room
  graph and hands back ICE credentials (a JSON stand-in for SDP offer/answer — full SDP is a
  stretch, not the learning):
  - `POST /rooms/:room/publish` — a publisher announces its **simulcast layers** (rid/ssrc/
    bitrate) and gets ICE creds + the media address to connect to.
  - `POST /rooms/:room/subscribe` — a subscriber names a publisher and gets ICE creds + the
    **stable SSRC** it will receive on.
  - `GET /rooms` — the live topology.
- Shares that HTTP server with the **admin/observability** surface: `GET /healthz` / `GET
  /readyz`, `GET /status`, and `GET /metrics` (Prometheus).

> There is **no database and no docker-compose** here (as in project 14): the SFU *is* the media
> server, and everything lives in-process. The parts you'd normally hand to `webrtc-rs` — the
> ICE/STUN agent, the per-subscriber RTP rewriter, the simulcast layer selector, the bandwidth
> estimator — are exactly the parts you build. To exercise it end-to-end you point a real
> browser (`getUserMedia` + `RTCPeerConnection` with `sendEncodings` for simulcast) or
> `gstreamer`'s `webrtcbin` at the signaling API, and — for the boss fight — spin up many
> subscribers and degrade their links with `tc netem`.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** — observable
> criteria you can check off — and a **Proof**: the test/bench/doc that *demonstrates* it (not
> "I think it works"). The criteria describe *what the system must do*, never *how*; figuring
> out the how is the entire point. A box only flips to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. ICE / STUN connectivity — *let a browser behind NAT actually reach you*
In `src/ice.rs`, build the **STUN codec + ICE-lite agent** that makes the SFU reachable. Before
one media byte flows, the browser fires **STUN Binding requests** at the SFU's advertised
address; the SFU must answer each with a correct **Binding success response**, and when the
browser nominates a pair, remember which source address won — that address *is* the peer's media
path. This SFU is **ICE-lite** (it answers checks, it doesn't gather or send its own), but
answering *correctly* is the whole vertical: get the transaction id, the XOR-MAPPED-ADDRESS
encoding, the HMAC-SHA1 integrity, or the CRC32 fingerprint wrong and the browser silently
discards your response and the call never connects.

A **STUN message** is a 20-byte header (a 14-bit type split into class+method, a length, the
`0x2112A442` magic cookie, and a 96-bit transaction id) followed by 4-byte-aligned **attribute
TLVs**. A connectivity check carries a `USERNAME` (`<remote-ufrag>:<local-ufrag>`), a `PRIORITY`,
a controlling/controlled tie-breaker, a **MESSAGE-INTEGRITY** (HMAC-SHA1 over the message keyed
by the ICE `pwd`), and a **FINGERPRINT** (`CRC32(msg) ^ 0x5354554e`); the controlling side adds
**USE-CANDIDATE** to nominate. The response echoes the transaction id and returns an
**XOR-MAPPED-ADDRESS** — the source address the SFU saw, XORed with the cookie + txid so NATs
that rewrite payloads can't corrupt it.

**Done when ALL true:**
- [ ] The **STUN codec round-trips**: encode∘parse (and parse∘encode) is identity on class,
  method, transaction id and attributes for a Binding request *and* a Binding success response,
  including `XOR-MAPPED-ADDRESS` for both **IPv4 and IPv6** peers.
- [ ] Parsing is **bounds-checked and total on garbage**: a datagram shorter than 20 bytes, a
  wrong magic cookie, or an attribute length that overruns the buffer is a clean `Err`, never a
  panic or an out-of-bounds read (an open UDP port receives arbitrary bytes from anyone).
- [ ] **MESSAGE-INTEGRITY verifies**: a message signed with a `pwd` validates with that `pwd`
  and **fails with the wrong key**; **FINGERPRINT** matches a known value — an unauthenticated
  check is dropped and **never nominates a path**.
- [ ] A valid Binding request produces a **valid success response** to the same source address,
  echoing the txid with `XOR-MAPPED-ADDRESS = source`, signed + fingerprinted.
- [ ] A request carrying **USE-CANDIDATE** (with valid integrity) **nominates** that source
  address as the peer's media path — subsequent RTP from that address routes to this peer.

**Proof:** unit/property tests for the codec and integrity (`stun_binding_roundtrips`,
`short_stun_errors`, `bad_cookie_errors`, `message_integrity_verifies`) and nomination
(`use_candidate_nominates`); `docs/15-design.md` states the ICE-lite scope (what's out: TURN,
mDNS candidates, ICE restart) and the credential/exchange model.

*Concept to internalize:* why NAT makes "just send a packet to the peer" impossible, what ICE's
check-and-nominate dance actually accomplishes, and why STUN authenticates every check.

### V2. Selective RTP forwarding — *rewrite one continuous stream out of a switching origin*
In `src/forward.rs`, build the **per-subscriber [`Rewriter`]** — the primitive at the heart of an
SFU. The SFU forwards the publisher's encoded RTP untouched in *payload*, but each subscriber
must see one **continuous** RTP stream even though the SFU is dropping packets under them
(deselected simulcast layers, packets that lost a pacing race) and switching which origin feeds
them (V3). A gap in the sequence number reads as loss to a browser's jitter buffer and triggers a
pointless NACK; a jump in SSRC or a backwards timestamp breaks playback outright.

So per subscriber the SFU keeps a tiny rewriter that maps whatever origin currently feeds it onto
that subscriber's **own** line: one **stable outbound SSRC**, outbound sequence numbers that
increase by exactly one **regardless of SFU-side drops** (wrapping at 65535), and a timestamp that
stays monotonic **across an origin switch**. And it must remember enough of that mapping to
**translate a NACK back**: when a subscriber asks to re-send *its* sequence 4127, the SFU has to
know that was the origin's sequence 5981 — the reliability you route across a rewrite. All of it
in **bounded** memory: the outbound→origin history is a fixed-size window, so a subscriber that
never NACKs can't grow it without end.

**Done when ALL true:**
- [ ] **Contiguous outbound stream:** forwarding a run of origin packets yields outbound
  sequence numbers that increase by exactly 1 under one stable outbound SSRC — **even when some
  origin packets are skipped** (an SFU-introduced drop leaves *no* outbound gap).
- [ ] **Continuous across an origin switch:** when the feeding origin SSRC changes mid-stream (a
  layer switch), the subscriber's outbound sequence stays contiguous and its timestamp stays
  monotonic — the switch is invisible downstream.
- [ ] **NACK translation:** an outbound sequence maps back to the exact origin sequence it was
  forwarded from, **including across the 16-bit wrap**; a sequence that has aged out of the
  bounded window maps to *nothing* (too old to usefully retransmit — same deadline logic as p14).
- [ ] **Isolation:** two subscribers fed from the same origin get **independent** outbound lines
  — a drop or NACK on one never perturbs the other's sequence numbering.
- [ ] **Bounded state:** the per-subscriber rewriter's memory is fixed-size regardless of stream
  length or NACK volume.

**Proof:** unit/property tests `rewrite_is_contiguous`, `rewrite_survives_origin_switch`,
`nack_translates_back` (with an `across_the_wrap` case), and `two_rewriters_are_independent`;
`docs/15-design.md` records the sequence-continuity scheme and the NACK-window size + staleness
bound.

*Concept to internalize:* why an SFU (unlike a dumb relay) *must* rewrite RTP headers, why
sequence continuity is a correctness property (not cosmetics), and what state a NACK translation
actually needs.

### V3. Simulcast layer selection — *give each subscriber the quality their link can take*
In `src/simulcast.rs`, build the **per-subscriber [`LayerSelector`]** that decides *which* of a
publisher's simulcast encodings to forward. The publisher sends the same video several times at
once — a low (~150 kbps), a mid (~500 kbps), and a high (~2 Mbps) layer, each its own SSRC — and
the SFU forwards exactly **one** to each subscriber: the highest layer that fits that subscriber's
estimated downlink budget (from V4). This is the SFU's superpower over a naive relay — it adapts
quality per viewer **without decoding a pixel**.

The subtlety is *when* you're allowed to switch. You can only **switch up** to a higher layer at a
frame the decoder can start from cleanly — a **keyframe** — because every other frame references
earlier frames the subscriber never received. So an up-switch means: send a **keyframe request
(PLI/FIR)** upstream to the publisher, keep forwarding the current layer, and only start
forwarding the new layer from its next keyframe. A **down-switch** is always safe immediately.
And the switch must be **invisible** downstream — the subscriber sees one continuous SSRC/seq/ts
line (V2's rewriter), so a layer switch is a change of *which origin feeds the rewriter*, never a
change the subscriber's jitter buffer notices.

**Done when ALL true:**
- [ ] **Picks the right layer for the budget:** given low/mid/high layers, a budget just above
  the mid bitrate selects mid; a budget below the lowest layer still selects the lowest (the SFU
  never forwards *nothing* while a layer exists) — never a layer above budget.
- [ ] **Up-switch waits for a keyframe:** after the budget rises past a higher layer, the selector
  reports it **wants a keyframe** and keeps forwarding the *old* layer until a keyframe arrives on
  the target layer, then commits — and the keyframe-owed flag clears (so exactly one PLI is
  requested per switch, not one per packet).
- [ ] **Down-switch is immediate:** a budget drop switches to a lower layer without waiting for a
  keyframe (dropping to a layer you already have is always safe).
- [ ] **Deselected layers are dropped:** packets from any layer other than the currently forwarded
  one return a drop decision (and the rewriter is told to `skip`, keeping the outbound line
  gapless).
- [ ] **The switch is invisible:** across an up- or down-switch the subscriber's outbound SSRC and
  sequence continuity hold (this is V2 composed with V3, exercised together).

**Proof:** unit tests `picks_highest_fitting_layer`, `up_switch_waits_for_keyframe`,
`down_switch_is_immediate`, `deselected_layer_is_dropped`; `docs/15-design.md` records the
selection policy (hysteresis to avoid flapping? margin below budget?) and the keyframe-request
mechanism (PLI vs FIR).

*Concept to internalize:* simulcast vs SVC vs transcoding as three points on a cost/flexibility
curve; why decodability (keyframes / GoP structure) constrains *when* you can switch; and why the
switch has to be hidden behind a stable downstream identity.

### V4. Bandwidth estimation — *figure out how much each subscriber's link can take*
In `src/bwe.rs`, build the **per-subscriber [`BandwidthEstimator`]** (a receive-side, GCC-lite
controller) whose number feeds V3's layer choice. Nobody tells the SFU a subscriber's downlink
capacity — it must **estimate it from feedback**, and the estimate has to track the link as it
moves (someone starts a download, a phone drops to 3G). Two signals drive it. The **delay-based**
signal is the early, subtle one: from transport-wide feedback (TWCC) the estimator watches the
**inter-arrival delay gradient** — if packets sent 10 ms apart start arriving 15 ms apart, a queue
is building on the path *before* it overflows into loss, so ease off now. The **loss-based**
signal is the blunt backstop: sustained loss (from RTCP receiver reports) means you're already
over — cut hard; near-zero loss means probe up. A real controller blends both, clamps to
`[min, max]`, and an **allocator** then splits that per-subscriber budget across the streams the
subscriber receives — which is exactly what V3 consumes.

**Done when ALL true:**
- [ ] **Reacts to the delay signal:** a batch of feedback whose *arrival spacing grows relative to
  send spacing* (a building queue) **lowers** the estimate; a flat/clean batch lets it **climb**.
- [ ] **Reacts to the loss signal:** a high loss fraction (≳10%) lowers the estimate
  multiplicatively; near-zero loss (≲2%) lets it probe up; the two signals combine conservatively
  (the lower estimate wins).
- [ ] **Clamped and robust:** the estimate stays within `[min, max]` and never goes negative,
  zero-stuck, unbounded, or NaN — no sequence of hostile/garbage feedback drives it out of range.
- [ ] **Converges + recovers:** on a link capped at capacity *C* the estimate settles near *C*
  (within a documented margin) without wild oscillation; after a sudden capacity drop it **backs
  off** within a bounded time and **climbs back** once the link clears.
- [ ] **The allocator reserves headroom:** splitting a budget across a subscriber's streams never
  hands out 100% (leaves room to probe) and sums to ≤ budget.

**Proof:** unit tests `backs_off_on_rising_delay`, `backs_off_on_loss`, `recovers_on_clear`,
`stays_clamped`, `allocator_reserves_headroom`; a simulated capacity-step test showing convergence
+ recovery in the bench harness; `docs/15-design.md` records the control law (delay + loss, the
AIMD/GCC-lite blend) and the allocation policy.

*Concept to internalize:* why the SFU (not the network) has to estimate each downlink; the
delay-gradient-vs-loss signal tradeoff (early vs certain); and how the estimate closes the loop
back into layer selection — measure → estimate → select → re-measure.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols / API
- [ ] **Interoperates with a real WebRTC stack:** a browser (`RTCPeerConnection` + simulcast
  `sendEncodings`) or `gstreamer webrtcbin` completes ICE against the SFU and its media is
  forwarded to a subscriber — not just against a hand-rolled client. The signaling shape (JSON
  stand-in vs. real SDP) and its limits are stated in `docs/15-design.md`.
- [ ] **RTCP feedback both ways:** the SFU consumes receiver reports / NACK / TWCC (or REMB) from
  subscribers and sends **PLI/FIR** upstream to publishers on a layer up-switch; the RTP/RTCP mux
  choice and the feedback formats supported are documented.
- [ ] **Graceful shutdown:** on SIGTERM the SFU stops forwarding, in-flight admin/signaling HTTP
  requests drain, and peers are signalled/torn down cleanly — no half-open sessions leaked.

### Security / abuse protection
- [ ] **Every parser is bounds-checked** so a malicious sender on the open UDP port can't OOM or
  panic the process: STUN attribute lengths, RTP header length, and RTCP length words are all
  range-checked before indexing/allocating; an oversized or truncated datagram is dropped.
- [ ] **Media is authenticated to a peer:** RTP/RTCP is only accepted from an address that
  **completed ICE** (nominated via an integrity-checked STUN check); a stray or spoofed source is
  ignored, not forwarded. The SRTP (media encryption) story is *named* as in- or out-of-scope in
  `docs/15-design.md` (DTLS-SRTP is a large stretch; say so explicitly).
- [ ] **Bounded everything:** per-subscriber rewriter windows, the room/peer tables, and any
  retransmit/history caches are all capped, and the signaling API enforces `MAX_ROOMS` /
  `MAX_PEERS_PER_ROOM` — a join flood or a chatty peer degrades itself, never the process.

### Observability
- [ ] A `tracing` span/context per peer (keyed by room + peer id / SSRC), with structured logs for
  lifecycle events (join/leave, ICE nominated, layer switch, keyframe request) — never log media
  payload bytes.
- [ ] Counters at `/metrics`: **RTP received vs forwarded** (the fan-out amplification),
  **bytes forwarded, packets dropped by reason, STUN messages, ICE nominations, layer switches
  (up/down), keyframe requests, NACKs translated.**
- [ ] Gauges: **rooms, peers by role, estimated vs selected bitrate** per (busy) subscriber —
  enough to watch a subscriber's quality adapt in real time.

---

## Cross-cutting scale skills
- **Fan-out amplification is the workload:** one ingress packet becomes N egress packets; you
  reason about egress pps and per-subscriber cost, and the SFU's value is doing that fan-out
  *without* the N² of a mesh or the transcode of an MCU.
- **No transcode, ever:** the payload is forwarded byte-identical; all adaptation is *selection*
  (which layer) and *rewriting* (headers), so CPU stays flat as quality adapts — proving CPU
  doesn't scale with bitrate is part of the win.
- **Per-subscriber control loops:** each subscriber has its own estimate → layer → rewrite loop
  running independently; you reason about convergence and stability per viewer, not globally.
- **Bounded memory on an open port:** rewriter windows, room tables, and history caches are all
  fixed-size — a hostile or broken peer degrades its own session, never your process.
- **Reachability before transport:** ICE means "can these two even exchange a packet?" is a
  problem you solve *first*, and authentication (STUN integrity) is what stops an open UDP port
  from being an open door.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the fan-out/adaptation load test lives in `bench/`,
   the numbers in `docs/15-benchmarks.md`.
3. `docs/15-design.md` records the decisions the SPEC grades: the **ICE-lite scope + credential
   model** (V1), the **sequence-continuity + NACK-translation scheme** (V2), the **layer-selection
   + keyframe-switch policy** (V3), the **BWE control law + allocation** (V4), and the SRTP/SDP
   scope calls.
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p webrtc-sfu` are green; no
   `todo!()` remains on a checked path.

## 🐉 Boss fight — The Crowded Room

> One publisher, streaming three simulcast layers into a room. Then the room fills: dozens of
> subscribers join in a burst, and their links are all over the map — half on fibre, some on a
> throttled 600 kbps mobile connection, a few on a link that sags to a quarter of its capacity
> mid-call and recovers. A dumb relay would forward every layer to everyone and melt the weak
> links while starving the strong ones; an MCU would transcode and cook a CPU. Your SFU has to
> route the *right* layer to each viewer, switch cleanly as their links move, keep every viewer's
> stream continuous through it all — and do it forwarding bytes it never decodes.

**Arena:** `bench/` runs a **release build** (`cargo run --release`). One publisher (a browser /
`webrtcbin` / synthetic sender at ~1.5 Mbps total across **3 simulcast layers**) publishes into a
room; a load harness spins up **≥ 50 subscribers** that ICE-connect and receive, on a spread of
`tc netem` downlink profiles (fibre, 600 kbps cap, and a "sagging" profile that drops to 25% for
60 s partway through and recovers). The run lasts **≥ 5 minutes**; quality is measured from the
subscribers' received streams + the SFU's metrics, not vibes.

**The boss falls when ALL true:**
- [ ] **Fan-out holds:** the SFU sustains the full egress forwarding rate (**≥ 50× the ingress
  packet rate** to ≥ 50 subscribers) for the whole run with **forwarding p99 ≤ 10 ms** (ingress
  packet → egress `send_to`).
- [ ] **Each link gets the right layer:** capped/mobile subscribers converge to the **low** layer
  and fibre subscribers to the **high** layer within **≤ 3 s** of joining — no subscriber is sent
  a layer above its estimated budget, and no fibre subscriber is stuck on low.
- [ ] **Switches are clean:** across every up/down layer switch (including the 25% sag and its
  recovery) each subscriber's outbound stream stays **continuous** — gapless sequence, stable
  SSRC, no decoder-visible break — and an up-switch commits **only** on a keyframe.
- [ ] **No transcode tax:** CPU stays **flat** as subscriber count and bitrate rise (proving the
  SFU forwards, not transcodes) — documented CPU vs. an MCU baseline, or simply that per-packet
  cost is O(subscribers), not O(pixels).
- [ ] **Bounded memory:** RSS stays **flat** across the full run and a **join storm** (all 50
  subscribers arriving within a few seconds) — a 5-minute room and a 5-hour room use the same RAM.

**Proof:** methodology + the fan-out/forwarding-latency numbers, the per-profile
convergence-time + selected-layer trace, the continuity check across switches, and the flat
CPU/RSS plots in `docs/15-benchmarks.md` (hardware + `tc netem` + the subscriber-harness commands
reproducible via `bench/`).

## Suggested order of attack
1. Get the boring path working: the UDP socket binds, the signaling API creates rooms + peers and
   returns ICE creds, and the admin endpoints answer — no media yet (a real client's first STUN
   check is the first `todo!()` you hit: V1 `StunMessage::parse`).
2. Build V1: the STUN codec (round-trip + integrity + fingerprint) then the ICE-lite agent —
   unit-test the codec and integrity before pointing a browser at it; a nominated pair populates
   the addr→peer route so RTP can flow.
3. Build V2: the per-subscriber `Rewriter` — contiguous outbound sequence across skips, continuity
   across an origin switch, and NACK translation; forward a single layer to a single subscriber on
   a *clean* link and confirm the browser plays it smoothly.
4. Build V3: the `LayerSelector` — pick the fitting layer, gate up-switches on a keyframe, send PLI
   upstream; drive it with a fixed budget first, then confirm switches are invisible downstream.
5. Build V4: the `BandwidthEstimator` + allocator; wire real RTCP feedback in, `tc netem` a
   subscriber's link down and up, and watch its selected layer track the estimate.
6. Add the parser bounds + ICE-gated media acceptance + metrics + graceful shutdown; then fill the
   room, degrade the links, and defeat the Crowded Room.

## Run it
```bash
cp .env.example .env          # set MEDIA_PORT / HTTP_PORT / PUBLIC_IP / limits
cargo run -p webrtc-sfu
#   The scaffold compiles and serves. Signaling + admin work immediately:
#     curl localhost:8080/healthz
#     curl -XPOST localhost:8080/rooms/demo/publish \
#          -H 'content-type: application/json' \
#          -d '{"layers":[{"rid":"q","ssrc":111,"bitrate_bps":150000},
#                          {"rid":"h","ssrc":222,"bitrate_bps":500000},
#                          {"rid":"f","ssrc":333,"bitrate_bps":2000000}]}'
#     curl localhost:8080/rooms            # see the topology
#   The media plane idles until a real client ICE-connects; the first STUN check it sends
#   hits the V1 StunMessage::parse todo!() — that panic is your worklist.

# Degrade a subscriber's downlink for the boss fight (Linux):
sudo tc qdisc add dev lo root netem rate 600kbit delay 40ms 10ms
```
