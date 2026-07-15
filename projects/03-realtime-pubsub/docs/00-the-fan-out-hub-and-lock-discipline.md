# The Fan-out Hub & Lock Discipline — From First Principles

> A ground-up guide to the core of every pub/sub system: a registry that maps
> **topic → subscribers** and broadcasts a message to all of them. We build the
> intuition for *why* the map is the easy part and the **locking discipline** is
> the hard part — the difference between a toy and a server that survives one slow
> client. No prior knowledge of pub/sub, locks, or async assumed.
>
> Prepares you for **V1** (`src/hub.rs`). Anchored to real code:
> [src/hub.rs](../src/hub.rs), [src/backpressure.rs](../src/backpressure.rs),
> [src/protocol.rs](../src/protocol.rs), [src/routes.rs](../src/routes.rs).

---



## 0. The one sentence to hold onto

**A hub is a** `HashMap<topic, set-of-subscribers>` **— and the entire engineering
challenge is *never holding the lock on that map while you send a message to a
subscriber*.**

Everything else in V1 is bookkeeping. If you internalize only that sentence, the
rest of this document is *why* it's true and what goes wrong when you forget it.

---



## 1. What "pub/sub" even is — and why it exists

Picture a chat server the naive way. A message arrives from Alice for "room1", and
your handler does this:

```
for each user in room1:
    user.socket.send(message)   // handler holds a direct reference to every socket
```

This works with three users on your laptop. Now ask: *how did the handler get a
reference to every user's socket?* It had to. Which means the code that receives a
publish is **coupled** to the code that owns every connection. Add a feature —
"also send to a logging sink", "also mirror to another node" — and you're editing
the publish loop to know about each new kind of receiver. The publisher knows too
much.

**Publish/subscribe is the pattern that cuts that coupling.** You insert a *broker*
(here, the `Hub`) in the middle:

```
   PUBLISHER                     HUB                        SUBSCRIBERS
  ┌─────────┐   publish(topic,  ┌──────────────────┐       ┌──────────┐
  │ Alice's │ ───── msg) ─────▶│ "room1" → {Bob,  │ ────▶│ Bob      │
  │ handler │                   │            Carol}│ ────▶│ Carol    │
  └─────────┘                   │ "room2" → {Dan}  │       └──────────┘
       ▲                        └──────────────────┘
       │ Alice does NOT hold references to Bob or Carol.
       │ She hands one message to the hub and forgets about it.
```

The publisher names a *topic*, not a *recipient*. It has no idea who is listening,
how many, or whether anyone is. Subscribers register their interest with the hub and
never learn who published. **That mutual ignorance is the feature.** It's why you can
add a subscriber (a new node, a metrics tap, an archiver) without touching a single
line of publisher code.

In this project the two sides speak through typed messages in
[src/protocol.rs](../src/protocol.rs): a client sends `ClientMessage::Publish { topic, payload }`, and the hub delivers `ServerMessage::Message { topic, payload }`
to each subscriber. The publisher constructs one `ServerMessage` and hands it over;
the hub does the rest.

> **In the wild:** this exact shape is the beating heart of Redis pub/sub, NATS
> subject routing, Phoenix PubSub, socket.io rooms, MQTT topics. Every chat and
> notification backend has a `Hub` at its center, however they dress it up.

---



## 2. The map is trivial. Here's where it gets hard.

Look at the data structure the scaffold hands you in [src/hub.rs](../src/hub.rs):

```rust
topics: RwLock<HashMap<String, HashMap<ConnId, Mailbox>>>
//              └ topic ┘  └ conn ┘  └ how to reach it ┘
```

That's it. A topic maps to *its subscribers*, and each subscriber is a `ConnId`
(a cheap process-unique id from [src/protocol.rs](../src/protocol.rs)) paired with a
`Mailbox` — the handle you push a message into to reach that connection (V2 builds
the mailbox; for now treat it as "the thing you `.deliver()` a message to").

If only one task ever touched this map, you'd be done in ten minutes. But a real
server holds **thousands of open sockets**, each driven by its own async task (see
`handle_socket` in [src/routes.rs](../src/routes.rs) — one spawned reader loop *per
connection*). At any instant, hundreds of those tasks may be calling `subscribe`,
`publish`, and `disconnect` **on the same map, concurrently.** Concurrent mutation of
a `HashMap` is a data race — instant undefined behavior — so the map lives behind a
lock. And *how you use that lock* is the whole game.

