# Global Conferencing — room + topology client

React + Tailwind + shadcn/ui client for project 17. The media path is project
15's SFU reused; what's *new* here is federation, so this page foregrounds the
**global topology**: each room's one home region, its active regions, and the
cascade legs between SFUs — all read from a single node's `GET /rooms`, which
reflects the replicated placement map the whole cluster agrees on.

## Run

```bash
bun install
bun run dev            # http://localhost:5117
```

Point `bun run dev` at any one region's signaling port (default `:8080`):

```bash
CONF_URL=http://localhost:8080 bun run dev     # region A
CONF_URL=http://localhost:8081 bun run dev     # region B (a second SFU)
```

Media (UDP 7000) and the inter-SFU backbone (UDP 7100) don't go through Vite.

## What's wired vs. yours

- **Wired (glue):** `getUserMedia` preview, mic/camera toggles, publish/subscribe
  signaling calls, and the live global-topology + cascade-leg panels.
- **Yours (the meat):** the media bridge (`connectMedia()`, same as project 15)
  **and** the backend's V1 consensus — `place_room` / `register_interest` are
  `todo!()`, so publish 500s until you build placement. Once V1 lands, the topology
  panel fills in with real home regions and epochs.
