# How a Publisher Session Works — One Connection, From Hello to Video

> A beginner-friendly guide to `session.rs`: what a "session" actually *is*, why it's
> a state machine, and how it stitches together the three things you already built —
> the handshake, the chunk reader, and the AMF0 codec.
>
> No prior knowledge assumed. Anchored to real code in
> [session.rs](../src/session.rs) and [live.rs](../src/live.rs). For the AMF0 *wire
> format* itself, see the sibling doc
> [01-amf0-and-the-publish-state-machine.md](./01-amf0-and-the-publish-state-machine.md);
> for the byte framing under it, [00-rtmp-chunk-stream.md](./00-rtmp-chunk-stream.md).

---

## 0. The one sentence to hold onto

**A `Session` is everything the server remembers about one broadcaster's TCP
connection — and it's a *state machine* because RTMP is a scripted conversation
(`connect` → `createStream` → `publish` → media) that must happen in order, with an
auth gate right before the video is allowed to flow.**

Everything below is unpacking that sentence.

---

## 1. The problem: a socket is just a pipe of bytes

When OBS or `ffmpeg` starts streaming to `rtmp://your-server/live/mykey`, the operating
system hands your program **one TCP connection** — a bidirectional pipe of bytes. That
pipe has no notion of "who is this", "are they allowed", or "has the video started yet".
It is a hose. Bytes come in; bytes go out.

But a live-ingest server has to answer real questions about that hose:

| Question the server must answer | Why the raw socket can't |
|---|---|
| Is this even an RTMP client, or a port scanner? | A socket is bytes; you must *speak* to find out (the handshake). |
| Which of 500 connected broadcasters is this? | The OS gives you a file descriptor, not an identity. |
| Are they allowed to publish to `mykey`? | Nothing checks that until *you* do. |
| Has the actual A/V started, or is this still setup? | Audio and control commands are just bytes on the same pipe. |
| Where does this broadcaster's video go so viewers find it? | You have to *route* it somewhere shared. |

So you need a per-connection scratchpad that holds the answers as you discover them.
That scratchpad is the **`Session`**.

---

## 2. What a `Session` actually is

Look at the struct — it is literally "the answers to the questions above"
([session.rs](../src/session.rs)):

```rust
pub struct Session {
    id: u64,                          // which connection (for logs)
    stream: TcpStream,                // THE hose — this one broadcaster's socket
    registry: Arc<LiveRegistry>,      // shared: the map of all live streams
    cfg: Arc<IngestConfig>,           // config: allowed keys, window size…
    state: State,                     // ← where we are in the conversation
    stream_key: Option<String>,       // filled once they publish (who they are)
    live: Option<Arc<LiveStream>>,    // filled once publishing (where video goes)
}
```

Two of these fields are `Option` for a reason that matters: `stream_key` and `live`
are **`None` until the broadcaster has earned them**. You don't know the key until the
`publish` command arrives, and you don't hand them a place to write video until that key
is authorized. The types encode the lifecycle: an un-authorized session *cannot* have a
`live` window to push into, because it's `None`.

`id`, `registry`, and `cfg` come from the outside (the server). `state`, `stream_key`,
`live` are the session's own evolving memory.

### One connection = one Session = one task

Where do sessions come from? The `accept_loop` at the bottom of the file
([session.rs](../src/session.rs)) is the server's front door:

```rust
Ok((stream, peer)) => {
    let id = next_id; next_id += 1;
    let _ = stream.set_nodelay(true);                 // (see box below)
    let session = Session::new(id, stream, registry.clone());
    tokio::spawn(session.run());                       // ← its own async task
}
```

Every time a broadcaster connects, the loop:
1. mints a new `id`,
2. builds a fresh `Session` (starting in `State::Connected` — see `new`),
3. `tokio::spawn`s `session.run()` — **its own independent task**.

So 500 broadcasters = 500 `Session`s = 500 concurrent tasks, each with its own socket
and its own `state`. They share only the `Arc<LiveRegistry>` (the map of live streams)
and `Arc<IngestConfig>` — both behind `Arc` (a reference count) so cloning them is cheap
and safe to share across tasks. The accept loop itself never blocks on a slow
broadcaster: it spawns and immediately goes back to `accept()`.

