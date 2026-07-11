<div align="center">

# 🦀 backend-gauntlet

### Building the infrastructure primitives that power the modern web — in Rust.

A hands-on gauntlet of **pure backend / systems projects**, going from *easy → hard*.
No todo apps. The goal is to build the primitives the modern world actually runs on —
**queues, caches, brokers, consensus, gateways, media pipelines** — and get a real grip on Rust, scale,
and backend fundamentals along the way.

<br>

[![CI](https://github.com/utkarsh5026/backend-gauntlet/actions/workflows/ci.yml/badge.svg)](https://github.com/utkarsh5026/backend-gauntlet/actions/workflows/ci.yml)
![Rust](https://img.shields.io/badge/Rust-stable-000000?logo=rust&logoColor=white)
![Tokio](https://img.shields.io/badge/runtime-Tokio-1a1a2e)
![Axum](https://img.shields.io/badge/web-Axum-0a7e8c)
![License](https://img.shields.io/badge/license-MIT-blue)
![Status](https://img.shields.io/badge/status-in%20progress-yellow)

</div>

---

## 🎯 What this is

A learning monorepo where each project **is** a piece of real infrastructure — the kind
companies pay for. Every project is scaffolded as a [`SPEC.md`](projects/01-url-shortener/SPEC.md)
"ticket" with the wiring done and the **interesting logic left to implement from scratch**.

> 💡 The point isn't to *use* Kafka or Redis — it's to understand them well enough to
> **rebuild their core ideas yourself**.

---

## 🧭 Philosophy: two axes

Every project is graded on **two axes** at once:

<table>
<tr>
<td width="50%" valign="top">

### ⬆️ Vertical — *the primitive's internals*

Each project teaches one piece of infrastructure. Where you'd normally `cargo add` a
crate, the SPEC tells you to **build that part yourself** so you learn how it works:

- 🆔 a Snowflake distributed ID generator
- 🪣 a token-bucket / sliding-window rate limiter
- 🗂️ a hand-built LRU/LFU cache
- 🪵 a segmented append-only log
- 🗳️ a Raft replication log
- 🎞️ an fMP4 / HLS media segmenter
- 🌲 an LSM-tree storage engine
- 🔎 a BM25 inverted index

</td>
<td width="50%" valign="top">

### ➡️ Horizontal — *fundamentals everywhere*

The backend skills that show up in **every** project, woven into each SPEC — never
bolted on at the end:

- 🌐 **Protocols** — HTTP/1.1 & 2, WebSockets, gRPC, SSE, raw TCP, RTMP, RTP/WebRTC
- ⚡ **Caching** — cache-aside, write-through, stampede protection
- 🔒 **Security** — auth, TLS/mTLS, input validation, abuse protection
- 📊 **Observability** — `tracing`, structured logs, metrics
- 🚢 **Ship it** — `Dockerfile` per project, health/readiness probes, graceful shutdown

</td>
</tr>
</table>

---

## 🗺️ The roadmap

> Difficulty: 🟢 foundational · 🟡 intermediate · 🟠 advanced · 🔴 hard · 🏆 capstone

### 🟢 Tier 1 — Foundations · *async, I/O, protocols*

| # | Project | What you build | Key tech |
|:-:|---------|----------------|----------|
| 01 | **URL shortener + analytics** `🚧 in progress` | Snowflake/base62 IDs, cache-aside w/ stampede protection, async click ingestion, API-key auth | `HTTP` `caching` `security` |
| 02 | **Distributed rate limiter** | Token bucket + sliding window, Redis + Lua atomics, served over gRPC | `gRPC/HTTP2` `abuse-protection` |

### 🟡 Tier 2 — Concurrency & messaging

| # | Project | What you build | Key tech |
|:-:|---------|----------------|----------|
| 03 | **Real-time pub/sub + presence** | WebSocket fan-out, backpressure, multi-node via Redis | `WebSockets` `pub/sub` |
| 04 | **Distributed job queue** | Durable jobs, at-least-once delivery, retries, DLQ, `SKIP LOCKED` | `Postgres` `messaging` |

### 🟠 Tier 3 — Storage & data systems

| # | Project | What you build | Key tech |
|:-:|---------|----------------|----------|
| 05 | **Time-series metrics pipeline** | Kafka/NATS ingest, ClickHouse storage, rollups, SSE dashboard | `Kafka` `ClickHouse` `SSE` |
| 06 | **S3-compatible object store** | Chunked/multipart uploads, content-addressed blobs, streaming bodies | `streaming` `storage` |
| 07 | **Distributed cache** | Consistent hashing, virtual nodes, hand-built LRU/LFU, gossip membership | `consistent-hashing` `gossip` |

### 🔴 Tier 4 — The hard stuff

| # | Project | What you build | Key tech |
|:-:|---------|----------------|----------|
| 08 | **Mini message broker** *(Kafka-lite)* | Segmented append-only log, partitions, consumer groups | `log-storage` `partitions` |
| 09 | **Distributed KV store w/ Raft** | Leader election, log replication, snapshots | `openraft` `consensus` |
| 10 | **API gateway / L7 reverse proxy** | Routing, load balancing, circuit breaking, mTLS | `tower` `hyper` `mTLS` |

### 🎬 Tier 5 — Multimedia & streaming · *the bytes behind video & live*

| # | Project | What you build | Key tech |
|:-:|---------|----------------|----------|
| 11 | **VOD streaming server** *(HLS/DASH)* | Hand-written fMP4 segmenter + m3u8/mpd manifest generator, adaptive bitrate, HTTP byte-range serving | `HLS/DASH` `ABR` `ISO-BMFF` |
| 12 | **Distributed transcoding pipeline** | Keyframe-aligned chunking, parallel transcode workers, stitch + remux into a job DAG | `ffmpeg` `job-DAG` `codecs` |
| 13 | **Live ingest server** *(RTMP → LL-HLS)* | RTMP handshake / AMF / chunk-stream parser, repackage to low-latency HLS | `RTMP` `live` `LL-HLS` |
| 14 | **Real-time media transport** *(RTP/RTCP)* | RTP packetization, jitter buffer, NACK / retransmit, congestion control over UDP | `RTP/RTCP` `UDP` `jitter-buffer` |
| 15 | **WebRTC SFU** | Selective RTP forwarding, ICE/STUN, simulcast layer selection, bandwidth estimation | `WebRTC` `SFU` `ICE` |

> 🧵 **11 → 12 → 13** is the VOD-to-live spine; **14 → 15** are the hard real-time pair
> (lossy UDP, no HTTP to hide behind). Each *vertical* is the part you'd normally reach
> for `ffmpeg` / `GStreamer` / `webrtc-rs` to do — here you build the core yourself.

### 🏆 Tier 6 — Capstone · *where it all pays off*

| # | Project | What you build | Key tech |
|:-:|---------|----------------|----------|
| 16 | **Live streaming platform** *(Twitch-lite)* | End-to-end glass-to-glass: RTMP/WebRTC ingest → ABR transcode ladder → LL-HLS packaging → edge delivery → realtime chat & presence — **deployed to k8s** with autoscaling transcode workers | composes `03` `11` `12` `13` · `k8s/HPA` |
| 17 | **Global WebRTC conferencing** *(cascaded SFU)* | Multi-region SFU federation, room placement, simulcast routing, server-side recording | composes `14` `15` + `consensus` |

> 🏁 No new primitive here — the capstones are about **integration**: wiring the pieces
> you built into one system that survives real traffic, with backpressure and failure
> handling end to end. **#16** is the marquee (the full live pipeline at scale) and the
> one place you do **real k8s ops** — autoscaling transcode workers is where HPA,
> readiness probes, and pod disruption budgets actually earn their keep. **#17** is the
> real-time stretch goal that also leans on your Raft work (#09) for placement.

### 🌐 Tier 7 — Cross-cutting gauntlet · *networking + storage + caching + systems eng, all at once*

> 🎛️ Every project here deliberately spans **all four pillars** — networking, database/storage
> internals, caching, and systems engineering — instead of leaning on one. Difficulty 🟡 → 🔴.

| # | Project | What you build | Key tech |
|:-:|---------|----------------|----------|
| 18 | **Ledger / payments core** *(Stripe-lite)* | Double-entry ledger, serializable transactions & balance invariants under concurrency, idempotency-key cache, signed webhooks w/ retries | `transactions` `idempotency` `webhooks` |
| 19 | **BitTorrent client + seeder** | Peer wire protocol over raw TCP, UDP tracker + Kademlia DHT, piece verification, rarest-first scheduling, choke algorithm | `raw-TCP` `UDP` `DHT` `p2p` |
| 20 | **Full-text search engine** *(Elasticsearch-lite)* | On-disk inverted index, tokenization, BM25 ranking, segment merging, query cache, scatter-gather shard fan-out | `inverted-index` `BM25` `mmap` |
| 21 | **Workflow engine** *(Temporal-lite)* | Event-sourced history log, deterministic replay, durable timers, gRPC worker dispatch, sticky workflow-state caches | `event-sourcing` `replay` `gRPC` |
| 22 | **LSM storage engine + Redis-compatible server** | WAL, memtables, SSTables, compaction, bloom filters, hand-built block cache — served over RESP so real `redis-cli` connects | `LSM` `RESP` `raw-TCP` `fsync` |

> 🔩 **#22 is the keystone**: the engine you build here is exactly what the message broker
> (**#08**) and Raft KV (**#09**) want underneath, and its RESP front-end makes your
> distributed cache (**#07**) speak a protocol real clients already know. **#18** is where
> isolation levels stop being trivia — bugs cost money. **#19** is the purest protocol
> workout on the board.

### 🧊 Tier 8 — Cloud-native internals · *optional · rebuild the tools themselves*

> ⏸️ **Parked, not scheduled.** The streaming arm (Tiers 5–6) comes first. This tier is
> here so the itch is captured — pick it up only if you want to rebuild the machinery
> *under* Docker/k8s rather than just operate it.

| # | Project | What you'd build | Key tech |
|:-:|---------|------------------|----------|
| — | **Container runtime** *(docker-lite)* | Linux namespaces + cgroups v2 + overlayfs + `pivot_root` — i.e. what `docker run` actually does | `namespaces` `cgroups` `OCI` |
| — | **Mini orchestrator** *(k8s-lite)* | Reconciliation loop (desired vs. actual), bin-packing scheduler, health checks, rolling deploys — control-plane state on your **Raft KV (#09)** | `reconciliation` `scheduling` |

> 🧩 A service-mesh sidecar would be the third, but it mostly overlaps the **API gateway
> (#10)** — folded in there rather than duplicated.

---

## 🛠️ Tech stack

![Rust](https://img.shields.io/badge/Rust-000000?logo=rust&logoColor=white)
![Tokio](https://img.shields.io/badge/Tokio-1a1a2e?logoColor=white)
![Axum](https://img.shields.io/badge/Axum-0a7e8c)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)
![gRPC](https://img.shields.io/badge/gRPC-244c5a?logo=grpc&logoColor=white)
![Kafka](https://img.shields.io/badge/Kafka-231F20?logo=apachekafka&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white)

---

## 📂 Repository layout

```
backend-gauntlet/
├── crates/                  🔧 shared, fully-implemented helpers (reused everywhere)
│   ├── common-telemetry/       tracing / structured logging setup
│   └── common-config/          env + secrets loading
├── projects/
│   └── NN-name/             📦 one project each
│       ├── SPEC.md             the "ticket" — challenges, no spoilers
│       ├── src/                wiring done, logic left as TODO
│       ├── migrations/         SQL migrations
│       └── docker-compose.yml  its dependencies
├── docs/                    📝 architecture notes + benchmark results
└── .github/                 ⚙️ CI + Dependabot
```

---

## 🚀 Getting started

```bash
# 1. Pick a project and start its dependencies
cd projects/01-url-shortener
docker compose up -d            # postgres + redis

# 2. Configure
cp .env.example .env            # then fill in values

# 3. Apply migrations & run
sqlx migrate run                # cargo install sqlx-cli
cargo run -p url-shortener
```

Workspace-wide commands:

```bash
cargo check --workspace                       # fast type-check everything
cargo clippy --workspace -- -D warnings       # lint
cargo fmt --all                               # format
cargo test --workspace                        # test
```

> 🧩 `todo!()` bodies **panic at runtime by design** — they *are* your worklist.
> A clean build with only dead-code warnings is the expected scaffold state.

---

## 🧗 How to work through it

Each [`SPEC.md`](projects/01-url-shortener/SPEC.md) reads like a self-assigned ticket:
requirements + the scaling challenges to solve, **without spoiling the solution**. The
`src/` is scaffolded — wiring is done, the interesting logic is yours to write.

Every project also carries cross-cutting **"scale skills"**:

- 📈 Observability from day one (`tracing`, structured logs, metrics)
- 🏎️ A `bench/` with documented throughput numbers (before/after the scaling fix)
- 🛑 Graceful shutdown, backpressure, connection pooling, timeouts/deadlines
- 🐳 `docker-compose.yml` for dependencies

### 🎮 …and it's a game

- 🐉 **Boss fights** — every SPEC's benchmark is staged as a named boss with numeric
  targets (*"The Thundering Herd: 1,000 concurrent requests for the same cold key
  reach Postgres as ≤ 1 query"*). You don't finish a project — you **defeat** it.
- 🏆 **Trophies** — `make trophies` shows the trophy case. Achievements unlock
  themselves from the code, the SPECs, and the git log: first vertical (🩸 First
  Blood), commit streaks (🔥), reviving a dormant project (🧟 Necromancer)…
- ⚔️ **Quests** — `/quest 02 V1` (in Claude Code) runs one vertical as a guided
  session: Socratic concept check → design sketch on a shared whiteboard → **failing
  acceptance tests written up front** from the Done-when criteria (black-box, so
  nothing is spoiled) → you implement while a health bar fills (`🟩🟩🟩⬜⬜ 3/5`) →
  checkboxes flip with their Proofs when everything's green.
- 🚨 **Incident drills** — `/incident 01` secretly breaks a running project and
  pages you with *symptoms only*. Diagnose it with the tracing and metrics you
  built, then write the blameless postmortem to `docs/incidents.md`.

**→ Start here: [`projects/01-url-shortener/SPEC.md`](projects/01-url-shortener/SPEC.md)**

---

<div align="center">
<sub>Built to learn. One primitive at a time. 🦀</sub>
</div>
