<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 16 — Live Streaming Platform *(Twitch-lite)*

> This is the **capstone** — the marquee project. There's no new primitive to invent here;
> the hard part is *integration*: wiring the pieces you already built (chat fan-out from 03,
> HLS packaging from 11, transcode from 12, RTMP ingest from 13) into one **glass-to-glass**
> pipeline that survives a real crowd. A broadcaster's frame is captured, ingested, transcoded
> into an ABR ladder, packaged as low-latency HLS, delivered through an edge, and painted on a
> thousand viewers' screens — while chat scrolls in real time. Then a streamer goes viral and
> everything that was comfortable at 10 viewers has to hold at 100,000. This is also the one
> place you do **real k8s ops**: the transcode workers autoscale, and that only works if the
> queue signal, the lease semantics, and the drain behavior are right.

## What it does (the easy part)
- `POST /ingest/start` / `POST /ingest/stop` — the webhook an ingest edge calls when a
  broadcaster connects/disconnects with a stream key.
- `GET /live/{stream}/master.m3u8` — the ABR master playlist a player picks a rendition from.
- `GET /live/{stream}/{rendition}/index.m3u8` — a rendition's LL-HLS media playlist (with
  `_HLS_msn`/`_HLS_part` blocking reload).
- `GET /live/{stream}/{rendition}/{segment}` — a segment or partial, byte-range capable.
- `GET /chat/{stream}/ws` — a viewer's WebSocket into the channel's chat + presence.
- `/healthz` `/readyz` `/status` `/metrics` — probes and observability for k8s.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** — observable
> criteria you can check off — and a **Proof**: the test/bench/doc that *demonstrates* it (not
> "I think it works"). The criteria describe *what the system must do*, never *how*; figuring
> out the how is the entire point. A box only flips to ✅ when its Proof exists.

> **A note on scope.** This capstone *reuses* the media internals you already wrote — the fMP4
> segmenter (11), the transcode worker (12), the RTMP parser (13), the pub/sub hub (03). The
> verticals below are the **integration glue** those pieces don't give you: the orchestrator
> that ties them together, the autoscaling that keeps transcode ahead of demand, the edge that
> shields the origin, and the chat fan-out that survives a hot channel. If a vertical tempts
> you to re-solve a lower project, you've drifted — call into that project's idea instead.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Stream control plane / session lifecycle — *the orchestrator*
Something has to know that *"stream `abc123` is live, ingested on node-2, transcoded to a
3-rung ladder, playing at `/live/abc123/…`"* — and keep that true as ingests start and stop.
Build the state machine + registry in `src/control.rs`: a session walks
`Offline → Ingesting → Transcoding → Live → Ended`, and each transition fans work out to the
other planes (enqueue transcode, register the edge route, open the chat channel) and cleans it
up on teardown. Postgres is the source of truth so a control-plane restart *reconciles* live
streams instead of losing them.

**Done when ALL true:**
- [ ] An ingest-start for a valid stream key creates **exactly one** live session and moves it
  off `Offline`; an **unknown key is rejected** before any resource is allocated.
- [ ] Transitions are **idempotent**: the same ingest-start (or stop) webhook delivered twice
  does not create a second session, double-enqueue transcode, or double-count anything.
- [ ] **Illegal transitions are refused** (e.g. `Offline → Live` with no ingest) — the state
  machine never lands in a state its inputs don't justify.
- [ ] Ingest-stop **tears everything down**: transcode work, the edge route, and the chat
  channel for that stream are released, and the session ends — safe to call twice.
- [ ] After a **control-plane restart**, streams that were live are recovered from Postgres
  (reconciled), and sessions whose ingest lease expired are ended — no ghost "live" streams.
- [ ] `max_streams` is **enforced**: past the cap, a new ingest is rejected rather than
  degrading every existing stream.

**Proof:** state-machine unit/property tests (legal-transition matrix + idempotent replay);
an integration test that kills and restarts the control plane and asserts the registry
reconciles from Postgres; `docs/16-design.md` names the transition side-effects and the
lease/reconcile rule.

*Concept to internalize:* orchestration as a state machine — idempotent transitions, a durable
source of truth, and reconciliation (desired vs. actual) as the pattern that makes a
distributed system self-heal instead of drift.

