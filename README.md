<div align="center">

# 🦀 backend-gauntlet

### Build the infrastructure primitives that power the modern web — in Rust.

Queues, caches, brokers, consensus, gateways, media pipelines.
Scaffolded SPECs, interesting logic left as `todo!()`. No todo apps.

<br>

[![CI](https://github.com/utkarsh5026/backend-gauntlet/actions/workflows/ci.yml/badge.svg)](https://github.com/utkarsh5026/backend-gauntlet/actions/workflows/ci.yml)
[![Site](https://img.shields.io/badge/site-GitHub%20Pages-d4783a)](https://utkarsh5026.github.io/backend-gauntlet/)
![Rust](https://img.shields.io/badge/Rust-stable-000000?logo=rust&logoColor=white)
![Tokio](https://img.shields.io/badge/runtime-Tokio-1a1a2e)
![Axum](https://img.shields.io/badge/web-Axum-0a7e8c)
![License](https://img.shields.io/badge/license-MIT-blue)

</div>

---

## 📊 Progress

<!-- status-dashboard:start -->
<p align="center">
  <img src="assets/status-dashboard.svg?h=a8909e462fa3" alt="backend-gauntlet progress dashboard (make status)" width="100%" />
</p>
<!-- status-dashboard:end -->

<p align="center">
<sub>
<code>make status</code> · <code>make status-readme</code> · refreshed on push to <code>master</code>
</sub>
</p>

---

## 🗺️ Roadmap

Each project is a real infrastructure piece. Verticals = build the core from scratch.
Horizontals = protocols, caching, security, observability — woven into every SPEC.

<details>
<summary><b>🟢 Tier 1 — Foundations</b> · async, I/O, protocols</summary>

| # | Project | What you build |
|:-:|---------|----------------|
| 01 | **URL shortener + analytics** `🚧` | Snowflake IDs, cache-aside, async clicks, API keys |
| 02 | **Distributed rate limiter** | Token bucket + sliding window over gRPC |

</details>

<details>
<summary><b>🟡 Tier 2 — Concurrency & messaging</b></summary>

| # | Project | What you build |
|:-:|---------|----------------|
| 03 | **Real-time pub/sub + presence** | WebSocket fan-out, backpressure |
| 04 | **Distributed job queue** | Durable jobs, retries, DLQ, `SKIP LOCKED` |

</details>

<details>
<summary><b>🟠 Tier 3 — Storage & data</b></summary>

| # | Project | What you build |
|:-:|---------|----------------|
| 05 | **Time-series metrics pipeline** | Ingest → ClickHouse → SSE dashboard |
| 06 | **S3-compatible object store** | Multipart uploads, CAS blobs, streaming |
| 07 | **Distributed cache** | Consistent hashing, LRU/LFU, gossip |

</details>

<details>
<summary><b>🔴 Tier 4 — The hard stuff</b></summary>

| # | Project | What you build |
|:-:|---------|----------------|
| 08 | **Mini message broker** | Append-only log, partitions, consumer groups |
| 09 | **Distributed KV + Raft** | Leader election, replication, snapshots |
| 10 | **API gateway** | Routing, load balancing, circuit breaking, mTLS |

</details>

<details>
<summary><b>🎬 Tier 5 — Multimedia & streaming</b></summary>

| # | Project | What you build |
|:-:|---------|----------------|
| 11 | **VOD streaming** (HLS/DASH) | fMP4 segmenter, manifests, ABR |
| 12 | **Transcoding pipeline** | Chunked parallel transcode + job DAG |
| 13 | **Live ingest** (RTMP → LL-HLS) | RTMP parse → low-latency HLS |
| 14 | **Realtime media transport** | RTP/RTCP, jitter buffer, NACK |
| 15 | **WebRTC SFU** | Selective forwarding, ICE, simulcast |

</details>

<details>
<summary><b>🏆 Tier 6 — Capstones</b></summary>

| # | Project | What you build |
|:-:|---------|----------------|
| 16 | **Live streaming platform** | Ingest → ABR → LL-HLS → chat · k8s |
| 17 | **Global WebRTC conferencing** | Multi-region SFU federation |

</details>

<details>
<summary><b>🌐 Tier 7 — Cross-cutting</b></summary>

| # | Project | What you build |
|:-:|---------|----------------|
| 18 | **Ledger / payments core** | Double-entry, idempotency, webhooks |
| 19 | **BitTorrent client** | Peer wire, DHT, rarest-first |
| 20 | **Full-text search** | Inverted index, BM25, shards |
| 21 | **Workflow engine** | Event-sourced replay, durable timers |
| 22 | **LSM engine + Redis protocol** | WAL, SSTables, compaction, RESP |

</details>

---

## 🚀 Start

```bash
cd projects/01-url-shortener
docker compose up -d
cp .env.example .env
sqlx migrate run
cargo run -p url-shortener
```

```bash
cargo check --workspace
cargo test -p url-shortener
make status          # progress dashboard
make hooks           # once: block commit/push if rustfmt (CI) would fail
make preflight       # optional manual: cargo fmt --check
```

**→** [`projects/01-url-shortener/SPEC.md`](projects/01-url-shortener/SPEC.md)

---

<div align="center">
<sub>Built to learn. One primitive at a time. 🦀</sub>
</div>
