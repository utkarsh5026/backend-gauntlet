# WebRTC SFU — room client

React + Tailwind + shadcn/ui room page for project 15. You literally cannot
exercise an SFU without a browser peer, so this is part of the project, not an
extra. It opens your camera, announces a simulcast ladder to the signaling API,
and renders the live topology from `GET /rooms`.

## Run

```bash
bun install
bun run dev            # http://localhost:5115  (WebRTC needs a secure origin;
                       #  localhost counts as secure, so getUserMedia works)
cargo run -p webrtc-sfu             # signaling :8080, media UDP :7000
```

The dev server proxies the **signaling + admin** HTTP API to `:8080`. The **media
plane is UDP on :7000** — the browser ICE-connects to the advertised host
candidate directly, not through Vite. Override signaling with `SFU_URL=...`.

## What's wired vs. yours

- **Wired (glue):** `getUserMedia` local preview, mic/camera toggles, the
  `POST /publish` + `POST /subscribe` signaling calls, and the live topology poll.
- **Yours (the meat):** `connectMedia()` in `src/webrtc.ts` throws on purpose.
  This SFU hands back raw ICE creds + a media UDP address (not an SDP answer);
  bridging a browser `RTCPeerConnection` to that — ICE, DTLS, SRTP, simulcast
  `sendEncodings` — is exactly the SPEC's V1/V3 interop work. Build it and the
  remote tiles come alive.
