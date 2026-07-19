# Live Ingest — LL-HLS web player

React + Tailwind + shadcn/ui low-latency player for project 13. `hls.js` runs in
`lowLatencyMode`, and the page puts **live latency** front and centre — that's the
number the whole project exists to shrink. If your 200ms parts, `PART-HOLD-BACK`,
and blocking playlist reload are correct, it sits well under a second.

## Run

```bash
bun install
bun run dev            # http://localhost:5113
```

The dev server proxies `/live` to the backend's HTTP delivery plane on
`http://localhost:8080`. Push a stream over RTMP (a separate raw-TCP port), then
watch it here:

```bash
cargo run -p live-ingest             # binds HTTP :8080 + RTMP :1935
ffmpeg -re -i in.mp4 -c copy -f flv rtmp://localhost:1935/live/testkey
```

Point at a different backend with `LIVE_URL=http://host:port bun run dev`.

## What's wired vs. yours

- **Wired (glue):** LL-HLS playback, live-latency / target / buffer / stall
  readouts, jump-to-edge, `/live` on-air list.
- **Yours (the backend):** the RTMP handshake, AMF/chunk parsing, fMP4 part
  packaging, and the blocking-reload playlist — build V1–V4 and this lights up.
