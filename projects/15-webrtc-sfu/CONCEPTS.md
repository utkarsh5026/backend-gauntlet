# Concept Bank — Project 15: WebRTC SFU (Selective Forwarding Unit)

> This is the map of what this project should leave in your head. Each card gives you the problem the concept solves, the core idea, where it runs in the real world, and the questions that prove you own it. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 0 — Mesh vs MCU vs SFU *(the framing idea)*

**The problem.** One publisher, many viewers, all on different networks. **Mesh** (everyone sends everyone): each publisher uploads N−1 copies — a 4-person call already saturates home uplinks. **MCU** (server decodes all, composites, re-encodes per viewer): scales the network but burns a CPU core per call and inserts a decode→encode latency hop into every frame.

**The idea.** The **SFU** forwards the publisher's *already-encoded* packets — payload untouched — choosing per subscriber what to forward. One upload in, one tailored stream out per viewer, zero transcoding. CPU cost is per-*packet*, not per-*pixel*; that asymmetry is why 50-person calls exist.

- [ ] You can sketch all three topologies with their per-participant upload/download/CPU costs and say why the industry converged on SFU.
- [ ] You can explain "adaptation by selection and rewriting, never by re-encoding" — and what that predicts about CPU vs subscriber count.

**In the wild:** every major conferencing system — Zoom, Meet, Teams, Discord, LiveKit, Jitsi, mediasoup — is an SFU at its core.

---

## 🧠 Card 1 — ICE/STUN: reachability before transport *(V1 · `src/ice.rs`)*

**The problem.** The browser is behind NAT: it has no public address to advertise, and unsolicited inbound packets are dropped. Before one media byte flows, both sides must discover a working address pair — and because an open UDP port takes datagrams from anyone, the discovery itself must be authenticated or a stranger can nominate themselves into your call path.

**The idea.** ICE fires **STUN Binding requests** between candidate address pairs until one works, then nominates it. A STUN message: 20-byte header (class+method packed in 14 bits, the `0x2112A442` magic cookie, a 96-bit transaction id) + aligned attribute TLVs. The response's **XOR-MAPPED-ADDRESS** tells the sender what address the world sees — XORed with the cookie so payload-rewriting NATs can't corrupt it. Every check carries **MESSAGE-INTEGRITY** (HMAC-SHA1 keyed by the ICE password) — an unauthenticated check must never nominate a path. Your side is **ICE-lite**: a public server just answers checks correctly; the browser drives.

**In the wild:** every WebRTC connection ever made starts with this dance; TURN relays exist for the NATs where no direct pair works; the same STUN codec runs in game networking and VoIP.

**You own it when you can explain:**
- [ ] Why NAT breaks naive peer-to-peer (no advertisable address, drop-unsolicited-inbound) and what an ICE "candidate" is.
- [ ] The STUN message layout well enough to decode one from hex, and why the magic cookie doubles as a demux discriminator on a muxed port.
- [ ] Why XOR-MAPPED-ADDRESS is XORed — the ALG middlebox problem it defeats.
- [ ] What MESSAGE-INTEGRITY authenticates, where the key comes from (signaling-exchanged ufrag/pwd), and the takeover you'd allow by nominating unauthenticated checks.
- [ ] ICE-lite vs full ICE: what a publicly-addressable server can skip and why.