### V2. Autoscaling transcode worker pool — *keep transcode ahead of demand*
Transcoding the ABR ladder is the CPU-heavy, bursty part: one streamer going live can 10× the
work in seconds. The workers run as a k8s Deployment that **autoscales**, and two things make
that safe — both yours to build in `src/workers.rs`. **(1)** The autoscaler signal: HPA can't
see "transcode backlog", so you expose *queue depth per worker* as a metric it scales on.
**(2)** At-least-once leasing under pod churn: a worker *claims* a job under a visibility-timeout
lease, so when HPA scales down (or a node preempts a pod) an in-flight job redelivers instead of
vanishing — but a completed job acks exactly once.

**Done when ALL true:**
- [ ] The control plane entering `Transcoding` **enqueues one job per ladder rung**; nothing on
  the ingest path blocks waiting for a worker.
- [ ] A worker **claims** a job under a lease and no **other** worker gets the same job while the
  lease holds — concurrent claimers never double-process.
- [ ] A worker that **dies mid-job** (lease expires) causes the job to be **redelivered** and
  completed by another worker — at-least-once, nothing silently dropped.
- [ ] A **completed** job is acked exactly once; a redelivery that races a slow ack is harmless
  (no duplicate output committed).
- [ ] The exported **queue-depth / desired-replicas metric tracks backlog**: as backlog rises the
  desired-replica number rises (clamped to `[1, max_replicas]`), and falls as it drains — this
  is the number the HPA scales on.
- [ ] On **graceful shutdown** (SIGTERM within the k8s grace period) a draining worker finishes or
  cleanly relinquishes its current job — a rolling deploy loses no work.

**Proof:** an integration test that claims under concurrency and asserts single-delivery + lease
redelivery on a simulated worker death; a test asserting `desired_replicas` is monotonic in queue
depth and clamped; k8s manifests (`k8s/`) with the HPA wired to the queue-depth metric + a
PodDisruptionBudget; `docs/16-design.md` records the lease duration + drain choice.

*Concept to internalize:* the autoscaling control loop — a custom/external metric closing the
loop between backlog and replicas — and why at-least-once work distribution needs leases, acks,
and idempotent completion to survive the pod churn autoscaling *causes*.

### V3. LL-HLS edge delivery with request coalescing — *shield the origin, cut latency*
Between the packager (origin) and thousands of viewers sits an edge, in `src/edge.rs`. Its job:
serve the *same* freshly-produced bytes to a crowd without melting the origin, at low latency.
Two subtleties. **(1)** Single-flight on a cold segment: the instant the playlist references a
new partial, every viewer asks for it at once — if the edge doesn't have it, **exactly one** fill
goes to origin and the rest wait on that fill (a cache stampede, now on video). **(2)** Blocking
playlist reload: LL-HLS players long-poll the media playlist with `_HLS_msn`/`_HLS_part` — "hold
the request open until media-sequence N part K exists, then return" — which is what pushes
glass-to-glass toward ~2s instead of ~10s.

**Done when ALL true:**
- [ ] A cache **hit** serves a segment/partial from the edge and **does not touch origin**.
- [ ] With **≥1,000 viewers** racing for the **same just-produced** partial that the edge doesn't
  have yet, origin sees **≤1 fill** — not one per viewer (single-flight).
- [ ] A **blocking media-playlist reload** for a not-yet-existing `msn`/`part` **holds the request
  open** until it's produced (or a deadline), then returns the updated playlist — it never
  busy-polls and never returns a playlist stale past the requested cursor.
