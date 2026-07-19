# Concept Bank — Project 16: Live Streaming Platform (Twitch-lite)

> This is the map of what this capstone should leave in your head. There's no new primitive here — the concepts are about *integration*: making planes built in projects 03/11/12/13 behave as one system under a crowd. Check a box only when you could teach that item at a whiteboard, unprompted.

---

## 🧠 Card 1 — The control plane: state machines + reconciliation *(V1 · `src/control.rs`)*

**The problem.** Somebody must know "stream abc123 is live, ingesting on node-2, transcoding a 3-rung ladder, playable at /live/abc123" — and keep that true while ingests start, stop, crash, and *deliver their webhooks twice* (webhooks always deliver twice eventually). Hold this state in memory and a control-plane deploy forgets every live stream on the platform. Update it non-idempotently and a duplicated webhook double-enqueues an entire transcode ladder.

**The idea.** Sessions walk an explicit state machine (`Offline → Ingesting → Transcoding → Live → Ended`); each transition fans out side effects (enqueue transcode, register edge route, open chat) and teardown reverses them. Two disciplines make it production-grade: **idempotent transitions** (same webhook twice = same state once) and **reconciliation** — Postgres is the desired-state record, and on restart the control plane *reconciles* (recover live sessions, expire dead leases) rather than trusting memory. Desired-vs-actual reconciliation is the deepest pattern in the card: it's how systems self-heal instead of drift.

**In the wild:** every Kubernetes controller is a reconcile loop (this is *the* k8s idea); Twitch/Mux stream lifecycle services; payment-state machines; Temporal workflows formalize the same shape.

**You own it when you can explain:**
- [ ] Why lifecycle state must be a *machine* with legal transitions, not a status string anyone can set — what an illegal `Offline → Live` jump would skip.
- [ ] Idempotency at the webhook boundary: how the same ingest-start twice produces one session, and why "webhooks deliver at-least-once" is an axiom you design for.
- [ ] Reconciliation vs restore: not "reload state" but "compare recorded intent against observable reality and repair" — what a ghost live stream is and which check kills it.
- [ ] Why teardown must be safe to run twice, and what leaks per plane (transcode jobs, edge routes, chat channels) if any teardown path is missed.
- [ ] Admission control: why `max_streams` rejects the marginal stream instead of degrading all streams.

**Depth probes:**
- The k8s parallel, precisely: what maps to spec, what to status, what to the controller? Where does your lease/expiry fit?
- A stop webhook arrives *before* its start (reordered delivery). What does the state machine do?

**Trap:** trusting transition side effects to have happened because the transition committed. The side effect (enqueue, register) can fail independently — reconciliation exists because the world and the record *will* diverge.

---

## 🧠 Card 2 — Autoscaling on a custom signal + leases under churn *(V2 · `src/workers.rs`)*

**The problem.** Transcode is the CPU-hungry, bursty plane: one big streamer going live 10×es the work in seconds. Static provisioning either wastes a fleet or falls behind live content (which, being live, cannot wait). But autoscaling *causes* the failure it must survive: scale-down and node preemption kill workers mid-job, so the scaling mechanism itself guarantees worker deaths.