Let's derive the two failure modes from scratch.

---



## 3. Failure mode #1 — the frozen hub

Here is the seductive, wrong implementation of `publish`:

```
publish(topic, msg):
    guard = topics.write()          // take the lock
    for (conn, mailbox) in guard[topic]:
        mailbox.deliver(msg)        // ... and SEND while still holding it
    // lock released here
```

Reads fine. Now trace what happens when **one** of those subscribers is slow.

A "slow" subscriber isn't exotic — it's a phone on hotel wifi, a laptop whose lid
just closed, a client that stopped calling `recv()`. When you `deliver` to it, that
send can *block* (or at least take real time) because the client's buffer is full and
isn't draining. Here's the timeline, and it's brutal:

```
 time │ what's happening
──────┼───────────────────────────────────────────────────────────────
  t0  │ Task P calls publish("room1", msg). Takes the WRITE lock. 🔒
  t1  │ Delivers to Bob (fast).   ✅
  t2  │ Delivers to Carol — Carol is on hotel wifi. deliver() STALLS.
  t3  │ ...still stalled. P is inside the loop, STILL HOLDING THE LOCK.
  t4  │ Task Q wants to publish to "room2" (totally unrelated topic).
      │   → blocks on topics.write(). 🔒 held by P.
  t5  │ Task R wants to subscribe a brand-new client.
      │   → blocks on the same lock. 🔒
  t6  │ Dan wants to unsubscribe. → blocks. 🔒
      │
      │ The ENTIRE hub is frozen behind one slow phone in room1.
      │ room2, room3, new connections, everyone — all waiting on Carol.
```

**One slow client froze the whole server.** Not room1 — *everything*. This is the
single most important failure mode in the project, and it's why the scaffold's
`publish` comment shouts:

> *Take a READ lock, clone out the mailboxes you need to reach, then RELEASE the lock
> before delivering — never* `deliver` *while holding it.*

