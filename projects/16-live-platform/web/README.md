# Live Platform — watch page

React + Tailwind + shadcn/ui watch page for project 16. "Glass to glass" ends at
glass: this is the glass. An LL-HLS player on the ABR master playlist sits next to
the channel's chat + presence WebSocket — the two viewer-facing surfaces the
platform composes from ingest, transcode, packaging, and edge.

## Run

```bash
bun install
bun run dev            # http://localhost:5116
```

The dev server proxies `/live`, `/chat` (WebSocket), `/ingest`, and `/status` to
the backend on `http://localhost:8080` (docker deps: postgres/nats/redis via the
project's `docker-compose.yml`). Override with `PLATFORM_URL=...`.

## What's wired vs. yours

- **Wired (glue):** LL-HLS playback, the chat WebSocket client, live-status
  header, channel/name inputs.
- **Yours (the backend):** the control plane, transcode ladder, LL-HLS packaging,
  edge, and the `chat_socket` pump. The chat client assumes JSON `ChatMessage`
  frames — match it in `src/routes.rs::chat_socket` (or adjust the client).
