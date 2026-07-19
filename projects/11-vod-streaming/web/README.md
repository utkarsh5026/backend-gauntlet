# VOD Streaming — web player

React + Tailwind + shadcn/ui player for project 11. A real `hls.js` player is the
only thing that proves adaptive bitrate: `curl` can fetch a segment, but it can't
switch renditions mid-stream. This page loads your master playlist, lists the
rungs of the ABR ladder, lets you pin a level, and logs every switch.

## Run

```bash
bun install
bun run dev            # http://localhost:5111
```

The dev server proxies `/vod`, `/assets`, and `/healthz` to the backend on
`http://localhost:8080`. Start the backend first:

```bash
cargo run -p vod-streaming           # from repo root (binds :8080)
```

Point at a different backend with `VOD_URL=http://host:port bun run dev`.

## What's wired vs. yours

- **Wired (glue):** `hls.js` playback, level list from the master playlist, forced
  rendition pinning, buffer/dropped-frame stats, switch log, `/assets` catalog.
- **Yours (the backend):** the manifests and fMP4 segments this player consumes —
  build V1–V4 in `src/` and this page lights up.
