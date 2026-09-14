# How a Publisher Session Works — One Connection, From Hello to Video

> A beginner-friendly guide to `session.py`: what a "session" actually *is*, why it's
> a state machine, and how it stitches together the three things it is built on —
> the handshake, the chunk reader, and the AMF0 codec.
>
> No prior knowledge assumed. Anchored to real code in
> [session.py](../src/live_ingest/session.py), [ingest.py](../src/live_ingest/ingest.py) and
> [live.py](../src/live_ingest/live.py). For the AMF0 *wire
> format* itself, see the sibling doc
> [01-amf0-and-the-publish-state-machine.md](./01-amf0-and-the-publish-state-machine.md);
> for the byte framing under it, [00-rtmp-chunk-stream.md](./00-rtmp-chunk-stream.md).

---

## 0. The one sentence to hold onto

**A `PublishSession` is everything the server remembers about one broadcaster's TCP
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
That scratchpad is the **`PublishSession`**.

---

## 2. What a `PublishSession` actually is

Look at `__init__` — it is literally "the answers to the questions above"
([session.py](../src/live_ingest/session.py)):

```python
class PublishSession:
    def __init__(self, session_id, reader, writer, registry, settings):
        self.id = session_id  # which connection (for logs)
        self.reader = reader  # THE hose, inbound — this broadcaster's socket
        self.writer = writer  # …and outbound, for replies
        self.registry = registry  # shared: the map of all live streams
        self.settings = settings  # config: allowed keys, window size…
        self.chunks = ChunkStreamReader()  # V1's per-connection chunk state
        self.state = SessionState.CONNECTED  # ← where we are in the conversation
        self.stream_key: str | None = None  # filled once they publish (who they are)
        self.live: LiveStream | None = None  # filled once publishing (where video goes)
```

Two of these fields are `X | None` for a reason that matters: `stream_key` and `live`
are **`None` until the broadcaster has earned them**. You don't know the key until the
`publish` command arrives, and you don't hand them a place to write video until that key
is authorized. The types encode the lifecycle: an un-authorized session *cannot* have a
`live` window to push into, because it's `None` — and pyright strict will not
let `handle` call `self.live.push_part` without first proving it isn't.

`id`, `registry`, and `settings` come from the outside (the server). `state`, `stream_key`,
`live` are the session's own evolving memory.

### One connection = one PublishSession = one task

Where do sessions come from? `RtmpIngest` ([ingest.py](../src/live_ingest/ingest.py)) is the
server's front door. `asyncio.start_server` calls its `_on_connection` once per
accepted socket, *inside a task it created for that connection*:

```python
async def _on_connection(self, reader, writer):
    session_id = next(self._ids)  # mint an id
    structlog.contextvars.bind_contextvars(rtmp_session=session_id)  # this task's logs only
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # (see box below)
    session = PublishSession(session_id, reader, writer, self._registry, self._settings)
    await session.run()  # ← already its own task
```

Every time a broadcaster connects, asyncio:
1. creates a task for that connection and calls `_on_connection` in it, which
2. mints a new `id` and builds a fresh `PublishSession` (starting in `SessionState.CONNECTED`),
3. awaits `session.run()` — **inside that independent task**.

So 500 broadcasters = 500 `PublishSession`s = 500 concurrent tasks, each with its own
socket and its own `state`. They share only the `LiveRegistry` (the map of live streams)
and the `Settings` — plain references, safe to share because every task runs on the
same event-loop thread and none of the registry's methods `await` mid-mutation (see
[live.py](../src/live_ingest/live.py)). The listener never blocks on a slow broadcaster: a session
waiting on its socket is a suspended coroutine, not a busy thread.

