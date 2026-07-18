# Job Queue — live dashboard

A **pure-client** dashboard for project 04. It watches the queue work by scraping
the metrics and endpoints the Rust backend **already** serves — there are **no
backend changes** and no new endpoints. The queue internals stay yours to build.

React + TypeScript + Tailwind + shadcn/ui, dark by default (repo web conventions).

## What it shows

- **Stat tiles** — ready (backlog), running (in-flight), completed, DLQ depth, lag
  (oldest ready age), and live throughput, each with a sparkline.
- **Queue depth** — ready + running stacked over time. Flood the queue and watch
  the backlog spike, then drain as the worker pool chews through it.
- **Throughput** — enqueue rate vs. completion rate. When completion sits below
  enqueue the backlog grows; when it climbs above, the queue drains.
- **Signals** — retried / dead-lettered / leases-reaped / empty-claims counters and
  execution p50·p99 (the *why* behind the headline gauges).
- **Load generator** — flood / burst / poison (→ DLQ) / delayed quick-actions plus a
  custom enqueue form, so you can make the queue do something and watch it react.
- **Dead-letter queue** — inspect poison jobs and requeue them.

## Data source

Everything is read from what the backend exposes (paths are relative; Vite's dev
proxy forwards them to the backend — no CORS):

| Endpoint | Used for |
|---|---|
| `GET /metrics` | all gauges + counters + histograms (the live feed) |
| `POST /jobs` | enqueue / load generation (auth: `Bearer ENQUEUE_TOKEN`) |
| `GET /dlq` | dead-letter table |
| `POST /job/{id}/requeue` | requeue a dead job (auth) |
| `GET /healthz` | liveness |

The depth gauges are published by the backend's gauge sampler, which — like the
worker pool — only runs with **`RUN_WORKERS=true`**. Start the server that way or
the dashboard will warn you that jobs are piling up in `ready` and never draining.
If auth is enabled, paste the `ENQUEUE_TOKEN` into the load-generator panel (it is
kept only in your browser's `localStorage`).

## Run it

```bash
# One-window dev stack (postgres + cargo run + this dashboard):
make dev NN=04                 # from the repo root

# …or standalone (backend already running on :8080):
bun install
bun run dev                    # http://localhost:5273

# Point at a different backend instance (e.g. a 2nd worker process):
JOBQUEUE_URL=http://localhost:8081 bun run dev
```

Bun only (no npm/pnpm), per repo convention.