The fix in principle (you'll write the code): the lock protects the *map*, not the
*sending*. Hold it just long enough to **copy out** who you need to reach, let it go,
and *then* do the slow work of delivering — outside the lock, where a stall hurts
only that one delivery:

```
 time │ the disciplined version
──────┼───────────────────────────────────────────────────────────────
  t0  │ P takes the READ lock. 🔒
  t1  │ Snapshots room1's mailboxes into a local Vec. Releases lock. 🔓
  t2  │ Delivers to Bob ✅, then to Carol — Carol stalls...
  t3  │ ...but the map is UNLOCKED. Q publishes to room2 ✅. R subscribes ✅.
      │ Carol's slowness is now Carol's problem alone.
```

Notice this also motivates why the field is an `RwLock`, not a `Mutex`: publishes
only *read* the map (snapshot subscribers), so many publishes to different topics can
proceed in parallel — they only contend with the rarer *writes* (subscribe /
unsubscribe / disconnect). That's a hint, not a mandate; §6 lays out the full menu.

> There's a second, subtler reason to snapshot-then-send even beyond speed: if you
> held the lock across `deliver`, and `deliver` itself ever needed to touch the hub
> (it doesn't here, but in richer designs it might), you'd **deadlock** re-entering a
> lock you already hold. "Do I/O outside the lock" is a discipline that keeps paying
> off.

---



## 4. Failure mode #2 — the leak that never surfaces in testing

Sockets don't close politely. A tab is killed, a network vanishes, a process is
OOM-ed. When a connection ends **for any reason**, it must disappear from *every*
topic it ever joined. Miss one path and you get a leak that's invisible until it
isn't.

Trace the bug. Bob joins "room1", "room2", "room3", then his laptop lid closes:

```
BEFORE Bob's socket dies:            AFTER, if disconnect is incomplete:
  room1 → {Bob, Carol}                 room1 → {Bob💀, Carol}   ← Bob is a ghost
  room2 → {Bob, Dan}                   room2 → {Bob💀, Dan}     ← still "subscribed"
  room3 → {Bob}                        room3 → {Bob💀}          ← topic never empties
```

Now every publish to room1 tries to `deliver` to a dead mailbox (wasted work, and
depending on your code, an error to handle on the hot path). room3 has *zero live
subscribers* but never gets pruned, so the map grows forever — one dead topic per
abandoned room, for the life of the process. This is a memory leak with a slow fuse:
your 10-minute test passes, your server falls over at 3 a.m. after a week.

The defense is twofold, and both halves live in
[src/routes.rs](../src/routes.rs)'s teardown:

```rust
// Teardown — runs no matter how we exited (clean close, error, abrupt drop).
state.hub.disconnect(conn);
state.presence.disconnect(conn);
```

1. **Every exit path funnels through** `disconnect`**.** The reader loop in
  `handle_socket` is structured so that whether the client sent a clean `Close`,
   errored, or the writer task died, control falls out of the loop to those two
   lines. You get this for free *if* your `disconnect` actually does its job.
2. **`disconnect(conn)` must remove `conn` from every topic and prune emptied
  topics** — that's the `todo!()` you'll fill in [src/hub.rs](../src/hub.rs). And it
   must be **idempotent**: calling it twice (clean leave *then* teardown) must not
   panic or corrupt anything.

There's a real cost question buried here, flagged in the scaffold: naively,
`disconnect` scans *every* topic to find the ones this conn is in — O(number of
topics). With 100k topics and a reconnect storm, that's brutal. A reverse index
(`conn → its topics`) makes it O(*this conn's* topics). That's a design decision,
not a requirement — §6.

---



## 5. A full worked trace

Let's run one concrete sequence through the disciplined hub so the pieces click.
Three connections, `ConnId`s minted in order by [src/protocol.rs](../src/protocol.rs):


| Step | Call                                      | Map state after                                    |
| ---- | ----------------------------------------- | -------------------------------------------------- |
| 1    | `subscribe("room1", conn-1, mbox1)`       | `room1 → {1}`                                      |
| 2    | `subscribe("room1", conn-2, mbox2)`       | `room1 → {1, 2}`                                   |
| 3    | `subscribe("room2", conn-1, mbox1)`       | `room1 → {1,2}`, `room2 → {1}`                     |
| 4    | `publish("room1", msg)` → returns **2**   | snapshot {mbox1, mbox2}, deliver to both, count 2  |
| 5    | `subscribe("room1", conn-2, mbox2)` again | `room1 → {1, 2}` — **idempotent**, still 2, no dup |
| 6    | `unsubscribe("room1", conn-1)`            | `room1 → {2}`, `room2 → {1}`                       |
| 7    | `publish("room1", msg)` → returns **1**   | only conn-2 left in room1                          |
| 8    | `disconnect(conn-1)`                      | `room1 → {2}`, `room2` **pruned** (emptied)        |
| 9    | `publish("room2", msg)` → returns **0**   | topic gone; must not panic, returns 0              |


Two things this trace pins down, both of which are Done-when criteria:

- `publish` **returns a count** (step 4 → 2, step 7 → 1, step 9 → 0). That number is
gold: it's your fan-out metric, your "did anyone hear me?" debug signal, and it's
cheap to compute since you're already iterating.
- **Empty topics get pruned** (step 8: room2 vanishes when conn-1, its last member,
leaves). Prune eagerly and the map only ever holds *live* rooms.

---



## 6. The design space (this is the actual V1 decision)

The scaffold deliberately gives you *one reasonable starting point* — a single
`RwLock` over the whole map — and tells you it's "not a mandate". Choosing where on
this spectrum to land **is** the vertical. Here's the menu:


| Design                                                      | Publishes to *different* topics…   | Cost / complexity                                                              | When it's right                                                                     |
| ----------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------- |
| One `Mutex<HashMap>`                                        | serialize (one at a time)          | dead simple                                                                    | tiny scale, or if you truly snapshot-then-send so the lock is held for microseconds |
| One `RwLock<HashMap>` *(scaffold default)*                  | run in parallel (shared read lock) | simple; writes still exclusive                                                 | read-heavy fan-out — publishes vastly outnumber subs/unsubs                         |
| Per-topic locks (`HashMap<String, Mutex<Set>>`)             | fully independent                  | more moving parts; the *outer* map still needs protection to add/remove topics | many hot topics contending                                                          |
| Sharded map (e.g. `DashMap`, or N stripes by `hash(topic)`) | independent across shards          | more complex; harder to reason about                                           | very high topic cardinality                                                         |


The honest answer for this project: a single `RwLock` where you **snapshot under the
read lock and send outside it** is enough to pass every V1 test, *because the lock is
held only for the microseconds it takes to clone a* `Vec` *of mailboxes.* The fancier
options matter when even that snapshot contends — which you can only know by
measuring (that's what the project's `bench/` is for). Don't reach for `DashMap`
because it sounds scalable; reach for it because a flame graph told you to.

**The trap to avoid:** "why not just use `tokio::sync::broadcast` and delete this
whole file?" It's worth understanding *why the SPEC makes you build it by hand.* A
`broadcast` channel has fixed per-topic buffering and *its own* lossy semantics baked
in; it decides the slow-consumer policy *for* you. V2 is entirely about *you* owning
that policy per-connection. Building the hub yourself is what lets V2 exist. (Keep
this as a depth probe: what exactly does `broadcast` fix that you might not want?)

---



## 7. Mental model — looks-like vs actually-is


| It looks like…                                 | It actually is…                                                                          |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------- |
| "A hub is a data-structure problem."           | A concurrency and lock-discipline problem. The map is 10 lines.                          |
| "Hold the lock while delivering — it's safer." | The fastest way to freeze the whole server behind one slow client.                       |
| "`publish` just loops and sends."              | Snapshot-under-lock, release, *then* send. Two distinct phases on purpose.               |
| "`disconnect` on clean close is enough."       | Every exit path (error, abrupt drop, shutdown) must reach it, and it must be idempotent. |
| "Empty topics are harmless."                   | Unpruned dead topics are an unbounded memory leak with a slow fuse.                      |
| "Use `broadcast` and move on."                 | That surrenders the V2 backpressure policy you're here to own.                           |


---



## 8. Where you'll build this

Four `todo!()`s in [src/hub.rs](../src/hub.rs), each a method on `Hub`:

- `[subscribe(topic, conn, mailbox)](../src/hub.rs)` — insert into the topic's set;
create the inner map for a brand-new topic; **idempotent** on re-subscribe.
- `[unsubscribe(topic, conn)](../src/hub.rs)` — remove; **prune** the topic if now
empty.
- `[publish(topic, msg)](../src/hub.rs)` — **snapshot under the lock, release, then
deliver**; return the count reached. This is the method the whole doc is about.
- `[disconnect(conn)](../src/hub.rs)` — remove `conn` from **every** topic; prune
emptied ones; idempotent.

The plumbing that calls these is already done for you in
[src/routes.rs](../src/routes.rs) (`dispatch` → `subscribe`/`unsubscribe`/`publish`;
teardown → `disconnect`), so you can run the server and watch a `publish` panic with
`todo!("V1: fan-out…")` — that panic *is* your worklist.

**This doc unlocks these V1 "Done when ALL true" boxes:**

- [ ] `subscribe` / `unsubscribe` / `publish` work, and `publish` reports how many
  subscribers it reached. *(§5 trace)*
- [ ] `disconnect(conn)` removes the connection from **every** topic — no leaked
  entries, no empty topics growing forever. *(§4)*
- [ ] The hub **never holds its lock while sending**, so one slow client can't freeze
  publishes to everyone else. *(§3 — the heart of it)*
- [ ] Concurrent subscribe/publish/disconnect leaves **no dangling subscriber** and
  never delivers to a closed socket. *(§4, §6 — this is what the concurrency test
  proves)*

**The trap when testing** (from `CONCEPTS.md`): fast local clients hide every bug in
this file. The freeze in §3 and the leak in §4 only appear with a *slow* or *dead*
receiver — which is exactly why the V1 proof asks for a concurrency test with a
stalled receiver, and why V2 exists. Test with a client that *stops reading*.

---

*Next: once the hub delivers, [what happens when a subscriber can't keep up?](01-backpressure-and-the-slow-consumer.md) — that's V2, and it's the other half of the "slow client" story this doc kept pointing at.*