> **Why `TCP_NODELAY`?** TCP normally batches small writes (Nagle's algorithm) to
> save packets. RTMP's control replies (`_result`, `onStatus`) are tiny, and the
> broadcaster is *waiting* for them before it proceeds. Batching would add latency to
> every handshake. Turning Nagle off sends them immediately. This is the same reason
> `ffmpeg` sets `TCP_NODELAY` on its RTMP sockets.

---

## 3. The lifecycle: what `run()` does

`run()` is the whole life of one connection, start to finish
([session.py](../src/live_ingest/session.py)):

```python
async def run(self) -> None:
    # (a) prove it's a real RTMP peer
    await handshake(self.reader, self.writer)

    # (b) framing — self.chunks, a ChunkStreamReader, was built in __init__

    try:
        # (c) the message loop — ends when something raises
        while True:
            message = await self.chunks.read_message(self.reader)
            await self.handle(message)
    finally:
        # (d) teardown — runs however the loop ended
        if self.live is not None:
            self.live.mark_ended()
        if self.stream_key is not None:
            self.registry.close(self.stream_key)
```

Four phases, and notice how each pulls in a piece you already built:

- **(a) Handshake** — `rtmp.handshake` (V1). Until this returns, you don't trust
  the peer at all. A wrong version byte or a bad C2 echo and it raises
  `HandshakeError`, `run` lets it propagate, [ingest.py](../src/live_ingest/ingest.py) logs it, the
  task ends, the socket closes. (`make smoke-rtmp` proves it against a real `ffmpeg`.)
- **(b) Framing** — `ChunkStreamReader` (V1). RTMP splits every logical message into
  ≤128-byte chunks; the reader reassembles them back into whole `Message`s. `run`
  doesn't care about chunks — it just asks for the next *message*.
- **(c) The loop** — pull one `Message`, `handle` it, repeat. This is the heart. A
  `Message` has a `type_id` (command? audio? video?) and a `payload` (the bytes). The
  loop is dumb on purpose; all the intelligence is in `handle`.
- **(d) Teardown** — when the loop ends (socket closed, or `handle` raised), *if* this session had actually reached publishing, tell the shared world:
  `live.mark_ended()` (so the viewer playlist gets `#EXT-X-ENDLIST` — "the stream is
  over") and `registry.close(key)` (remove it from the live map). If the session never
  published, `stream_key`/`live` are still `None` and there's nothing to clean up — the
  `is not None` guards skip it.

The loop is the load-bearing idea: **the chunk reader turns the byte hose into a stream
of discrete `Message`s, and the session is a loop that reacts to one message at a
time.**

---

## 4. `handle`: match the message, reply, advance the state

`handle` is the dispatcher — currently the `NotImplementedError` you're about to build
([session.py](../src/live_ingest/session.py)). Its shape (from the SPEC and the doc-comment
worklist) is a dispatch on `message.type_id`:

```
AMF0_COMMAND (20)  → decode the AMF0 → look at the command NAME → run the state machine
AUDIO (8)/VIDEO(9) → only legal once Publishing → feed the packager (V3)
SET_CHUNK_SIZE (1) → already absorbed by the reader — nothing to do
other control      → handle or ignore per spec
```

The command messages are where the *conversation* happens, and this is where your AMF0
codec (V2) earns its keep: `amf.decode(message.payload)` turns the raw bytes into a
`list[AmfValue]`, and the **first value is the command name** — a string like `"connect"`.
That name, combined with the current `state`, decides two things:

1. **What reply to send** — build a response with `amf.encode(...)` and write it back
   to `self.writer`.
2. **What state to move to** — reassign `self.state`.

That is the entire pattern. It's a request/response RPC (remote procedure call) running
over the socket, and the `SessionState` enum is your memory of how far the RPC dance has
progressed.

---

## 5. Why a state machine? (the important part)

A "state machine" sounds fancy; it's just **a variable that remembers where you are,
plus rules for which inputs are legal now**. Here it's the `SessionState` enum
([session.py](../src/live_ingest/session.py)):

```python
class SessionState(StrEnum):
    CONNECTED = "connected"  # handshake done; waiting for `connect`
    APP_CONNECTED = "app_connected"  # `connect` answered; waiting for `createStream`
    STREAM_CREATED = "stream_created"  # `createStream` answered; waiting for `publish`
    PUBLISHING = "publishing"  # authorized — media is flowing
```

Why not just accept whatever comes? Because RTMP setup is **ordered**, and skipping a
step is either a broken client or an attack:

| Without a state machine | With the state machine |
|---|---|
| Audio arrives before `publish` → you'd packetize video from an *unauthenticated* peer. | Media is rejected unless `state is SessionState.PUBLISHING`. |
| A second `connect` mid-stream → ambiguous; could corrupt the session. | Out-of-order command is rejected/ignored — documented which. |
| Any key streams to any name → stream takeover. | The `publish` handler checks the key **before** flipping to `Publishing`. |

The single most important line of reasoning: **`Publishing` is a gate, and the auth
check is the lock on it.** The transition into `Publishing` only happens inside the
`publish` handler, and only *after* `registry.authorize(key)` returns true
([live.py](../src/live_ingest/live.py)):

```python
def authorize(self, key: str) -> bool:
    return not self._allowed or key in self._allowed  # empty allow-list ⇒ any key (dev)
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
  [TCP SYN] ───────────────────▶ RtmpIngest: new PublishSession(id=0)
                                 run() in its own task                Connected
  C0/C1 ───────────────────────▶ rtmp.handshake  (V1)
        ◀────────────── S0/S1/S2
  C2 ──────────────────────────▶ echo verified → returns             Connected
                                 ── enter message loop ──
  connect("live") ─────────────▶ read_message → AMF0_COMMAND
                                 amf.decode → name "connect"
        ◀── _result (+ Window Ack, Set Peer BW, Set Chunk Size)      AppConnected
  releaseStream / FCPublish ───▶ (bookkeeping commands — ack/ignore) AppConnected
  createStream() ──────────────▶ name "createStream"
        ◀────────── _result(streamId = 1)                            StreamCreated
  publish("mykey","live") ─────▶ name "publish" → authorize("mykey")
                                 ✓ → registry.open("mykey")
                                 self.live = stream
                                 self.stream_key = "mykey"
        ◀── onStatus NetStream.Publish.Start                         Publishing
  [video seq header] ──────────▶ Publishing ✓ → extract avcC (V3)
  [audio seq header] ──────────▶ Publishing ✓ → extract ASC  (V3)
  [video][audio][video]… ──────▶ Publishing ✓ → fmp4 packager → live.push_part()
  [TCP FIN] ───────────────────▶ read_message raises EOFError → loop ends
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
| **ffmpeg** (`-f flv rtmp://…`) | `librtmp`/`rtmpproto.c`: sends `connect`, waits for `_result`, sends `releaseStream`+`FCPublish`, `createStream`, then `publish`. | If your server never replies to `connect`, ffmpeg hangs then errors `Input/output error` — the exact symptom you'll see before `handle` is implemented. |
| **Twitch / YouTube / Cloudflare Stream ingest** | RTMP(S) ingest endpoints that are, at the edge, this same handshake + `connect`/`publish` state machine. | The **stream key** *is* the auth token (a long random secret), checked against your account — the production version of `authorize`. They then transcode to multiple renditions and repackage to HLS/DASH, exactly the V3/V4 you're heading toward. |

Where our version deliberately stops:

- **Auth** — ours is a static allow-list (`STREAM_KEYS`) or "any key" in dev. Real
  ingests verify a signed/random secret against an account, often via an HTTP callback
  (`on_publish`) so the key can be rotated and revoked without redeploying. The
  `TODO` on `authorize` even says so ([live.py](../src/live_ingest/live.py)).
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
| A `PublishSession` is the video. | A `PublishSession` is per-connection *memory*: where we are (`state`), who they are (`stream_key`), where video goes (`live`). |
| The state machine is bureaucracy. | It's the **security boundary** — `Publishing` is a gate whose lock is the stream-key check. |
| `handle` "processes messages." | `handle` = match the command name + current state → send a reply → advance the state. |
| One server handles all broadcasters in one place. | One `asyncio` task *per connection*, each with its own socket and `state`, sharing only the registry — all on one loop thread. |
| The first audio/video is the first frame. | The first A/V messages are the **codec config** (avcC / ASC), mined once for the init segment. |

---

## 9. Where to look in the code

| Subtopic | File / item |
|---|---|
| The per-connection scratchpad | `PublishSession` class — [session.py](../src/live_ingest/session.py) |
| The lifecycle states | `SessionState` — [session.py](../src/live_ingest/session.py) |
| Front door: one task per connection | `RtmpIngest._on_connection` — [ingest.py](../src/live_ingest/ingest.py) |
| The four phases of a connection | `PublishSession.run` — [session.py](../src/live_ingest/session.py) |
| The dispatcher you're building | `PublishSession.handle` (`NotImplementedError`) — [session.py](../src/live_ingest/session.py) |
| Proving the peer is real (V1) | `rtmp.handshake` — [rtmp.py](../src/live_ingest/rtmp.py) |
| Byte hose → whole messages (V1) | `ChunkStreamReader.read_message` — [rtmp.py](../src/live_ingest/rtmp.py) |
| Command bytes → values (V2) | `amf.decode` / `amf.encode` — [amf.py](../src/live_ingest/amf.py) |
| The auth gate | `LiveRegistry.authorize` — [live.py](../src/live_ingest/live.py) |
| Where video is routed once publishing | `LiveRegistry.open` / `LiveStream.push_part` — [live.py](../src/live_ingest/live.py) |
| Teardown signal to viewers | `LiveStream.mark_ended` / `LiveRegistry.close` — [live.py](../src/live_ingest/live.py) |

---

*Next: with the session shape clear, the V2 work is filling in `handle` — decode each
command, send the matching reply, and flip the state, with the key check gating
`Publishing`. The AMF0 replies themselves are in
[01-amf0-and-the-publish-state-machine.md](./01-amf0-and-the-publish-state-machine.md).*
