# Concept Bank — Project 14: Real-time Media Transport (RTP/RTCP)

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 0 — Why real-time media abandons TCP *(the framing idea)*

**The problem.** TCP promises every byte, in order, eventually. For a video call, "eventually" is the bug: a packet retransmitted 400 ms late describes a moment that has already been shown (or skipped) — it's not late data, it's *useless* data, and worse, TCP made every packet behind it wait too (transport-level head-of-line blocking). Deadlines, not delivery, define this domain.

**The idea.** Run on UDP — which gives you nothing — and build back *only* the reliability that pays for itself before the deadline. That inversion (reliability as a per-packet economic decision, not a transport guarantee) is the whole project.

- [ ] You can explain why TCP's in-order guarantee is *harmful* here (one lost packet stalls delivery of everything after it, even packets already arrived).
- [ ] You can name the four failure modes you now own on UDP: loss, reordering, duplication, jitter — and which vertical handles each.

---

## 🧠 Card 1 — RTP: making a stream out of datagrams *(V1 · `src/rtp.rs`)*

**The problem.** UDP datagrams are lonely — no order, no timing, no identity. To reconstruct media you need to know: which packets belong together (one frame), in what order, at what moment they should play, and from which source. Also, encoded frames routinely exceed the path MTU — but letting IP fragment for you means one lost fragment silently kills the whole datagram, with no way to ask for just the missing piece.

**The idea.** RTP's 12-byte header carries exactly the missing facts: a per-packet **sequence number** (ordering/loss detection), a per-*sampling-instant* **timestamp** on the media clock (90 kHz video — all packets of one frame share it), the **marker bit** (last packet of the frame), a **payload type**, and the **SSRC** source id. Frames larger than the MTU are fragmented at the application layer (H.264 FU-A: a fragment header with start/end bits), so loss is visible and recoverable per-fragment.

**In the wild:** RTP carries essentially all interactive media — WebRTC, SIP/VoIP telephony, IP TV contribution feeds; the payload-specific packetization rules (like FU-A) live in per-codec RFCs.

**You own it when you can explain:**
- [ ] Each RTP header field and the failure you couldn't handle without it.
- [ ] Sequence vs timestamp: why they advance independently (many packets, one frame time) and what each detects.
- [ ] Why application-layer fragmentation beats IP fragmentation (per-fragment loss visibility, path-MTU reality, middlebox behavior).
- [ ] The frame-reassembly contract: same timestamp + consecutive sequences + marker on last — and detecting an incomplete frame instead of emitting corrupt bytes.
- [ ] Why parsers on an open UDP socket are bounds-checked-total (short header, lying CSRC count → clean error).

**Depth probes:**
- Why is the RTP timestamp's *starting value* random per stream (correlation resistance), and what does that mean for cross-stream sync (spoiler: RTCP SR, project 17)?
- What breaks if two senders accidentally share an SSRC?

**Trap:** treating the timestamp as wall-clock. It's a media-clock counter with arbitrary origin — mapping it to real time is a separate mechanism (sender reports), and conflating them causes drift bugs that look like network problems.

---

## 🧠 Card 2 — The jitter buffer: smoothness bought with latency *(V2 · `src/jitter.rs`)*

**The problem.** The network delivers packets early, late, out of order, and twice. Play each the instant it arrives and motion stutters; wait for stragglers forever and the stream freezes on the first true loss. Between those failures sits a genuine trade: every millisecond of buffering absorbs a millisecond of network variance — and adds a millisecond between the speaker's mouth and the listener's ear.

**The idea.** Hold arriving packets a small **target delay**; release in sequence order, complete frames at a time; drop duplicates; count late arrivals as lost-for-playout; and *give up* on gaps that never fill (skip, conceal, move on — never stall). Two mechanics make it subtle: the 16-bit sequence wraps (unwrap to a monotonic index or ordering breaks at 65535→0), and the target delay should track measured jitter (RFC 3550's smoothed inter-arrival estimate), because a static buffer is wrong on every network but one.

**In the wild:** every VoIP phone, WebRTC's NetEq (adaptive jitter buffer with time-stretching), game networking's interpolation delay — same structure, same trade.

**You own it when you can explain:**
- [ ] The latency-vs-smoothness trade as *the* design axis — what too-small and too-large each look like to a viewer.
- [ ] Sequence unwrapping across the 16-bit wrap, and the mis-ordering bug at the boundary without it.
- [ ] The three timing verdicts for an arriving packet (on time / duplicate / too late) and what each increments.
- [ ] Why "skip the gap after a bounded wait" is the only correct loss posture — the stall-forever failure of waiting.
- [ ] How the buffer's gap list feeds V3 (these are the NACK candidates) and its jitter estimate feeds sizing.

**Depth probes:**
- Adaptive depth: jitter spikes mid-call, the buffer should grow — but growing means *pausing playout* or stretching audio. How do real implementations hide that (time-stretch, silence insertion)?
- Why does audio tolerate less jitter-buffer latency than video in a call (lip sync vs conversational turn-taking)?

**Trap:** ordering by raw sequence number. Passes every test until a stream crosses 65535, then packets vanish "randomly" — the wrap bug is a classic because test streams are short.

---

## 🧠 Card 3 — RTCP + NACK: reliability you choose per packet *(V3 · `src/rtcp.rs`)*

**The problem.** The sender is blind: it has no idea what the receiver is experiencing — loss, jitter, gaps. And full reliability is the wrong goal: retransmitting *everything* is TCP again, spending bandwidth on packets whose deadlines already passed while starving the ones that still matter.

**The idea.** RTCP is the feedback channel. **Receiver Reports** carry the experience summary (loss fraction, cumulative loss, highest sequence, jitter). **Generic NACKs** name specific missing packets compactly — a PID (base sequence) + 16-bit BLP bitmask covers up to 17 losses per word. The sender keeps a small **retransmit cache** of recent packets and resends what's still held *and still useful*: a packet past its playout deadline is not resent, period — recovery serves latency, not completeness. Receiver-side, NACKs are generated from the jitter buffer's real gaps and rate-limited so a loss burst doesn't melt the back-channel.

**In the wild:** WebRTC's loss recovery is exactly RR + NACK + RTX (plus FEC for the losses NACK can't beat); the PID+BLP FCI format is RFC 4585 verbatim.