**The idea.** Close the loop with the right signal: the HPA can't see "transcode backlog", so you export queue-depth-per-worker as the metric it scales on — backlog up → replicas up → backlog drains → replicas down. Under it, project 04's machinery makes churn survivable: claims are visibility-timeout leases (a killed pod's job redelivers), completion acks exactly once (a redelivery racing a slow ack is harmless because completion is idempotent), and a SIGTERM'd worker finishes or relinquishes within the k8s grace period so rolling deploys lose zero jobs.

**In the wild:** KEDA scaling deployments on queue depth is precisely this; SQS-driven ASGs predate it; every video platform autoscales its transcode farm this way.

**You own it when you can explain:**
- [ ] Why CPU% is the *wrong* scaling signal for queue-fed workers (busy ≠ keeping up; the backlog is the truth) and queue-depth-per-worker is right.
- [ ] The control loop's failure modes: lag (scale too slow → backlog balloons), overshoot, flapping — and what clamps/hysteresis do.
- [ ] The causality worth saying out loud: *autoscaling is why leases are mandatory* — the platform kills its own workers by design.
- [ ] The ack-race analysis: worker A's lease expires, B redelivers-and-completes, A's late ack arrives. Walk why nothing corrupts.
- [ ] Graceful drain + PodDisruptionBudget: what each protects during deploys and node drains.

**Depth probes:**
- Scale-up takes 60 s (image pull, schedule). What absorbs the spike meanwhile, and what does that say about queue sizing vs latency SLOs?
- Why must desired-replicas be clamped and monotone-ish in backlog — what does a noisy metric do to the HPA?

**Trap:** demonstrating autoscaling on the happy ramp only. The exam is scale-*down*: pods terminated mid-job with zero lost work — that's where leases, drains, and idempotent completion earn their keep.

---

## 🧠 Card 3 — The edge: single-flight + blocking reload *(V3 · `src/edge.rs`)*

**The problem.** The instant a live playlist references a new partial, *every* viewer wants that exact resource — a synchronized stampede by protocol design. 100k viewers, one just-produced 200 ms partial the edge doesn't have yet: without protection that's 100k simultaneous origin fills of the same bytes, i.e. project 01's thundering herd, rebuilt at video scale every 200 ms, forever.

**The idea.** Two mechanisms, one card. **Single-flight**: the first request for a cold key triggers the origin fill; every concurrent request for the same key *waits on that fill* — origin sees ≤1 request per new object no matter the crowd. **Blocking reload** (project 13's LL-HLS trick, now at the edge): players long-poll the playlist with `_HLS_msn/_HLS_part`, and the edge holds requests until the part exists — thousands of held requests parked on one "part ready" signal. Origin slowness degrades cleanly (fill timeout → gateway error to that viewer) instead of piling connections.

**In the wild:** every CDN's request-coalescing/collapsed-forwarding feature (Varnish request coalescing, nginx `proxy_cache_lock`, Cloudflare tiered cache); LL-HLS at scale is exactly held-request fan-out.

**You own it when you can explain:**
- [ ] Why live video *manufactures* synchronized stampedes (shared playlist clock) where VOD traffic doesn't.
- [ ] Single-flight mechanics: what the followers wait on, what happens when the fill fails (error all waiters? retry once?), and why the answer matters.
- [ ] The edge's two proof-numbers: hit ratio and origin-fill count — "the herd collapsed to one fill" as a metric, not a vibe.
- [ ] How blocking reload composes with single-flight: the held playlist request releases → everyone requests the new partial → single-flight absorbs *that* too.
- [ ] The degradation contract when origin is slow/down: bounded fill timeouts, no unbounded connection pileup.

**Depth probes:**
- Layered edges (origin shield → regional → pop): where does single-flight run at each layer, and what does the origin see?
- What's cacheable for how long? Re-derive the playlist/part/segment TTL ladder from mutability.

**Trap:** sizing the edge for steady-state hit ratio. The killer moment is the *cold key at peak concurrency* — steady state has no stampede; the transition does.

---

## 🧠 Card 4 — Chat at 100k: isolation, shedding, cross-pod fan-out *(V4 · `src/chat.rs`)*

**The problem.** Project 03's hub, pushed to hostile scale: one raided channel becomes a firehose while thousands of small channels stay quiet — and the firehose must not add a millisecond to the quiet rooms. Meanwhile 100k viewers in one room means some *thousands* of them are on bad networks at any instant, and chat runs across many pods, so a message posted on pod A must reach subscribers on pods B..Z exactly once each.

**The idea.** Three compositions of things you already know. **Isolation**: per-channel broadcast domains so fan-out work is O(that channel's subscribers) and channels can't contend. **Shedding**: per-subscriber bounded outboxes with a declared overflow policy — hotel-wifi viewers lag out or drop; the broadcaster never slows (live chat is the canonical "may drop" stream from project 05's two-regimes rule). **The bus**: cross-pod Redis pub/sub with NODE_ID echo-suppression (project 03 V4, verbatim), plus cluster-wide presence aggregation.

**In the wild:** Twitch chat (IRC-descended, massively sharded), Discord's guild sharding, YouTube live chat — all are this architecture with bigger numbers.

**You own it when you can explain:**
- [ ] Why isolation is the *multi-tenant* requirement: the noisy-neighbor failure when channels share queues/locks, and what per-channel domains bound.
- [ ] The shedding argument at 100k: the probability *someone* is slow approaches 1, so slow-consumer handling is the steady state, not the edge case.
- [ ] Why chat classifies as may-drop (project 05's regimes) and what that licenses (lag → drop policy) vs what it doesn't (silent, uncounted loss).
- [ ] Cross-pod delivery with echo suppression, and why presence must aggregate cluster-wide (the local pod sees a fraction of the room).
- [ ] Per-connection inbound rate limiting — one client must not be able to firehose a channel.

**Depth probes:**
- 100k subscribers × 50 msg/s is 5M sends/s on the hot channel. What actually bounds this (message batching per socket? conflation? fan-out sharding)?
- Does the firehose channel deserve its own bus topic — when does the shared bus become the noisy-neighbor surface?

**Trap:** load-testing the hot channel alone. The multi-tenant claim — the *quiet* channel's p99 unchanged while the firehose rages — is the property that actually fails in shared-everything designs.

---

## ⚡ Rapid-fire round

- [ ] Glass-to-glass, end to end: name each hop (capture → RTMP → transcode → package → edge → player buffer) and its latency contribution — and which knob you'd turn first to cut it.
- [ ] Which plane reuses which project (03→chat, 11→packaging, 12→transcode, 13→ingest) and what the glue added that none of them had (lifecycle, scaling, edge, tenancy).
- [ ] "Degrade, don't die" under the spike: what sheds (chat laggards, live-view frames), what must never drop (transcode queue, control-plane state), and why the classification is the design.
- [ ] Stream-key auth on ingest; token-gated vs public playback as a documented tradeoff (hotlinking vs simplicity).
- [ ] Readiness vs liveness probes: what each gates (traffic vs restart) and why conflating them causes restart storms.
- [ ] The boss-fight metrics that matter: glass-to-glass p95, origin fills under stampede, HPA reaction time, chat fan-out p99 on hot *and* quiet channels.

## 🔗 Connects to

- This is the composition exam for projects 03, 11, 12, 13 — every card above names its inheritance.
- The reconcile loop (V1) and the autoscaling loop (V2) are the same desired-vs-actual pattern at two timescales.
- Project 17 does the same "integrate what you built" move for the conferencing stack (09 + 14 + 15).
