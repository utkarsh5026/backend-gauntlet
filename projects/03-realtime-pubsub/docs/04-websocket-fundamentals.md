# WebSocket Fundamentals — From First Principles

> The protocol layer woven through *all four* verticals: how a normal HTTP request
> becomes a **persistent, two-way** WebSocket, why that changes everything about
> server design, and the five things you must get right around it — the upgrade,
> ping/pong liveness, authenticating *before* the socket exists, capping everything
> a client controls, and closing gracefully. No prior knowledge of HTTP or
> WebSockets assumed.
>
> Covers the horizontal checklist + the `CONCEPTS.md` "rapid-fire round". Anchored to
> real code: [src/routes.rs](../src/routes.rs), [src/main.rs](../src/main.rs),
> [src/protocol.rs](../src/protocol.rs), [.env.example](../.env.example).

---

## 0. The one sentence to hold onto

**A WebSocket is a normal HTTP request that, once, asks the server "can we stop doing
request/response and just keep this TCP connection open to send bytes both ways?" —
and everything hard about this project comes from the server now holding *thousands*
of those open, stateful connections at once.**

---

## 1. Why HTTP alone can't do real-time

Plain HTTP is *request → response → done*. The client asks, the server answers, the
connection's job is over. That's perfect for loading a web page and useless for "tell
me the instant someone posts in room1", because **the server has no way to speak
first.** The client would have to ask "anything new?" over and over (polling) — wasteful
and always a beat behind.

WebSockets fix the shape: **one** HTTP request bootstraps a **persistent, full-duplex**
channel. After that handshake, either side can send a message at any time, with no new
request. The server can finally push.

The cost of that gift is the entire theme of this project: a request/response server
handles a request and *forgets* you. A WebSocket server **remembers** you — it holds
your socket, your subscriptions ([hub, V1](00-the-fan-out-hub-and-lock-discipline.md)),
your outbound buffer ([backpressure, V2](01-backpressure-and-the-slow-consumer.md)),
your room membership ([presence, V3](02-presence-as-soft-state.md)) — for as long as
you're connected. Thousands of clients = thousands of live, stateful connections. That
statefulness is where all four verticals come from.

---

## 2. The upgrade: `101 Switching Protocols`

The handshake is an ordinary HTTP `GET` with special headers that say "let's switch
protocols":

```
   CLIENT                                          SERVER
   GET /ws HTTP/1.1
   Host: example.com
   Upgrade: websocket            ──────────▶
   Connection: Upgrade
   Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
   Sec-WebSocket-Version: 13
                                 ◀──────────   HTTP/1.1 101 Switching Protocols
                                               Upgrade: websocket
                                               Connection: Upgrade
                                               Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=
   ── from here on, it is NOT HTTP anymore. Same TCP socket, WebSocket frames both ways. ──
```

Two things the `101` actually negotiates: (1) *both sides agree to abandon HTTP
semantics* on this connection and speak the WebSocket framing protocol instead; (2) the
`Sec-WebSocket-Accept` value is the server hashing the client's `Sec-WebSocket-Key` in
a defined way — a proof it genuinely understood the upgrade (not a confused proxy
echoing bytes). After `101`, the connection is **stateful**: it's a long-lived pipe,
not a series of independent requests.

In this project you don't hand-roll any of that — axum's extractor does it. From
[src/routes.rs](../src/routes.rs):

```rust
.route("/ws", get(ws_handler))                     // an ordinary GET route
async fn ws_handler(ws: WebSocketUpgrade, State(state): State<AppState>) -> Response {
    ws.on_upgrade(move |socket| handle_socket(socket, state))  // returns 101, then runs your loop
}
```

`WebSocketUpgrade` is the extractor that performs the handshake; `on_upgrade` hands you
the live `socket` *after* the `101`. Everything from §3 on happens on that open socket.

---

## 3. Full-duplex means two tasks: reader and writer

Once open, messages flow *both* directions independently — Bob can send a `publish`
while the server is pushing him three `message`s. You can't model that with one
sequential loop, so `handle_socket` in [src/routes.rs](../src/routes.rs) splits the
socket in two:

```rust
let (mut ws_tx, mut ws_rx) = socket.split();     // sink (out) + stream (in)

let mut writer = tokio::spawn(async move {        // WRITER task: outbox → socket
    while let Some(msg) = outbox.recv().await { ws_tx.send(Message::Text(...)).await ... }
});

loop {                                            // READER loop: socket → dispatch
    tokio::select! {
        frame = ws_rx.next() => match frame { ... dispatch(cmd) ... },
        _ = &mut writer => break,                 // writer died ⇒ socket's gone ⇒ we're done
    }
}
```

This is why V2's mailbox exists *between* them: the reader/hub push into the bounded
`Outbox`, the writer drains it to the socket at the client's pace. The two halves are
decoupled by that queue — which is exactly what lets backpressure be a per-connection
decision. The `select!` also ties their lifetimes together: if the writer task ends
(socket closed on the send side), the reader breaks too, and control falls to teardown.

---

## 4. Ping/pong: the heartbeat that detects the dead

Here's a nasty truth: a TCP connection can be **dead for minutes** while looking
perfectly alive to your server. If a client's machine loses power or its network
vanishes, no FIN/RST is ever sent — your `ws_rx.next()` just... never returns anything,
and the socket sits there consuming a slot and a mailbox forever.

The fix is the WebSocket **ping/pong** control frames — the transport-level heartbeat
that powers [presence's absence detection (V3)](02-presence-as-soft-state.md):

```
   server ──ping──▶ client
   client ──pong──▶ server      → "still alive", refresh last-seen
   ... server pings every N seconds ...
   server ──ping──▶ client
   (silence — no pong within the window)  → presume dead → close + reap the connection
```

Who sends: typically the server pings on an interval; a well-behaved client auto-pongs.
A **missed pong** (no reply within a timeout) is your signal that the client is gone —
close the socket and run teardown, freeing the hub/presence/mailbox slots. Crucially,
close with a **proper WebSocket close frame + code**, not by yanking TCP — the client
deserves to know *why* (and a clean close lets it reconnect sanely instead of guessing).

The scaffold leaves this as a `todo!()`-shaped gap you'll fill. In
[src/routes.rs](../src/routes.rs) the reader currently swallows control frames:

```rust
Some(Ok(_)) => {} // ping/pong/binary: TODO(protocol) handle heartbeats
```

That's the hook: respond to pings, treat pongs as liveness, and drive an idle-timeout
that closes unresponsive sockets. (Also the natural place to reject malformed frames
with a `ServerMessage::Error` — [src/protocol.rs](../src/protocol.rs) already defines
it — rather than silently dropping the connection, which the protocol checklist asks
for. Note the handler *already* replies to an unparseable frame with an `Error` and
keeps the socket open.)

---

## 5. Authenticate *before* the socket exists

The most important security rule here is about **timing**. Check auth on the *upgrade
request*, before you accept the socket — not after. Why? Because an open WebSocket is
*expensive and stateful*: it's a held file descriptor, a task, a mailbox, a slot in the
hub. If you let anyone open a socket and only check credentials once they send their
first message, an attacker opens 100,000 anonymous sockets and exhausts your resources
*before authenticating a single one*. That's a trivial denial of service.

So the check belongs in `ws_handler`, where you still have a normal HTTP request you can
reject with a plain `401` — no socket ever gets created. The scaffold marks the exact
spot in [src/routes.rs](../src/routes.rs):

```rust
// TODO(security): authenticate *before* accepting the upgrade — pull an API
// key / token from the query string or a header and reject with `401` here, so
// anonymous clients can never open a socket.
async fn ws_handler(ws: WebSocketUpgrade, State(state): State<AppState>) -> Response { ... }
```

Pull a token from a header or query param, validate it, and only then call
`ws.on_upgrade(...)`. And — tying back to [presence (V3)](02-presence-as-soft-state.md)
— derive the client's *display identity* from that verified token, never from what the
client claims. The scaffold's `dispatch` uses `conn.to_string()` as a placeholder
precisely because trusting client-supplied identity is a spoofing hole (Bob announces
himself as "Alice"). Client `identity` is for display only; never log tokens.

---

## 6. Cap everything a client controls

A held-open connection lets a client feed you input indefinitely — so *every*
client-controlled quantity needs a ceiling, or one socket can wedge the server. The
horizontal checklist enumerates them; each cap blocks a specific abuse:

| Cap | Without it… |
|-----|-------------|
| **Max message size** | one client streams a 2 GB "frame" and OOMs the node |
| **Max topics per connection** | a client subscribes to 10M topics, bloating the hub map on your dime |
| **Max subscribers / connections** | unbounded sockets exhaust file descriptors and memory |
| **Publish rate per connection** | one client floods a room at 1M msg/s, saturating everyone's mailboxes |
| **Topic-name length + charset** | a client wedges the `HashMap` with a 1 MB key or control characters |

The theme: **validate and bound anything that crosses the trust boundary.** The client
is not your friend; the caps are how you stay standing when it misbehaves (deliberately
or via a bug). Sensible defaults live alongside the others in
[.env.example](../.env.example) (`OUTBOX_CAPACITY` is one such bound already). Reject
over-limit input with a `ServerMessage::Error`, don't silently truncate or drop the
connection.

---

## 7. Graceful shutdown: don't yank the rug

When you deploy or scale down, the server stops — but it holds thousands of live
sockets. Kill the process and every client's TCP connection dies mid-flight with no
explanation, and they *all reconnect at the same instant* — a **reconnect storm** that
hammers whichever node is still up (a self-inflicted thundering herd).

Graceful shutdown is the courteous version: **stop accepting** new connections, then
**close the live ones with a proper close frame** (and drain any buffered work) so
clients learn "server going away, reconnect calmly" and back off. The plumbing is
started for you in [src/main.rs](../src/main.rs):

```rust
axum::serve(listener, app).with_graceful_shutdown(shutdown_signal()).await?;

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
    // TODO(protocol): on shutdown, close live sockets with a proper WebSocket close
    // frame rather than dropping the TCP connections out from under clients.
}
```

`with_graceful_shutdown` already stops *accepting* on the signal; the `todo!()` is
sending each live socket a close frame before exit. This is the connection-lifecycle
bookend to V1's disconnect discipline: every exit path — clean close, error, abrupt
drop, *and server shutdown* — must remove the connection from the hub and presence.

---

## 8. Mental model — looks-like vs actually-is

| It looks like… | It actually is… |
|----------------|-----------------|
| "A WebSocket is just a faster HTTP request." | A persistent, stateful, full-duplex pipe — the server now *remembers* every client. |
| "The `101` is a formality." | It's both sides agreeing to abandon HTTP and speak framing; the connection is stateful after it. |
| "TCP tells me when a client's gone." | Not for minutes on a silent death — ping/pong is how you detect it in human time. |
| "Auth the first message." | Too late — the expensive socket already exists. Auth the *upgrade*, reject with `401`. |
| "Trust the `identity` the client sends." | Display-only; derive real identity from the verified token, or it's spoofable. |
| "Just `kill` the process to deploy." | Yanks every socket → reconnect storm. Stop accepting, close frames, drain. |

---

## 9. Where you'll build this

These are the **horizontal checklist** items — woven through the verticals rather than
one module. The hooks the scaffold leaves you:

- **Ping/pong + malformed-frame handling** — the `Some(Ok(_)) => {}` branch and the
  `Error` reply in [src/routes.rs](../src/routes.rs) (§4).
- **Auth on upgrade** — the `TODO(security)` in `ws_handler`
  ([src/routes.rs](../src/routes.rs)) (§5).
- **Client-controlled caps** — validate in `dispatch`
  ([src/routes.rs](../src/routes.rs)); defaults in
  [.env.example](../.env.example) (§6).
- **Graceful close frames** — the `TODO(protocol)` in `shutdown_signal`
  ([src/main.rs](../src/main.rs)) (§7).

**This doc unlocks these horizontal "Done when its criterion is observably true" items:**

- [ ] HTTP upgrade to WebSocket done correctly (`GET /ws`, 101 via the axum extractor).
  *(§2)*
- [ ] A versioned, typed JSON protocol; reject malformed frames with an `error`
  message, don't drop silently. *(§4)*
- [ ] Respond to ping/pong and use it as liveness; close idle/unresponsive sockets with
  a proper close frame + code. *(§4)*
- [ ] Graceful shutdown: stop accepting, then close live sockets with a close frame.
  *(§7)*
- [ ] Authenticate the upgrade before accepting the socket; cap every client-controlled
  input; topic-name validation; never trust client `identity`, never log tokens. *(§5,
  §6)*

---

*This is the protocol substrate under all four verticals. Start at
[V1 — the fan-out hub](00-the-fan-out-hub-and-lock-discipline.md) if you haven't, and
follow the chain V1 → V2 → V3 → V4.*