- [ ] Segment responses honor **HTTP `Range`** so a player can seek / resume mid-segment.
- [ ] Origin **being slow or down degrades cleanly**: a fill timeout returns a gateway error to
  that viewer (and doesn't pile up), rather than hanging the connection or the edge.
- [ ] **Edge hit ratio and origin-fill count** are observable — you can *show* the herd collapsed
  to one fill, not assert it from vibes.

**Proof:** an integration test firing ≥1k concurrent requests at one cold partial and asserting
the origin-fill counter reads ≤1; a test for blocking reload (a reload returns only once the part
exists) and for `Range`; `bench/` numbers for edge hit ratio under a viewer fan-out; the fill
count in `docs/16-benchmarks.md`.

*Concept to internalize:* single-flight / request coalescing as origin protection (the URL
shortener's thundering herd, one tier up and on segments), and how LL-HLS trades a held-open
request for latency.

### V4. Chat & presence fan-out at scale — *survive the hot channel*
Every live channel has a chat, and a viral stream puts 100k people in one room. This is project
03's WebSocket fan-out, now multi-tenant and pushed hard, in `src/chat.rs`. The failure modes are
about **isolation and backpressure**: one broadcast channel per stream (a firehose channel must
not stall a quiet one); each subscriber has a bounded outbox and an explicit overflow policy (a
viewer on hotel wifi is dropped, never allowed to back-pressure the broadcaster); presence counts
per channel; and because the platform runs as many pods, a message on one pod reaches the others
over a Redis bus with each pod dropping its own echoes.

**Done when ALL true:**
- [ ] A message posted in channel A reaches **A's subscribers only** — no cross-channel leakage.
- [ ] A **slow subscriber** that can't keep up is handled by a **declared overflow policy** (lag →
  drop / disconnect) and **never** lets its outbox grow unbounded or slow the broadcaster.
- [ ] Fan-out to a channel is **O(subscribers) work isolated to that channel**: a firehose channel
  does not measurably delay delivery on a quiet one.
- [ ] **Presence** (viewer count) per channel is reported and correct across join/leave, and —
  across pods — reflects the whole cluster, not just the local pod.
- [ ] A message published on **one pod reaches subscribers on other pods** via the bus, and a pod
  **drops its own echo** (no message delivered twice to a local subscriber).
- [ ] Chat **connections and slow-consumer drops are observable** as metrics.

**Proof:** an integration test with a deliberately slow subscriber asserting the broadcaster's
send latency is unaffected and the slow one is dropped per policy; a two-node test proving
cross-pod delivery + echo suppression; a fan-out bench at high subscriber count in
`docs/16-benchmarks.md`.

*Concept to internalize:* multi-tenant fan-out isolation and slow-consumer backpressure — why a
bounded outbox with a drop policy is the only safe answer, and how a pub/sub bus makes a
horizontally-scaled chat behave like one room.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] **Ingest interop:** the ingest webhook accepts a real ingest edge's start/stop (RTMP from
  project 13, or a WebRTC ingest) and rejects a bad/malformed body with a `4xx`. *(Proof: ingest
  contract test.)*
- [ ] **HLS correctness:** the master + media playlists validate against an HLS checker / real
  player, and segments serve with correct `Content-Type` + working `Range`. *(Proof: playlist
  validation + a player playing the stream, noted in `docs/16-design.md`.)*
- [ ] **LL-HLS low latency:** blocking reload is used (not client polling), and the glass-to-glass
  target below is met. *(Proof: the boss fight.)*
- [ ] **Graceful shutdown:** SIGTERM stops admitting new ingests, drains in-flight HTTP + chat
  sockets, and lets in-flight transcodes finish or relinquish within the k8s grace period.

### Caching / delivery
- [ ] Edge single-flight (V3) collapses a herd to ≤1 origin fill.
- [ ] Playlists carry sane `Cache-Control` (short/near-zero for live media playlists, longer for
  immutable segments) — a stale playlist never pins an old media sequence. *(Proof: header test
  + note in `docs/16-design.md`.)*

### Security
- [ ] **Stream-key auth on ingest:** an ingest-start without a valid, registered key is rejected
  before any session/resource is created, and the key never appears in logs or error bodies.
  *(Proof: reject test + a log-scrub check.)*
- [ ] **Signed / token-gated playback** is a documented decision: `docs/16-design.md` states whether
  playback URLs are public or token-signed and the tradeoff (hotlinking vs. simplicity). *(Proof:
  design doc; if signed, a token-reject test.)*
- [ ] **Chat abuse protection:** per-connection rate limiting on inbound chat messages, so one
  client can't flood a channel. *(Proof: rate-limit test.)*
- [ ] **Input validation:** stream keys, renditions, and segment names in URLs are validated against
  an allowlist/shape — no path traversal into the origin/edge store. *(Proof: traversal-reject test.)*

### Observability
- [ ] `tracing` span per request with a request id (via `common-telemetry`).
- [ ] **Glass-to-glass latency is measured** end-to-end (capture ts → playable at edge) and exported
  as a histogram — the number the boss fight judges. *(Proof: `live_glass_to_glass_ms` in `/metrics`.)*
- [ ] Per-plane metrics at `/metrics`: streams live, **transcode queue depth + desired replicas**,
  edge hit ratio + origin fills, chat connections + slow drops. *(Proof: `/metrics` render test.)*

