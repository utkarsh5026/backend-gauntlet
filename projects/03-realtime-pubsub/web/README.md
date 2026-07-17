# Pub/Sub Chat

A chat client for the project-03 pub/sub server — because a chat room *is* a
pub/sub topic: joining a room subscribes you to it, sending a message
publishes to it, "who's online" is a presence frame, and another tab in the
same room lighting up live *is* fan-out.

Stack (per the repo's frontend convention): **Bun · React + TypeScript · Vite ·
Tailwind v4 · shadcn/ui**, dark theme.

## Run

```bash
# 1. start the backend (from projects/03-realtime-pubsub/) on port 8080
cargo run -p realtime-pubsub

# 2. start this app (from projects/03-realtime-pubsub/web/)
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

You can also override the endpoint/token per-tab from the sidebar's ⚙ settings —
set the endpoint to an absolute `ws://host:port/ws` to bypass the proxy entirely.

## What it does

Pick a display name, join a room, start typing. Everything that makes pub/sub
*pub/sub* is visible without leaving the chat metaphor:

- **Fan-out, visible** — open the same room in a second tab (or ask a friend to
  join). One message shows up in both threads at once, because the hub delivered
  it to every subscriber, not just you.
- **Presence** — the room header lists who else is in it, driven by `presence`
  frames. Stays empty until V3 is wired; disconnect a tab (or close it) and watch
  the membership update.
- **Latency, per message** — every message carries a `ts` from send time; since
  every tab shares the page clock, the timestamp under each bubble is a true
  end-to-end delivery latency.
- **Backpressure & load, in the Dev tools panel** — a chat UI alone can't make
  drop-shedding *visible*: you need traffic. Flip on **Dev tools** for a firehose
  (publish at 1–2000/s to any topic) and live counters for publish/deliver rate
  and **dropped gaps** — holes in a per-sender sequence number, the client-side
  mirror of the server shedding messages under its overflow policy (V2's payoff).

## Notes & honest limits

- **The backend ships with `todo!()` bodies (V1–V4).** In the fresh scaffold the
  V1 hub is entirely unimplemented, so the first `subscribe` *or* publish panics
  in `src/hub.rs` and drops the socket (you'll see a `1006` close, and your own
  message never appears — no local echo, it only shows up once the server
  actually fans it back out to you). Expected, not a bug; sockets stay up as you
  fill V1 in.
- **Auth** — the token field appends `?token=…` to the upgrade URL, ready for the
  security checklist's "authenticate the upgrade". Ignored until you enforce it.
- **A browser can't create real TCP backpressure** — it drains its socket as fast
  as it can, so to *see* drops you generally need a high Firehose rate and/or a
  small `OUTBOX_CAPACITY`. The gap-detection above is the honest, client-side way
  to observe shedding without a server metrics endpoint; once you add one
  (Observability checklist), wire it in here for the authoritative numbers.
- Each room's thread is a bounded ring buffer (newest 300) — a small nod to the
  "no unbounded queue" rule.
