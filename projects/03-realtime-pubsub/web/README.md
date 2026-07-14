# Realtime Pub/Sub — Playground

A presence/chat playground for the project-03 pub/sub server. Testing WebSockets
from a terminal (`websocat`, one socket at a time) makes fan-out and backpressure
impossible to *see*. This opens **many sockets on one page** so the interesting
behaviour is visible at a glance.

Stack (per the repo's frontend convention): **Bun · React + TypeScript · Vite ·
Tailwind v4 · shadcn/ui**, dark theme.

## Run

```bash
# 1. start the backend (from projects/03-realtime-pubsub/) on port 8080
cargo run -p realtime-pubsub

# 2. start this playground (from projects/03-realtime-pubsub/web/)
bun install
bun run dev                        # http://localhost:5173
```

Vite proxies `/ws` and `/healthz` to `http://localhost:8080` with the WebSocket
upgrade forwarded (`ws: true`), so the browser stays same-origin — **no CORS layer
needed on the Rust backend**. Point at another node (e.g. the second instance in a
two-node V4 run) with:

```bash
PUBSUB_URL=http://localhost:8081 bun run dev
```

You can also override the endpoint per-tab in the UI (top bar) — set it to an
absolute `ws://host:port/ws` to bypass the proxy entirely.

## What it does

Each **client card** is one real WebSocket connection. Add/remove them, connect
all, subscribe each to topics, and publish — all live.

- **Fan-out, visible** — subscribe several clients to one topic, then hit a room's
  ▶ *ping* (or run the Firehose): one publish lights up every subscriber at once.
  The **Fan-out** tile shows `deliverRate` and the amplification factor
  (`delivered/s ÷ published/s ≈ subscribers`).
- **Firehose / load** — publish at a chosen rate (1–2000/s) through one client and
  watch every subscriber's `rate/s` track the source. This is how you push the
  server hard enough to exercise V2.
- **Backpressure, visible** — every published payload carries a per-sender `seq`.
  If the server sheds messages under its overflow policy (`OVERFLOW_POLICY`,
  `OUTBOX_CAPACITY`), a subscriber sees **holes in the sequence**, counted as
  **drops** per client and in the global *Dropped* tile. That counter is the
  client-side mirror of the server's drop metric — the V2 payoff.
- **Latency** — payloads also carry `ts`; since every card shares the page's clock,
  `now − ts` is a true end-to-end delivery latency (shown per message and averaged
  per client).
- **Presence** — the Rooms panel renders `presence` frames per topic. It stays
  empty until V3 is wired; then joins/leaves show up live. Test abrupt drop:
  *disconnect* a client (or close the tab) and watch its membership disappear.

## Notes & honest limits

- **The backend ships with `todo!()` bodies (V1–V4).** In the fresh scaffold the
  V1 hub is entirely unimplemented, so the first `subscribe` *or* `publish` panics
  in `src/hub.rs` and drops the socket (you'll see a `1006` close) — expected, not
  a bug. This is the client you build against; sockets stay up as you fill V1 in.
- **Auth** — the token field appends `?token=…` to the upgrade URL, ready for the
  security checklist's "authenticate the upgrade". Ignored until you enforce it.
- **A browser can't create real TCP backpressure** — it drains its socket as fast
  as it can, so to *see* drops you generally need a high Firehose rate and/or a
  small `OUTBOX_CAPACITY`. The gap-detection above is the honest, client-side way
  to observe shedding without a server metrics endpoint; once you add one
  (Observability checklist), wire it in here for the authoritative numbers.
- The per-client log is a bounded ring buffer (newest 60) — itself a small nod to
  the "no unbounded queue" rule.