### Ship it (this is the k8s project)
- [ ] **k8s manifests** in `k8s/`: Deployments for the API + transcode workers, Services, and the
  three stateful deps (or their managed equivalents).
- [ ] **HPA** on the transcode worker Deployment, scaling on the **queue-depth metric** (V2) — a
  backlog spike scales workers up and drains scale them down. *(Proof: the boss fight + manifest.)*
- [ ] **Readiness/liveness probes** wired to `/readyz` / `/healthz`, and a **PodDisruptionBudget**
  on the workers so a drain doesn't evict everything at once.
- [ ] Reproducible: `docs/16-design.md` documents the deploy (local `kind`/`minikube` is fine).

---

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load test lives in `bench/`, the numbers in
   `docs/16-benchmarks.md`.
3. `docs/16-design.md` records the decisions the SPEC grades: the **state-machine + reconcile
   rule** (V1), the **transcode lease + autoscale signal** (V2), the **single-flight + blocking-
   reload strategy** (V3), the **chat overflow policy + cross-node bus** (V4), and the **playback
   auth** call.
4. `cargo clippy --workspace -- -D warnings` and `cargo test -p live-platform` are green; no
   `todo!()` remains on a checked path.

## 🐉 Boss fight — The Viral Spike

> A mid-tier streamer gets raided. In **30 seconds** the channel goes from 200 viewers to
> **100,000**, and the raid brings a chat firehose with it. Everything that was fine a minute ago
> is now a stampede: every new viewer hits the edge for the same cold partial, the transcode
> backlog balloons and the HPA has to spin workers *now*, and 100k people are all typing "POG" at
> once. Glass-to-glass must stay low, the origin must not fall over, and chat must not melt — all
> while pods are being added under you. Beat the spike without dropping the stream.

**Arena:** `bench/` load test (`k6` / `oha` for HTTP playback + a WS load tool for chat) against a
**release build** deployed to a local k8s (`kind`/`minikube`) with Postgres + Redis + NATS up and
the HPA active. Drive a viewer ramp (200 → 100k over 30s) on one hot stream while transcode load
scales, plus a cold-partial stampede scenario and a chat-firehose scenario.

**The boss falls when ALL true:**
- [ ] **Glass-to-glass p95 ≤ 3s** on LL-HLS playback sustained through the ramp (and the stream
  never stalls / rebuffers into failure).
- [ ] Under the **cold-partial stampede** (≥1,000 concurrent viewers for the same just-produced
  partial), origin sees **≤1 fill** and **edge hit ratio ≥ 99%** at steady state.
- [ ] The **HPA scales transcode workers up within ~60s** of the backlog spike and the queue
  **drains back down** (no unbounded backlog); a worker pod killed mid-transcode loses **0 jobs**.
- [ ] **Chat p99 fan-out latency ≤ 500ms** to 100k subscribers on the hot channel, slow consumers
  shed per policy, and a quiet channel's latency is **unaffected** by the firehose.
- [ ] Throughout, **p99 API latency stays bounded** and no dependency (origin, DB, bus) is driven to
  failure — degrade, don't die.

**Proof:** methodology + numbers in `docs/16-benchmarks.md` (cluster shape noted, HPA events and
the queue-depth/replica timeline captured, commands reproducible via `bench/`).

## Suggested order of attack
1. **Get one stream through end-to-end, single-node, no scale.** Ingest webhook → control plane
   marks it live → transcode one rung → package → edge serves it → a player plays it. Boring path first.
2. **V1** — make the lifecycle a real state machine with idempotent transitions and Postgres-backed
   reconciliation. This is the spine everything else hangs off.
3. **V3** — put the edge in front of the packager: blocking reload + single-flight. Now a crowd is safe.
4. **V2** — move transcode onto the durable queue with leases, then expose the queue-depth signal and
   wire the HPA. Now demand can scale.
5. **V4** — bring chat over from project 03, shard per channel, add the bus + presence.
6. **Ship it** — k8s manifests, HPA, probes, PDB. Then benchmark the Viral Spike, document, tune.

## Run the dependencies
```bash
docker compose up -d        # postgres + redis + nats
cp .env.example .env        # then fill in values
sqlx migrate run            # apply migrations (install: cargo install sqlx-cli)
cargo run -p live-platform
```
