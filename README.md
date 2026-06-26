<div align="center">

# 🦀 backend-gauntlet

### Building the infrastructure primitives that power the modern web — in Rust.

A hands-on gauntlet of **pure backend / systems projects**, going from *easy → hard*.
No todo apps. The goal is to build the primitives the modern world actually runs on —
**queues, caches, brokers, consensus, gateways** — and get a real grip on Rust, scale,
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

</td>
<td width="50%" valign="top">

### ➡️ Horizontal — *fundamentals everywhere*

The backend skills that show up in **every** project, woven into each SPEC — never
bolted on at the end:

- 🌐 **Protocols** — HTTP/1.1 & 2, WebSockets, gRPC, SSE, raw TCP
- ⚡ **Caching** — cache-aside, write-through, stampede protection
- 🔒 **Security** — auth, TLS/mTLS, input validation, abuse protection
- 📊 **Observability** — `tracing`, structured logs, metrics

</td>
</tr>
</table>

---

## 🗺️ The roadmap

> Difficulty: 🟢 foundational · 🟡 intermediate · 🟠 advanced · 🔴 hard

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

**→ Start here: [`projects/01-url-shortener/SPEC.md`](projects/01-url-shortener/SPEC.md)**

---

<div align="center">
<sub>Built to learn. One primitive at a time. 🦀</sub>
</div>