**You own it when you can explain:**
- [ ] The selective-reliability philosophy: per-packet cost/benefit against a deadline, vs TCP's all-or-nothing.
- [ ] What each RR field tells a sender, and which one feeds congestion control (V4).
- [ ] The PID+BLP encoding by hand: pack {100, 102, 115} into FCI words; unpack across a sequence wrap.
- [ ] Both bounds on the retransmit path: cache eviction (memory) and staleness (deadline) — and why each "not resent" case is correct, not a failure.
- [ ] Why effective-loss-below-raw-loss is the measurable point of the whole loop.

**Depth probes:**
- NACK costs one RTT to recover. At what loss rate / RTT does FEC (proactive redundancy) beat retransmission? What does WebRTC do?
- Why does RTCP self-limit its bandwidth share (the ~5% rule) — what happens in a huge session otherwise?

**Trap:** treating every gap as NACK-worthy forever. A gap older than the playout deadline must *stop* being requested — NACKing the unplayable wastes exactly the bandwidth the congestion controller is trying to protect.

---

## 🧠 Card 4 — Congestion control: pacing to the path's truth *(V4 · `src/congestion.rs`)*

**The problem.** TCP would slow you down when the path fills; UDP just lets you drown it — your own packets queue, delay balloons (bufferbloat), then loss cascades, and the stream you were protecting collapses. Nobody tells you the capacity; it changes mid-call (someone starts a download); you must *estimate* it from feedback and respect the estimate.

**The idea.** Two signals, one law, one output. **Loss** says you're already over — cut multiplicatively. **Delay gradient** says a queue is *building* — the earlier, gentler signal (packets sent 10 ms apart arriving 15 ms apart = the bottleneck is filling). Blend them (delay for early warning, loss as backstop), run AIMD-style: probe additively on clean feedback, back off multiplicatively on congestion, clamp to [min, max]. Then **pace**: release packets spread across the frame interval via a token/leaky bucket, because bursting a whole frame at line rate builds the very queue you're avoiding.

**In the wild:** Google Congestion Control (GCC) — the delay+loss blend running in every WebRTC call; BBR brings the same "model the path, don't just react to loss" shift to TCP; SCReAM in 5G media.

**You own it when you can explain:**
- [ ] The bufferbloat chain: overshoot → standing queue → delay grows *before* loss → why delay is the earlier signal.
- [ ] Loss-based vs delay-based control: what each reacts to, its lag, and why the blend takes the lower estimate.
- [ ] AIMD's asymmetry (add to probe, multiply to retreat) and what convergence/backoff/recovery look like on a capacity-step link.
- [ ] Why pacing is not cosmetic: the queue math of a 50 KB frame burst at line rate vs the same bytes spread over 33 ms.
- [ ] Robustness clamps: why garbage/hostile feedback must never drive the rate negative, zero-stuck, or unbounded.

**Depth probes:**
- The estimate feeds *what* exactly? (In a real system: the encoder's target bitrate — here, the pacer/source.) What's the control lag from signal to effect?
- Two of your streams share one bottleneck. Do your controllers cooperate or fight? What does fairness even mean here?

**Trap:** testing only convergence on a stable link. The interesting behavior is the *transient*: the 50% capacity drop, the recovery without overshoot — control loops are judged on their step response.

---

## ⚡ Rapid-fire round

- [ ] The three clock domains (media clock / sequence space / wall clock) and one bug from conflating each pair.
- [ ] RTCP BYE on shutdown — the peer learns the source ended vs timing it out.
- [ ] SSRC validation: a stray/spoofed source must not pollute the jitter buffer.
- [ ] Bounded everything on an open UDP port: jitter buffer, reorder window, retransmit cache — a hostile peer degrades itself only.
- [ ] The metrics that show quality decaying before eyes do: jitter, buffer depth, effective vs raw loss, target bitrate.

## 🔗 Connects to

- Everything here is the transport under project 15's SFU (which *forwards* RTP and *translates* these NACKs) and project 17's cascade.
- The feedback-control mindset (measure→decide→act→re-measure) reappears in project 15's bandwidth estimator and project 16's autoscaler.
- The jitter buffer's bounded-window discipline is project 13's live window, receiver-side.