> **Why `set_nodelay(true)`?** TCP normally batches small writes (Nagle's algorithm) to
> save packets. RTMP's control replies (`_result`, `onStatus`) are tiny, and the
> broadcaster is *waiting* for them before it proceeds. Batching would add latency to
> every handshake. Turning Nagle off sends them immediately. This is the same reason
> `ffmpeg` sets `TCP_NODELAY` on its RTMP sockets.

---

## 3. The lifecycle: what `run()` does

`run()` is the whole life of one connection, start to finish
([session.rs](../src/session.rs)):

```rust
async fn run(mut self) {
    // (a) prove it's a real RTMP peer
    if let Err(e) = rtmp::handshake(&mut self.stream).await { … return; }

    // (b) set up framing
    let mut reader = ChunkStreamReader::new(MAX_MESSAGE_SIZE);

    // (c) the message loop
    loop {
        match reader.read_message(&mut self.stream).await {
            Ok(msg) => { if self.handle(&mut reader, msg).await.is_err() { break; } }
            Err(e)  => { break; }   // socket closed or protocol error → done
        }
    }

    // (d) teardown
    if let (Some(key), Some(live)) = (&self.stream_key, &self.live) {
        live.mark_ended();
        self.registry.close(key);
    }
}
```

Four phases, and notice how each pulls in a piece you already built:

- **(a) Handshake** — `rtmp::handshake` (V1). Until this returns `Ok`, you don't trust
  the peer at all. A wrong version byte or a bad C2 echo and the function errors, `run`
  returns, the task ends, the socket closes. (This is exactly what your
  `make smoke-rtmp` proved end-to-end.)
- **(b) Framing** — `ChunkStreamReader` (V1). RTMP splits every logical message into
  ≤128-byte chunks; the reader reassembles them back into whole `Message`s. `run`
  doesn't care about chunks — it just asks for the next *message*.
- **(c) The loop** — pull one `Message`, `handle` it, repeat. This is the heart. A
  `Message` has a `type_id` (command? audio? video?) and a `payload` (the bytes). The
  loop is dumb on purpose; all the intelligence is in `handle`.
- **(d) Teardown** — when the loop breaks (socket closed, or `handle` returned an
  error), *if* this session had actually reached publishing, tell the shared world:
  `live.mark_ended()` (so the viewer playlist gets `#EXT-X-ENDLIST` — "the stream is
  over") and `registry.close(key)` (remove it from the live map). If the session never
  published, `stream_key`/`live` are still `None` and there's nothing to clean up — the
  `if let (Some, Some)` guard skips it.

The loop is the load-bearing idea: **the chunk reader turns the byte hose into a stream
of discrete `Message`s, and the session is a loop that reacts to one message at a
time.**

---

## 4. `handle`: match the message, reply, advance the state

`handle` is the dispatcher — currently the `todo!()` you're about to build
([session.rs](../src/session.rs)). Its shape (from the SPEC and the doc-comment
worklist) is a match on `msg.type_id`:

```
AMF0_COMMAND (20)  → decode the AMF0 → look at the command NAME → run the state machine
AUDIO (8)/VIDEO(9) → only legal once Publishing → feed the packager (V3)
SET_CHUNK_SIZE (1) → already absorbed by the reader — nothing to do
other control      → handle or ignore per spec
```

The command messages are where the *conversation* happens, and this is where your AMF0
codec (V2) earns its keep: `amf::decode(&msg.payload)` turns the raw bytes into a
`Vec<Amf0>`, and the **first value is the command name** — a string like `"connect"`.
That name, combined with the current `state`, decides two things:

1. **What reply to send** — build a response with `amf::encode(...)` and write it back
   to `self.stream`.
2. **What state to move to** — reassign `self.state`.

That is the entire pattern. It's a request/response RPC (remote procedure call) running
over the socket, and the `State` enum is your memory of how far the RPC dance has
progressed.

---

## 5. Why a state machine? (the important part)

A "state machine" sounds fancy; it's just **a variable that remembers where you are,
plus rules for which inputs are legal now**. Here it's the `State` enum
([session.rs](../src/session.rs)):

```rust
pub enum State {
    Connected,      // handshake done; waiting for `connect`
    AppConnected,   // `connect` answered; waiting for `createStream`
    StreamCreated,  // `createStream` answered; waiting for `publish`
    Publishing,     // authorized — media is flowing
}
```

Why not just accept whatever comes? Because RTMP setup is **ordered**, and skipping a
step is either a broken client or an attack:

| Without a state machine | With the state machine |
|---|---|
| Audio arrives before `publish` → you'd packetize video from an *unauthenticated* peer. | Media is rejected unless `state == Publishing`. |
| A second `connect` mid-stream → ambiguous; could corrupt the session. | Out-of-order command is rejected/ignored — documented which. |
| Any key streams to any name → stream takeover. | The `publish` handler checks the key **before** flipping to `Publishing`. |

The single most important line of reasoning: **`Publishing` is a gate, and the auth
check is the lock on it.** The transition into `Publishing` only happens inside the
`publish` handler, and only *after* `registry.authorize(key)` returns true
([live.rs](../src/live.rs)):

```rust
pub fn authorize(&self, key: &str) -> bool {
    self.cfg.stream_keys.is_empty()                    // empty allow-list ⇒ any key (dev)
        || self.cfg.stream_keys.iter().any(|k| k == key)
}
```

An unknown key → `authorize` returns false → the session refuses and closes, never
reaching `Publishing`, so the media branch of `handle` can never accept its frames.
This is why the SPEC insists "an open ingest is a takeover vector": the state gate *is*
the security boundary.

---

## 6. End-to-end trace: `ffmpeg` publishing 2 seconds of video

Follow one real broadcast through every layer. Each arrow is bytes on the one socket;
the right column is `self.state` *after* the step.

```
  ffmpeg                         your Session (run → handle)          state
  ─────                          ──────────────────────────          ─────
  [TCP SYN] ───────────────────▶ accept_loop: new Session(id=0)
                                 tokio::spawn(run)                    Connected
  C0/C1 ───────────────────────▶ rtmp::handshake  (V1)
        ◀────────────── S0/S1/S2
  C2 ──────────────────────────▶ echo verified → Ok                  Connected
                                 ── enter message loop ──
  connect("live") ─────────────▶ read_message → AMF0_COMMAND
                                 amf::decode → name "connect"
        ◀── _result (+ Window Ack, Set Peer BW, Set Chunk Size)      AppConnected
  releaseStream / FCPublish ───▶ (bookkeeping commands — ack/ignore) AppConnected
  createStream() ──────────────▶ name "createStream"
        ◀────────── _result(streamId = 1)                            StreamCreated
  publish("mykey","live") ─────▶ name "publish" → authorize("mykey")
                                 ✓ → registry.open("mykey")
                                 self.live = Some(stream)
                                 self.stream_key = Some("mykey")
        ◀── onStatus NetStream.Publish.Start                         Publishing
  [video seq header] ──────────▶ Publishing ✓ → extract avcC (V3)
  [audio seq header] ──────────▶ Publishing ✓ → extract ASC  (V3)
  [video][audio][video]… ──────▶ Publishing ✓ → fmp4 packager → live.push_part()
  [TCP FIN] ───────────────────▶ read_message → Err → loop breaks
                                 live.mark_ended(); registry.close("mykey")
```

Two things to notice in that trace:

- **`createStream`'s reply carries a stream id (`1`).** From then on, the broadcaster
  tags its audio/video messages with that message-stream id, and the reply told it which
  number to use. It's a handle the two sides agree on — like being told "your order
  number is 1" so later messages can reference it.
- **The first video and audio messages are special.** They aren't frames — they're the
  *codec configuration* (H.264 SPS/PPS as `avcC`, AAC `AudioSpecificConfig`). The
  session mines those once to build the CMAF init segment (V3), then the rest are real
  media. The SPEC calls this out: capture the *setup*, not the per-frame data.

---

## 7. In the real world

This tiny state machine is a scale model of what every production RTMP ingest does. The
protocol you're implementing is Adobe's **RTMP 1.0 spec** (2012) plus the de-facto
`FCPublish`/`releaseStream` extensions that Flash Media Encoder introduced and everyone
copied — which is why `ffmpeg` and OBS send them even though the core spec doesn't
mention them.

| System | How it models "the session" | What it adds beyond ours |
|---|---|---|
| **nginx-rtmp** (C, the classic OSS ingest) | One `ngx_rtmp_session_t` per connection with a state field, driven by the same `connect`/`createStream`/`publish` handlers. | `on_publish` HTTP callback: instead of a static key list, it POSTs to *your* app to authorize — exactly where our `registry.authorize` is, but as a webhook. |
| **SRS** / **node-media-server** | Same per-connection session object + command dispatch; node-media-server literally has a `connect`/`createStream`/`publish` switch like our `handle`. | Relay/edge clustering, HTTP-FLV & WebRTC output from the same ingest. |
| **OBS Studio** (the *client*) | Uses `librtmp` under the hood; walks the identical sequence from the other side and **blocks waiting for each reply** before sending the next command. | This is *why* replies must be byte-correct and timely — OBS shows "Failed to connect" if your `_result` is malformed or slow. |
| **ffmpeg** (`-f flv rtmp://…`) | `librtmp`/`rtmpproto.c`: sends `connect`, waits for `_result`, sends `releaseStream`+`FCPublish`, `createStream`, then `publish`. | If your server never replies to `connect`, ffmpeg hangs then errors `Input/output error` — the exact symptom you saw before `handle` was implemented. |
| **Twitch / YouTube / Cloudflare Stream ingest** | RTMP(S) ingest endpoints that are, at the edge, this same handshake + `connect`/`publish` state machine. | The **stream key** *is* the auth token (a long random secret), checked against your account — the production version of `authorize`. They then transcode to multiple renditions and repackage to HLS/DASH, exactly the V3/V4 you're heading toward. |

Where our version deliberately stops:

- **Auth** — ours is a static allow-list (`STREAM_KEYS`) or "any key" in dev. Real
  ingests verify a signed/random secret against an account, often via an HTTP callback
  (`on_publish`) so the key can be rotated and revoked without redeploying. The
  `todo!()` in `authorize` even says so ([live.rs](../src/live.rs)).
- **Backpressure & limits** — production ingests cap concurrent publishers, bytes/sec,
  and message size (we cap the last with `MAX_MESSAGE_SIZE = 16 MiB` so a lying length
  can't OOM us), and drop or throttle abusive peers.
- **RTMPS/RTMPE** — real ingest endpoints usually run over TLS (RTMPS). Ours is plain
  RTMP (`RTMP_VERSION = 0x03`), fine for a from-scratch learning ingest on localhost.
- **The wider command set** — `deleteStream`, `closeStream`, `FCUnpublish`, `receiveAudio`,
  ping/pong user-control events. Ours handles the publish happy-path and ignores the
  rest; a hardened server answers them all.

The payoff of building the small version: the words in Twitch's ingest docs
("stream key", "your encoder connects and publishes") stop being magic. You've held
every byte.

---

## 8. Mental-model summary

| It looks like… | …but it actually is |
|---|---|
| "The server receives a video stream." | The server runs a scripted RPC conversation, and video is only the *last* phase after three commands and an auth check. |
| A `Session` is the video. | A `Session` is per-connection *memory*: where we are (`state`), who they are (`stream_key`), where video goes (`live`). |
| The state machine is bureaucracy. | It's the **security boundary** — `Publishing` is a gate whose lock is the stream-key check. |
| `handle` "processes messages." | `handle` = match the command name + current state → send a reply → advance the state. |
| One server handles all broadcasters in one place. | One `tokio` task *per connection*, each with its own socket and `state`, sharing only the `Arc`'d registry. |
| The first audio/video is the first frame. | The first A/V messages are the **codec config** (avcC / ASC), mined once for the init segment. |

---

## 9. Where to look in the code

| Subtopic | File / item |
|---|---|
| The per-connection scratchpad | `Session` struct — [session.rs](../src/session.rs) |
| The lifecycle states | `enum State` — [session.rs](../src/session.rs) |
| Front door: one task per connection | `accept_loop` — [session.rs](../src/session.rs) |
| The four phases of a connection | `Session::run` — [session.rs](../src/session.rs) |
| The dispatcher you're building | `Session::handle` (`todo!()`) — [session.rs](../src/session.rs) |
| Proving the peer is real (V1) | `rtmp::handshake` — [rtmp.rs](../src/rtmp.rs) |
| Byte hose → whole messages (V1) | `ChunkStreamReader::read_message` — [rtmp.rs](../src/rtmp.rs) |
| Command bytes → values (V2) | `amf::decode` / `amf::encode` — [amf.rs](../src/amf.rs) |
| The auth gate | `LiveRegistry::authorize` — [live.rs](../src/live.rs) |
| Where video is routed once publishing | `LiveRegistry::open` / `LiveStream::push_part` — [live.rs](../src/live.rs) |
| Teardown signal to viewers | `LiveStream::mark_ended` / `LiveRegistry::close` — [live.rs](../src/live.rs) |

---

*Next: with the session shape clear, the V2 work is filling in `handle` — decode each
command, send the matching reply, and flip the state, with the key check gating
`Publishing`. The AMF0 replies themselves are in
[01-amf0-and-the-publish-state-machine.md](./01-amf0-and-the-publish-state-machine.md).*
