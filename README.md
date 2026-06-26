# backend-gauntlet

A progression of **pure backend / infrastructure projects in Rust**, going from easy to hard.
The goal is not to build apps — it's to build the *primitives* the modern world runs on
(queues, caches, brokers, consensus, gateways) and, along the way, get a real grip on
Rust, scale, and backend fundamentals.

## Philosophy: two axes

Every project is graded on **two axes**:

**1. Vertical — the scale primitive + its internals.**
Each project teaches one piece of infrastructure. Where you'd normally `cargo add` a crate,
the SPEC will instead tell you to *build that part yourself* so you learn how it actually works
(e.g. implement a token-bucket rate limiter, a Snowflake ID generator, an LRU cache, a Raft log).

**2. Horizontal — the backend fundamentals that show up everywhere.**
Protocols (HTTP/1.1, HTTP/2, WebSockets, gRPC, SSE, raw TCP), caching strategies,
security (auth, TLS, input validation, abuse protection), and observability
(`tracing` + metrics) are woven into *every* project's SPEC, not bolted on at the end.

## The roadmap

### Tier 1 — Foundations (async, I/O, protocols)
- **01 · URL shortener + analytics** — base62/Snowflake IDs, cache-aside with stampede
  protection, async click ingestion, API-key auth. *(HTTP, caching, security)*
- **02 · Distributed rate limiter** — token bucket + sliding window, Redis + Lua atomics,
  exposed over gRPC. *(gRPC/HTTP2, abuse protection)*

### Tier 2 — Concurrency & messaging
- **03 · Real-time pub/sub + presence** — WebSockets, fan-out, backpressure, multi-node via Redis.
- **04 · Distributed job queue** — durable jobs, at-least-once delivery, retries, DLQ, `SKIP LOCKED`.

### Tier 3 — Storage & data systems
- **05 · Time-series metrics pipeline** — Kafka/NATS ingest, ClickHouse storage, rollups, SSE dashboard.
- **06 · S3-compatible object store** — chunked/multipart uploads, content-addressed blobs, streaming bodies.
- **07 · Distributed cache** — consistent hashing, virtual nodes, hand-built LRU/LFU, gossip membership.

### Tier 4 — The hard stuff
- **08 · Mini message broker (Kafka-lite)** — segmented append-only log, partitions, consumer groups.
- **09 · Distributed KV store with Raft** — leader election, log replication, snapshots (`openraft`).
- **10 · API gateway / L7 reverse proxy** — routing, load balancing, circuit breaking, mTLS.

## Layout

```
crates/         shared, fully-implemented helpers (telemetry, config) reused everywhere
projects/NN-*/  one project each; SPEC.md is the "ticket", src/ is scaffolded with TODOs
docs/           architecture notes + benchmark results per project
```

## Cross-cutting "scale skills" (every project)
- Observability from day one (`tracing`, structured logs, metrics)
- A `bench/` with documented throughput numbers (before/after the scaling fix)
- Graceful shutdown, backpressure, connection pooling, timeouts/deadlines
- `docker-compose.yml` for dependencies

## How to work through this
Each project's `SPEC.md` reads like a self-assigned ticket: requirements + the scaling
challenges to solve, **without spoiling the solution**. The `src/` is scaffolded — wiring is
done, but the interesting logic is left as `TODO`s for you to implement.

Start with `projects/01-url-shortener/SPEC.md`.
