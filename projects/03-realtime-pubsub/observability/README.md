# Observability stack — Prometheus + Grafana

Playground observability for the pub/sub server: Prometheus scrapes the app's
`/metrics`, Grafana graphs it. This is scaffolding — **not** part of the pub/sub
core (V1–V4), the same tier as the `/admin` roster. Both run as Docker services
in this project's [`docker-compose.yml`](../docker-compose.yml).

## Run it

```bash
# from projects/03-realtime-pubsub/
make obs-up                    # start Prometheus + Grafana (prints the URLs)
cargo run -p realtime-pubsub   # …and the app on :8080 (or `make run` / `make dev`)
```

`make obs-up` wraps `docker compose up -d prometheus grafana`; use the raw
command directly if you prefer.

The app must be running for Prometheus to have something to scrape — it's polled
on the **host** (`host.docker.internal:8080`), not inside the compose network,
because you run it with `cargo run`, not in a container.

| Service | URL | Notes |
|---|---|---|
| **Grafana** | http://localhost:3003 | Dashboard **Realtime Pub/Sub — Observability** is auto-loaded. Anonymous admin — no login. |
| **Prometheus** | http://localhost:9003 | Check scrape state at `/targets` (the app should read `up`). |

Ports follow the repo convention (conventional port, last two digits → project
`03`): Grafana `3000→3003`, Prometheus `9090→9003`.

Stop just these two without touching Postgres/Redis:

```bash
make obs-down                  # = docker compose stop prometheus grafana
```

## Expected: panels are empty until you wire the metrics

Prometheus scrapes fine and Grafana is provisioned, but the dashboard starts
blank. That's not a bug — a Prometheus series only exists once the app has
**recorded** it at least once, and most metric call sites are still TODO (the
Observability item on the SPEC's horizontal checklist). See
[`src/metrics.rs`](../src/metrics.rs) for the "what's wired vs. TODO" list; each
panel lights up as you add its increment. `messages_dropped_total` is already
wired, so it's your first live line — drive it with a backpressure load test.

## Multi-node (V4)

Running two nodes? Start a second on another port
(`PORT=8081 NODE_ID=node-b cargo run -p realtime-pubsub`) and uncomment the
`node-b` target in [`prometheus.yml`](./prometheus.yml) — both nodes then scrape
into the same dashboard, so you can watch a publish on A fan out to a subscriber
on B.

## Files

| File | Role |
|---|---|
| `prometheus.yml` | Scrape config (5s interval → `rate()` works). |
| `grafana/provisioning/datasources/prometheus.yml` | Auto-wires the Prometheus datasource (`uid: prometheus`). |
| `grafana/provisioning/dashboards/dashboards.yml` | Tells Grafana to load any JSON in `dashboards/`. |
| `grafana/dashboards/realtime-pubsub.json` | The dashboard: 4 stat tiles + 4 time-series over the `realtime_pubsub_*` series. |