**Depth probes:**
- Why must media only be accepted from nominated addresses afterward — what does that make ICE, security-wise (the port's auth layer)?
- Where does TURN fit, and why is it the expensive last resort?

**Trap:** debugging "call never connects" at the media layer. If any byte of the STUN response is wrong (integrity, XOR, txid), the browser *silently discards it* — reachability bugs look like everything-else bugs.

---

## 🧠 Card 2 — Per-subscriber RTP rewriting *(V2 · `src/forward.rs`)*

**The problem.** "Forward the packet" is a lie the moment you *don't* forward some packets. The SFU deliberately drops (deselected layers) and switches origins (layer changes) — but the subscriber's browser runs a normal jitter buffer that treats any sequence gap as network loss (cue pointless NACKs) and any SSRC/timestamp jump as a broken stream. The SFU's editorial decisions must be *invisible* downstream.

**The idea.** Per subscriber, a tiny **rewriter** owns that subscriber's outbound identity: one stable SSRC, sequence numbers advancing by exactly 1 no matter what was skipped upstream, timestamps monotonic across origin switches. It keeps a bounded outbound→origin mapping window so a subscriber's NACK for *its* seq 4127 can be translated back to the origin packet that produced it — reliability routed across a rewrite. Every subscriber's line is independent: one viewer's drops never perturb another's numbering.

**In the wild:** mediasoup/LiveKit/Pion all have this exact component (usually named "sequence number rewriter" / RTP munger); its bugs are legendarily visible (frozen video on layer switch).

**You own it when you can explain:**
- [ ] Why gapless outbound sequencing is a *correctness* property, traced through the subscriber's jitter buffer and NACK generator.
- [ ] What state a rewriter keeps (offsets, last-forwarded marks, the translation window) and why it's O(1) per subscriber.
- [ ] The origin-switch case: what jumps on the input side (SSRC, seq base, timestamp base) and what must not jump on the output side.
- [ ] NACK translation across the 16-bit wrap, and why an aged-out mapping correctly answers "nothing" (deadline logic from project 14).
- [ ] Why isolation between subscribers matters (independent losses, independent NACKs, independent switches).

**Depth probes:**
- The rewriter drops a packet that carried the frame's marker bit. What does the subscriber's frame-assembly see, and does your design care?
- Why is per-subscriber state the SFU's true scaling cost — and what does that make a 10k-viewer "SFU" want to become (a tree — project 17)?

**Trap:** rewriting sequence numbers but forwarding timestamps raw across an origin switch. Video freezes only *sometimes* (when bases differ enough) — the intermittent bug that teaches you all three fields must be rewritten coherently.

---

## 🧠 Card 3 — Simulcast: quality per viewer without decoding *(V3 · `src/simulcast.rs`)*

**The problem.** One encoding can't serve a fibre viewer and a 3G viewer — pick high and the weak link drowns; pick low and everyone watches pixels. The SFU can't transcode (that's the MCU trap). So adaptation must happen by *choosing among* encodings, which means the publisher must offer more than one.

**The idea.** **Simulcast**: the publisher encodes the same video ~3 times (low/mid/high, each its own SSRC) and uploads all of them; the SFU forwards exactly one per subscriber — the highest fitting that subscriber's estimated budget (V4). The catch is *when* you may switch: **up-switches wait for a keyframe** on the target layer (every other frame references history the subscriber never received), so the SFU sends one PLI upstream, keeps forwarding the old layer, and commits at the keyframe. **Down-switches are immediate** (you already have what you need). V2's rewriter hides the seam either way.

**In the wild:** `sendEncodings` in every browser's WebRTC API is simulcast; Zoom/Meet run on it; SVC (layered encoding) is the more elegant sibling with different codec support tradeoffs.

**You own it when you can explain:**
- [ ] Simulcast vs SVC vs transcode as three points on the cost/flexibility curve (publisher upload vs server CPU vs granularity).
- [ ] The decodability constraint: exactly why an up-switch needs a keyframe and a down-switch doesn't.
- [ ] The PLI discipline: requested once per switch (a keyframe-owed flag), not once per packet — and what a PLI storm does to a publisher.
- [ ] Why "never forward nothing": a budget below the lowest layer still gets the lowest layer.
- [ ] How V2+V3 compose: a layer switch is "change which origin feeds the rewriter", invisible by construction.

**Depth probes:**
- What does simulcast cost the *publisher* (encode CPU + ~1.7× upload) and when is that unacceptable (mobile publishers)?
- Hysteresis: a subscriber's estimate oscillates around the mid/high boundary. What flapping does naive selection cause, and what damping fixes it?

**Trap:** switching up the instant the budget allows. Without the keyframe wait you feed the decoder deltas against frames it never had — corruption that self-heals at the next keyframe, making it maddening to reproduce.

---

## 🧠 Card 4 — Bandwidth estimation: closing the loop *(V4 · `src/bwe.rs`)*

**The problem.** The layer selector needs a number — this subscriber's downlink budget — and nobody supplies it. The link changes (someone starts a download, a phone walks out of wifi), and sending above it doesn't just degrade this viewer: the queue it builds delays *everything* on their link.

**The idea.** Estimate from feedback, per subscriber (project 14's V4, now receive-side and multiplied by N viewers). The **delay gradient** from transport-wide feedback (send spacing vs arrival spacing) catches a building queue *before* loss; the **loss fraction** from RRs is the blunt backstop; blend conservatively (lower wins), clamp to [min, max], converge-and-recover on capacity steps. An **allocator** splits the per-subscriber budget across their streams, reserving headroom — never hand out 100%, or there's no room to discover the link improved.

**In the wild:** GCC (Google Congestion Control) is this, running per-connection in every browser; TWCC feedback is its standard transport; LiveKit/mediasoup implement server-side estimators feeding exactly this layer choice.

**You own it when you can explain:**
- [ ] Why estimation must be per-subscriber and server-side — each downlink is a different, unshared bottleneck.
- [ ] The delay-gradient signal mechanically: sent 10 ms apart, arriving 15 ms apart ⇒ queue building ⇒ back off *now*, pre-loss.
- [ ] Why the blend takes the lower estimate, and what each signal's failure mode is (delay: noise; loss: too late).
- [ ] The full closed loop: feedback → estimate → allocator → layer selection → changed send rate → new feedback — and what stability/oscillation mean for a *viewer's experience* (quality flapping).
- [ ] The robustness clamps: hostile/garbage feedback must never drive the estimate out of range or to NaN.

**Depth probes:**
- The estimate says 600 kbps but layers are 150/500/2000. What does the allocator+selector do with the 100 kbps of headroom logic?
- How do you *probe* upward without risking the very congestion you're avoiding (padding probes, cautious additive increase)?

**Trap:** one global estimate "since the server's uplink is shared". The bottleneck is each viewer's *downlink* — averaging them starves fibre viewers and drowns mobile ones simultaneously.

---

## ⚡ Rapid-fire round

- [ ] RFC 7983 demux: how STUN/RTP/RTCP share one UDP port (first-byte ranges) and why bundling matters (one NAT binding, one ICE).
- [ ] Media accepted only from ICE-nominated addresses — the line between an open UDP port and an authenticated media path.
- [ ] PLI vs FIR: the two keyframe requests and when each is proper.
- [ ] Bounded everything: rewriter windows, room tables, MAX_PEERS — a join flood degrades itself, not the process.
- [ ] The SFU-health metrics: forwarded/ingress amplification ratio, drops by reason, layer switches, estimated-vs-selected bitrate per subscriber.
- [ ] What SRTP would add (media encryption via DTLS keying) and why it's scoped out here — name the boundary honestly.

## 🔗 Connects to

- Project 14 is the transport this SFU manipulates — jitter/NACK/BWE knowledge is assumed, then *routed through a middlebox*.
- Project 17 scales this to a planet: the rewriter, selector, and estimator all reappear composed across regions.
- The per-subscriber control loop is the same feedback-loop discipline as project 16's autoscaler — different actuator, same shape.
