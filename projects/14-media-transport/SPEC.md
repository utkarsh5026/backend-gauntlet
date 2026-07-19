<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 14 — Real-time Media Transport (RTP/RTCP)

> Projects 11–13 always had **HTTP** to hide behind: TCP guaranteed the bytes arrived,
> in order, eventually, and a player could buffer its way over any bump. This one takes
> that safety net away. Real-time media — a video call, a game stream, an esports feed —
> cannot wait for TCP's retransmit-and-reorder, because a packet that arrives 400 ms late
> is *useless*: the moment it was meant to be shown has already passed. So real-time media
> runs on **UDP**, which gives you exactly nothing — packets are dropped, duplicated,
> reordered, and delayed by a jittery amount, and the path's available bandwidth changes
> under your feet. **RTP** is the thin header that makes a stream of media out of those
> lonely datagrams (a sequence number, a media timestamp, a source id); **RTCP** is the
> back-channel that lets the receiver tell the sender what it's actually seeing (loss,
> jitter, "I'm missing packet 4127"). On top of those two headers you build the three
> things that turn a lossy pipe into watchable video: a **jitter buffer** that reorders
> and paces packets into a smooth playout despite variable delay; **selective
> retransmission (NACK)** that recovers the losses that matter *before their deadline* and
> ignores the ones that don't; and **congestion control** that paces your send rate to the
> bandwidth the path actually has, instead of drowning it. None of this is a library call
> here — it's the part you'd normally hand to `webrtc-rs`/`libwebrtc`, and it's the part
> where "the network is reliable" stops being a fallacy you can afford.

## What it does (the easy part)
- Binds a **UDP** socket on `RTP_PORT` (default `5004`) and runs in one of two roles
  (`ROLE=sender|receiver`, default `receiver`):
  - **sender:** pulls access units from a media source, **packetizes** them into RTP,
    paces them onto the wire under congestion control, and answers RTCP feedback
    (retransmitting NACK'd packets from a small history cache).
  - **receiver:** reads RTP datagrams, runs them through the **jitter buffer**
    (reorder + de-dup + playout pacing), **depacketizes** complete frames for playout,
    and sends RTCP back (receiver reports + NACK for the gaps worth recovering).
- Exposes a tiny **admin/observability** HTTP surface on `HTTP_PORT` (default `8080`):
  `GET /healthz` / `GET /readyz` liveness/readiness, `GET /status`, and `GET /metrics`
  (Prometheus). The media plane is UDP; this is only health + metrics.

> There is **no database and no docker-compose** here: the media plane is a raw UDP
> socket and everything else lives in-process. A built-in **synthetic media source**
> (`src/media.rs`, fully wired) emits constant-bitrate fake access units so the pipeline
> has something to carry before you point a real encoder at it. To exercise it for real
> you send RTP from `ffmpeg`/`gstreamer` (e.g. `ffmpeg -re -i in.mp4 -an -c:v copy -f rtp
> rtp://127.0.0.1:5004`) and, for the boss fight, degrade the path with `tc netem` (loss,
> delay, reorder, a bandwidth cap). The parts you'd normally hand to `webrtc-rs` — the RTP
> packetizer, the jitter buffer, the RTCP/NACK loop, the bandwidth estimator — are exactly
> the parts you build.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. RTP packetization + depacketization — *turn a frame into datagrams and back*
In `src/rtp.rs`, parse and build the **RTP header** by hand, and packetize an encoded
access unit into one or more RTP packets — fragmenting when a frame is larger than the
path MTU, and reassembling on the far side. This is the wire floor everything else stands
on, and it's pure binary layout over a datagram — no library.

An **RTP header** is 12 bytes: a 2-bit version (`2`), padding/extension/CSRC-count bits, a
marker bit (set on the **last** packet of a frame), a 7-bit payload type, a 16-bit
**sequence number** (increments per packet, wraps at 65535), a 32-bit **timestamp** (the
media clock — 90 kHz for video, so it does *not* increment per packet but per sampling
instant), and a 32-bit **SSRC** (the source id), optionally followed by CSRC entries. A
video frame rarely fits in one datagram, so H.264 defines **FU-A fragmentation**: a NAL
unit too big for the MTU is split across packets, each carrying a fragmentation-unit
header with **start**/**end** bits so the receiver can stitch it back together. Small NALs
ship as a single packet. The receiver collects a frame's packets (same timestamp, ending
at the marker) and reassembles the original access unit.

**Done when ALL true:**
- [ ] The **RTP header round-trips**: parse∘write (and write∘parse) is identity on the
  fields that matter — version, marker, payload type, sequence, timestamp, SSRC, CSRCs —
  and a buffer shorter than a full header is a clean error, never a panic or OOB read.
- [ ] An access unit **larger than the MTU is fragmented** into multiple RTP packets whose
  payloads reassemble back to the exact original bytes; one small enough ships as a
  **single** packet.
- [ ] Within a frame, **sequence numbers are consecutive**, every packet shares the frame's
  **timestamp**, and **only the last** packet has the **marker bit** set.
- [ ] Depacketization **reassembles a frame from its packets in the right order** and
  detects an incomplete frame (a missing fragment) rather than emitting corrupt bytes.
- [ ] Packetization is **MTU-respecting**: no emitted packet exceeds the configured MTU
  (header + payload), so nothing relies on IP-layer fragmentation.

**Proof:** unit/property tests round-tripping the header (`rtp_header_roundtrips`), a
fragment-then-reassemble test over a >MTU access unit (`fragmented_frame_reassembles`),
and a marker/sequence invariant test (`packet_sequence_and_marker_are_correct`);
`docs/14-design.md` notes the payload format chosen and the MTU budget.

*Concept to internalize:* why media uses a per-frame *media* timestamp separate from the
per-packet sequence number, why the marker bit exists, and why you fragment at the
application layer (path MTU / avoiding IP fragmentation) instead of letting IP do it.

### V2. Jitter buffer — *make a smooth playout out of a jittery arrival*
In `src/jitter.rs`, build the **playout buffer** that sits between "packets arrive from the
network, whenever" and "frames are shown at a steady cadence". This is the vertical that
makes real-time media *watchable*: the network delivers packets early, late, out of order,
and duplicated, and the jitter buffer turns that into an ordered, evenly-paced stream at
the cost of a small, bounded amount of added latency.

Packets arrive with variable **inter-arrival delay** (jitter). If you played each the
instant it arrived, the picture would stutter. Instead you hold a small window (a *target
delay*), release packets **in sequence order** once they've waited it out, **drop
duplicates and packets that arrive too late** to be useful, and track the gaps (missing
sequence numbers) so V3 can ask for them back. Two things make it subtle: the 16-bit
sequence number **wraps** (65535 → 0), so you must unwrap it to a monotonic index to order
correctly across the wrap; and you must estimate arriving **jitter** (RFC 3550 gives a
smoothed inter-arrival estimate) to size the buffer — too small and you stutter, too large
and you add needless latency.

**Done when ALL true:**
- [ ] **Reordering:** packets inserted out of order come out in ascending sequence order,
  and this holds **across the 16-bit wraparound** (…65534, 65535, 0, 1…) — no packet is
  mis-ordered or dropped at the wrap.
- [ ] **De-duplication:** a duplicate sequence number is counted and discarded, not
  played twice.
- [ ] **Playout pacing:** a packet is not released before it has waited the configured
  **target delay**, and a **complete frame** (all packets up to a marker) is released as a
  unit; the added latency stays within the configured bound.
- [ ] **Late/lost handling:** a packet that arrives after its playout deadline is dropped
  and counted as *late*; a gap that is never filled is eventually skipped so the buffer
  **never stalls forever** waiting for a packet that isn't coming.
- [ ] **Gap reporting + jitter estimate:** the buffer reports the missing sequence numbers
  below the highest received (the NACK candidates) and exposes a smoothed **inter-arrival
  jitter** estimate and current buffer depth.

**Proof:** unit tests for out-of-order insert, duplicate drop, and wraparound ordering
(`reorders_out_of_order`, `drops_duplicates`, `orders_across_sequence_wrap`); a test that a
never-arriving packet is skipped rather than stalling (`gap_is_skipped_not_stalled`); a
property test that random insert orders always play out sorted (`playout_is_always_ordered`);
`docs/14-design.md` records the target-delay/adaptivity policy.

*Concept to internalize:* the latency-vs-smoothness tradeoff a jitter buffer *is*; why a
16-bit sequence needs unwrapping; and why "wait a little, then give up" beats both
"play immediately" and "wait forever".

### V3. RTCP + selective retransmission (NACK) — *recover the losses that still matter*
In `src/rtcp.rs`, build the **control channel**: parse and build RTCP compound packets
(receiver reports and RFC 4585 **generic NACK** feedback), and wire the recover loop — the
receiver asks for the specific packets it's missing, the sender **retransmits** them from a
small history cache, but only while they can still arrive **before their playout deadline**.
This is reliability you *choose*, per packet, instead of TCP's reliability you're forced to
pay for on every byte.

**RTCP** rides alongside RTP (its own packets, conventionally the next port or muxed).
A **Receiver Report** (RR) tells the sender the fraction of packets lost, the cumulative
loss, the highest sequence seen, and the interarrival jitter — the sender's window into
what the receiver experiences. A **generic NACK** (RTPFB, feedback message type 1) names
lost packets compactly: a **PID** (a base sequence number) plus a 16-bit **BLP** bitmask
covering the next 16 sequence numbers, so one FCI word can request up to 17 packets. The
sender keeps a bounded **retransmit cache** of recently sent packets and, on a NACK, resends
the ones it still holds — but a good sender **won't retransmit a packet that can no longer
arrive in time**, because that just wastes bandwidth the congestion controller needs.

**Done when ALL true:**
- [ ] RTCP **parses and builds**: a receiver report and a generic-NACK feedback packet
  round-trip (build∘parse is identity on their fields), a **compound** RTCP packet (multiple
  stacked) parses into its parts, and a truncated/garbage datagram errors without panicking.
- [ ] The **NACK FCI is correct**: a set of missing sequence numbers packs into PID+BLP
  words and unpacks back to exactly that set, **including across the sequence wrap**, using
  the bitmask (not one packet per missing seq).
- [ ] **Retransmission works:** on receiving a NACK the sender resends the requested packets
  it still holds in its bounded cache; a packet **evicted** from the cache is simply not
  resent (no crash, no unbounded memory).
- [ ] **Deadline-aware:** the sender does **not** retransmit a packet whose playout deadline
  has already passed (a documented staleness bound) — recovery serves latency, it isn't
  reliability at any cost.
- [ ] The receiver **generates NACKs from real gaps** (V2's missing list), rate-limited so a
  burst of loss doesn't melt the back-channel, and **effective** loss after recovery is
  measurably lower than raw network loss.

**Proof:** unit tests round-tripping RR and NACK and packing/unpacking a wrapped NACK
bitmask (`rtcp_roundtrips`, `nack_bitmask_packs_missing`, `nack_packs_across_wrap`); an
integration test that a dropped packet is NACK'd and retransmitted and arrives before
deadline (`nack_recovers_dropped_packet`), and that a too-late packet is *not* retransmitted
(`stale_packet_not_retransmitted`); `docs/14-design.md` records the retransmit-cache size
and the staleness bound.

*Concept to internalize:* why real-time media uses *selective, deadline-bounded*
retransmission instead of TCP-style total reliability; the RR/NACK feedback vocabulary; and
the PID+BLP trick for naming many losses in a few bytes.

### V4. Congestion control — *pace to the bandwidth the path actually has*
In `src/congestion.rs`, build the **bandwidth estimator + pacer** that decides how fast to
send. UDP won't slow you down when the path is full — it just drops your packets, spikes
delay, and quietly destroys the stream. So the sender must **estimate the available
bandwidth** from the feedback signals (loss and/or one-way delay change) and **pace** its
output to match, backing off when the path congests and probing up when it clears. This is
the difference between a stream that stays smooth when the link degrades and one that
collapses the moment someone else uses the network.

Two classic signals drive the estimate: **loss-based** (rising loss ⇒ you're overshooting,
cut the rate; near-zero loss ⇒ probe higher) and **delay-based** (a growing one-way delay
gradient means a queue is building *before* it overflows into loss — the earlier, gentler
signal). A real controller (Google's GCC, the basis of WebRTC) blends both, clamps the rate
between a min and max, and paces packets out smoothly (a **leaky-bucket / token pacer**)
instead of bursting a whole frame at once. You choose how sophisticated to go — even a
clean loss-based AIMD controller with a pacer is a passing V4 — but it must **converge,
back off under congestion, and recover**.

**Done when ALL true:**
- [ ] The estimate **reacts to loss/delay feedback**: sustained loss (or a rising delay
  gradient) **lowers** the target bitrate; a clean path lets it **climb back** toward the
  max — it is not a constant.
- [ ] The target bitrate is **clamped** to `[min, max]` and never goes negative, zero-stuck,
  or unbounded — a hostile/garbage feedback value can't drive it out of range.
- [ ] The sender is **paced**: packets leave spread across the frame interval at roughly the
  target rate (a leaky-bucket/token pacer), not as an instantaneous burst — verifiable by
  the inter-send spacing under a fixed target.
- [ ] **Convergence:** on a link capped at capacity *C*, the steady-state send rate settles
  near *C* (within a documented margin) without oscillating wildly or collapsing to the
  floor.
- [ ] **Recovery:** after a sudden capacity drop the rate **backs off** within a bounded
  time and, once the link clears, **climbs back** — the controller doesn't get permanently
  stuck low or stuck high.

**Proof:** unit tests that loss raises then a clean path lowers the target and that it stays
clamped (`bitrate_backs_off_on_loss`, `bitrate_recovers_on_clear_path`,
`bitrate_stays_clamped`); a pacer spacing test (`pacer_spreads_sends`); a simulated
capacity-step test showing convergence + recovery in the bench harness; `docs/14-design.md`
records the control law (AIMD/GCC-lite), the signals used, and the pacer.

*Concept to internalize:* why UDP shifts congestion control into *your* application, the
loss-vs-delay signal tradeoff, AIMD, and why pacing (not bursting) is what keeps the queue —
and thus the latency — small.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols / API
- [ ] **RTP over UDP** interoperates with a real stack: a stream sent by `ffmpeg`/`gstreamer`
  (`-f rtp`) is received, jitter-buffered, and depacketized — not just against your own
  sender; the payload type / clock rate / packetization are stated in `docs/14-design.md`.
- [ ] **RTCP** is exchanged both ways (RR/NACK), and its transport (separate port vs.
  RTP/RTCP mux on one port) is a documented decision.
- [ ] **Graceful shutdown:** on SIGTERM the sender flushes/stops cleanly, the receiver drains
  its jitter buffer, and in-flight admin HTTP requests finish — no half-written state, and an
  RTCP **BYE** is sent so the peer knows the source is gone.

### Reliability / correctness under loss
- [ ] **Selective retransmission** (V3) demonstrably lowers *effective* loss below raw
  network loss, and is **deadline-bounded** so it never chases packets that can't arrive in
  time.
- [ ] **De-dup + reorder** (V2) mean a duplicated or reordered datagram never corrupts a
  frame; a permanently missing packet degrades (a skipped/concealed frame), it does not stall.

### Security / abuse protection
- [ ] **Inputs are bounded so a malicious sender can't OOM/panic you:** the RTP/RTCP parsers
  range-check every length (CSRC count, FU headers, RTCP length words, NACK FCI count) before
  indexing/allocating; an oversized or truncated datagram is dropped, not fatal. (An open UDP
  port takes bytes from anyone.)
- [ ] **Source validation:** packets are associated with the expected **SSRC**; an
  unexpected/spoofed SSRC (or a flood from a stray source) is ignored/rate-limited rather than
  polluting the jitter buffer. The at-rest/at-wire encryption story (SRTP) is *named* as
  out-of-scope-or-not in `docs/14-design.md`.
- [ ] **Bounded memory everywhere:** the jitter buffer and the retransmit cache are both
  **capped** — a sender that never marks a frame, or a receiver flooded with future sequence
  numbers, can't grow memory without bound.

### Observability
- [ ] A `tracing` span/context per stream (SSRC), and structured logs for the lifecycle
  events (stream start/end, big loss events, bitrate steps) — never log media payload bytes.
- [ ] Counters at `/metrics`: **packets/bytes sent & received, packets lost, NACKs sent /
  received / satisfied, retransmits, duplicates, late drops.**
- [ ] Gauges/histograms: **interarrival jitter, jitter-buffer depth & added latency, target
  bitrate, and effective vs. raw loss** — enough to watch quality degrade before an eye does.

---

## Cross-cutting scale skills
- **No transport safety net:** with UDP you own loss, ordering, duplication, and pacing —
  the reliability you take is the reliability you build, chosen per packet against a deadline.
- **Latency as the primary currency:** every buffer (jitter target, retransmit window) is a
  latency knob; you measure end-to-end playout delay, not just throughput, and defend it.
- **Bounded memory on an open port:** jitter buffer, reorder window, and retransmit cache are
  all fixed-size — a hostile or broken peer degrades its own stream, never your process.
- **Feedback control loops:** RTCP + congestion control are closed loops (measure → decide →
  act → re-measure); you reason about convergence, stability, and oscillation, not one-shot
  requests.
- **Clock domains:** a 90 kHz media clock, a 16-bit wrapping sequence, and wall-clock arrival
  times are three different timelines you continuously reconcile.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the loss/latency/bandwidth test lives in
   `bench/`, the numbers in `docs/14-benchmarks.md`.
3. `docs/14-design.md` records the decisions the SPEC grades: the **RTP payload
   format + MTU/fragmentation**, the **jitter-buffer policy** (target delay, adaptivity,
   wraparound), the **RTCP/NACK + retransmit** design (cache size, staleness bound, RTP/RTCP
   mux), and the **congestion-control law** (signals, AIMD/GCC-lite, pacer).
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p media-transport` are green;
   no `todo!()` remains on a checked path.

## 🐉 Boss fight — The Lossy Mile

> The stream leaves your sender clean and arrives at the receiver through the worst mile of
> the internet: a link that drops packets, reorders them, adds a jittery 30 ms of delay, and
> whose bandwidth sags to half of yours mid-call. TCP would grind this into a buffering
> spinner. Your job is to keep the picture moving — recover the losses that still matter,
> hide the jitter, and ride the send rate down and back up with the link — for five straight
> minutes, and prove the viewer barely noticed.

**Arena:** `bench/` runs a **release build** (`cargo run --release`). One process runs
`ROLE=sender` (the synthetic source or a real `ffmpeg -re … -f rtp` feed at ~1.5 Mbps),
another `ROLE=receiver`, with a **degraded link between them** — `tc netem` (or the built-in
impairment shim) injecting **5% random loss, 30 ms delay ± 10 ms jitter, packet reorder**,
and a **token-bucket bandwidth cap** that **drops to 50% for 60 s** partway through and
recovers. The run lasts **≥ 5 minutes**; quality is measured from the receiver's playout
timeline and metrics, not vibes.

**The boss falls when ALL true:**
- [ ] **Loss is recovered:** at 5% raw network loss, **≥ 90%** of lost packets are recovered
  via NACK **before their playout deadline**, so **effective frame loss ≤ 0.5%** over the run.
- [ ] **Playout stays smooth:** **≥ 99.5%** of frames are played on time across the 5-minute
  run (late/missing < 0.5%), with **no stall longer than 300 ms** — including through the
  bandwidth-drop window.
- [ ] **Jitter is absorbed within budget:** the jitter buffer holds the 30 ms p95 network
  jitter while adding **≤ 150 ms** of playout latency (documented target), and end-to-end
  playout delay stays bounded — it does not creep upward over the run.
- [ ] **Congestion control converges + recovers:** on the capped link the steady-state send
  rate settles **within 15% of capacity** without sustaining a queue that pushes one-way
  delay **> +100 ms** (no bufferbloat collapse); after the 50% capacity drop the rate backs
  off and **re-converges within 3 s** of the link clearing.
- [ ] **Bounded memory:** RSS stays **flat** across the full run (jitter buffer + retransmit
  cache are capped) — a 5-minute call and a 5-hour call use the same RAM.

**Proof:** methodology + the effective-vs-raw-loss numbers, the playout-timeliness
distribution, the jitter/added-latency trace, and the send-rate-vs-capacity plot in
`docs/14-benchmarks.md` (hardware + `tc netem`/`ffmpeg` commands reproducible via `bench/`).

## Suggested order of attack
1. Get the boring path working: the UDP socket binds, the admin server answers `GET /healthz`
   and `GET /metrics`, and (as `ROLE=sender`) the synthetic source produces frames — no
   packetization yet (the first `todo!()` you hit is your worklist).
2. Build V1: the RTP header codec, then packetize/depacketize with FU-A fragmentation —
   unit-test the header round-trip and a >MTU fragment/reassemble before sending real bytes.
3. Build V2: the jitter buffer — reorder, de-dup, wraparound-safe ordering, and playout
   pacing; loop sender→receiver on a *clean* localhost link and confirm smooth playout first.
4. Build V3: RTCP RR + generic NACK, the retransmit cache, and the deadline-bounded recover
   loop; turn on `tc netem` loss and watch effective loss drop below raw loss.
5. Build V4: the bandwidth estimator + pacer; add the `tc netem` bandwidth cap + mid-run drop
   and get the send rate to converge and recover.
6. Add the input bounds + SSRC validation + metrics + graceful shutdown/BYE; then run the full
   degraded link for 5 minutes and defeat the Lossy Mile.

## Run it
```bash
cp .env.example .env          # set RTP_PORT / HTTP_PORT / ROLE / REMOTE_ADDR
cargo run -p media-transport
#   The scaffold compiles and serves. GET /healthz and /metrics work. As ROLE=receiver it
#   binds and idles until a packet arrives; as ROLE=sender it produces a frame and hits the
#   V1 packetize todo!() — that panic is your worklist.

# Loopback smoke test (two terminals):
RTP_PORT=5004 HTTP_PORT=8080 ROLE=receiver               cargo run -p media-transport
RTP_PORT=5005 HTTP_PORT=8081 ROLE=sender REMOTE_ADDR=127.0.0.1:5004 cargo run -p media-transport

# Feed real RTP from ffmpeg instead of the synthetic source:
ffmpeg -re -i sample.mp4 -an -c:v copy -f rtp rtp://127.0.0.1:5004

# Degrade the link for the boss fight (Linux):
sudo tc qdisc add dev lo root netem loss 5% delay 30ms 10ms reorder 25% 50%
```
