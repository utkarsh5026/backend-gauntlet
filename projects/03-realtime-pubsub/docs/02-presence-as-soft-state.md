# Presence as Soft State — From First Principles

> A ground-up guide to "who's online?" — a question that sounds like a simple set
> but is secretly one of the deepest problems in distributed systems. We derive
> *soft state*, *heartbeat + TTL*, and why **detecting absence** is fundamentally
> harder than detecting a departure. No prior knowledge of distributed systems,
> eventual consistency, or TCP assumed.
>
> Prepares you for **V3** (`src/presence.rs`). Anchored to real code:
> [src/presence.rs](../src/presence.rs), [src/protocol.rs](../src/protocol.rs),
> [src/routes.rs](../src/routes.rs), [src/hub.rs](../src/hub.rs).

---

## 0. The one sentence to hold onto

**A presence list is *always* wrong some of the time — a laptop lid closing sends no
goodbye — so the job isn't to be correct, it's to *converge*: choose how wrong, for
how long, and in which direction.**

Presence is **soft state**: an approximation you continuously repair, not a fact you
store.

---

## 1. The seductively simple version — and the crack in it

"Who's in room1?" Obviously a set. Add on join, remove on leave:

```
join("room1", Bob)   → room1 members = {Bob}
join("room1", Carol) → room1 members = {Bob, Carol}
leave("room1", Bob)  → room1 members = {Carol}
```

The scaffold in [src/presence.rs](../src/presence.rs) hands you exactly this shape:

```rust
members: RwLock<HashMap<String, HashMap<ConnId, String>>>
//                       └topic┘  └conn┘  └ display identity ┘
```

and the two easy methods, `join` (on subscribe) and `leave` (on unsubscribe), wired
from `dispatch` in [src/routes.rs](../src/routes.rs). If every client politely said
goodbye, you'd be done. **They don't.** That's the entire vertical.

---

## 2. The crack, made concrete: absence has no event

Here's the asymmetry that makes presence hard. Compare two ways Bob leaves room1:

```
 CLEAN LEAVE (easy):                    ABRUPT DROP (hard):
   Bob clicks "leave"                     Bob's laptop lid closes / wifi dies
   → client sends {unsubscribe}           → client sends ... nothing. Ever.
   → server runs leave("room1", Bob)      → server runs ... nothing.
   → Bob removed immediately ✅            → Bob lingers as a GHOST 👻
```

A departure is an **event** — a frame arrives, you react. An *absence* is the
**absence of events** — and you cannot write `on_nothing_happens()`. Nothing arrives
to tell you Bob is gone. This is the crux the SPEC states directly:

> *"detecting absence" (TTL/heartbeat) is fundamentally harder than detecting a clean
> leave.*

"But won't TCP tell us the socket died?" **Eventually — and eventually can be
minutes.** TCP was designed to survive transient network blips, so a peer that
vanishes silently isn't declared dead until keepalive probes time out, which by
default is on the order of *minutes to over an hour*. For a "who's online" dot that's
useless: Bob's avatar would glow green for two hours after he's gone. You cannot lean
on the transport to detect absence in human time.

So you're stuck: some of the time, your member set contains people who left without
saying so. The set is *wrong*. Accept that — and engineer the wrongness.

---

## 3. The reframe: soft state

**Hard state** is a fact you're responsible for keeping exactly right (a bank
balance; lose it and you've lost money). **Soft state** is an approximation that's
allowed to be stale and is *continuously refreshed from a source of truth* — if you
lose it, it rebuilds itself.

Presence is soft state. The source of truth is "is this client actually still there
and talking to me?" and you sample that truth periodically. The design question stops
being *"how do I keep the set perfectly correct?"* (impossible) and becomes:

> *"How stale am I willing to be, and how do I guarantee I converge back to truth
> within that bound?"*

That reframe is the whole unlock. You're not chasing correctness; you're **bounding
staleness.**

> **In the wild — and go look:** Discord and Slack online indicators lag reality by
> ~30–60 seconds (close your laptop, watch a friend see you online for a bit). Redis
> key TTLs *are* a presence mechanism. Kubernetes decides a node is dead from missed
> heartbeats. Zookeeper/etcd "ephemeral" nodes vanish when a session's heartbeats
> stop. Every one of these picked a staleness bound and lives with it.

---

## 4. The mechanism: heartbeat + TTL

If absence produces no event, **manufacture** a steady stream of events whose
*absence* becomes detectable. That's a heartbeat:

1. **Heartbeat.** The client periodically proves it's alive — a ping frame, or any
   traffic. Each proof **refreshes a per-member "last seen" timestamp.**
2. **TTL (time-to-live).** A member not refreshed within a window `T` is *presumed
   gone.*
3. **Sweep.** A periodic task walks the members, evicts anyone whose last-seen is
   older than `T`, and broadcasts the resulting change.

Now "Bob went silent" *does* have a detectable consequence: his timestamp stops
advancing, crosses the TTL, and the sweep reaps him. You converted a non-event into a
timeout.

```
   t=0s   Bob heartbeats → last_seen[Bob]=0
   t=10s  Bob heartbeats → last_seen[Bob]=10
   t=15s  Bob's lid closes 💤  (no more heartbeats, no leave)
   t=20s  sweep: now=20, last_seen=10, age=10 < TTL(30) → Bob stays (still a ghost)
   t=50s  sweep: now=50, last_seen=10, age=40 > TTL(30) → EVICT Bob 👻→∅, broadcast
          Bob was a ghost for ~35s. That 35s is the price of TTL=30. Chosen, not accidental.
```

The scaffold points right at this as the stretch, and tells you the storage
consequence:

> *if you add heartbeats you'll want to store a last-seen `Instant` alongside the
> identity so a sweep can expire stale entries.* — [src/presence.rs](../src/presence.rs)

i.e. the inner `HashMap<ConnId, String>` grows a timestamp: `HashMap<ConnId, (String,
Instant)>`. The sweep is a `tokio` task on an interval. (Building it is the vertical;
`/hint` if you want graduated nudges.)

---

## 5. The one tradeoff that defines the system: how long is T?

TTL is a single dial with tension at both ends:

| TTL `T` | Absence detected in… | The failure it courts |
|---------|----------------------|-----------------------|
| **Short** (e.g. 5s) | ~5–10s (snappy!) | **False offlines / flapping** — one dropped heartbeat on a brief network hiccup and a *present* user blinks offline, then back. Green dot strobes. |
| **Long** (e.g. 120s) | ~2min (ghosts linger) | **Stale ghosts** — people who left glow online for minutes. "Bob's online, why won't he answer?" |

There's no free lunch — you're trading *detection latency* against *false-positive
rate.* The right `T` depends on heartbeat interval and expected network jitter (a
common rule of thumb: `T` = a small multiple of the heartbeat interval, so a single
lost beat doesn't evict a live user). This *is* the design decision the SPEC wants you
to reason about and record in the design doc — not a value to copy from here.

---

## 6. The other trap: presence broadcasts amplify

Presence changes aren't silent — the SPEC wants joins/leaves pushed live as their own
`ServerMessage::Presence { topic, members }` (already defined in
[src/protocol.rs](../src/protocol.rs)) so rooms update in real time. Fine for a
5-person room. Now do the arithmetic for a big one:

```
   A 10,000-member room. One person joins.
   → presence changed → broadcast the new member list to ALL members
   → 10,000 messages sent, each carrying a ~10,000-entry list
   → and it's a THUNDERING HERD: if 500 people join in the same second
     (everyone opens the app at 9:00am), that's 500 × 10,000 = 5,000,000 sends/s
     of presence traffic alone — before a single chat message.
```

A naive "broadcast full member list on every change" turns a popular room into a
self-inflicted DoS. This connects straight back to
[backpressure (V2)](01-backpressure-and-the-slow-consumer.md): those 5M sends land in
bounded mailboxes and start shedding. The mitigations are a design conversation, not a
required build: **batch** presence changes over a short window, send **deltas**
("+Bob", "−Carol") instead of the full list, send **digests** ("+3 members") to large
rooms, or simply **don't broadcast** per-member presence above some room size and let
clients poll `members()`. Know the cost; pick deliberately.

---

## 7. The ghost-member bug: close every exit path

The bug that *will* bite you: a member who lingers after their socket died. It comes
from forgetting a disconnect path — the exact same discipline as
[the hub's leak (V1 §4)](00-the-fan-out-hub-and-lock-discipline.md), now for presence.
Look at the teardown in [src/routes.rs](../src/routes.rs):

```rust
state.hub.disconnect(conn);
state.presence.disconnect(conn);   // ← V3's abrupt-drop safety net
```

`presence.disconnect(conn)` is the `todo!()` that removes `conn` from *every* topic it
was present in — the catch-all that runs no matter *how* the socket ended (clean
close, error, abrupt drop, writer death). The scaffold names it as *"the reason a
'ghost' lingers in a room after someone's tab dies"* if you forget it. Even without
the heartbeat stretch, wiring `disconnect` correctly removes ghosts on any drop the
*server* notices immediately (a TCP reset, a write error). The heartbeat+TTL sweep is
the backstop for the drops the server *doesn't* notice (silent vanish). You want both:
`disconnect` for observed deaths, TTL for silent ones.

---

## 8. Mental model — looks-like vs actually-is

| It looks like… | It actually is… |
|----------------|-----------------|
| "Presence is a set you keep correct." | Soft state you keep *converging* — it's wrong some of the time, by design. |
| "Remove on leave and you're done." | Clean leave is the easy 10%; silent absence (no event) is the real problem. |
| "TCP will tell me the socket died." | Eventually — minutes later. Useless for a live presence dot. |
| "Just pick a short TTL for accuracy." | Short TTL flaps live users offline on one lost heartbeat. It's a tradeoff, not a max. |
| "Broadcast the member list on every change." | In a 10k room that's a thundering-herd amplifier — batch/delta/digest or don't. |
| "Return the set to product code as truth." | Soft state consumed as hard state → double-notifications, missed pushes. Publish the staleness contract. |

---

## 9. Where you'll build this

Four `todo!()`s in [src/presence.rs](../src/presence.rs), plus one stretch:

- [`join(topic, conn, identity)`](../src/presence.rs) — record/refresh membership
  (on subscribe).
- [`leave(topic, conn)`](../src/presence.rs) — remove; prune empty topics (on
  unsubscribe).
- [`members(topic)`](../src/presence.rs) — snapshot the identities present, for a
  `Presence` broadcast; empty (not a panic) for an unknown topic.
- [`disconnect(conn)`](../src/presence.rs) — the abrupt-drop catch-all: remove from
  **every** topic. *This is the anti-ghost method.*
- [*stretch*: heartbeat + TTL sweep](../src/presence.rs) — store last-seen, refresh on
  ping, evict on a timer.

**Security note the scaffold flags** (see `dispatch` in
[src/routes.rs](../src/routes.rs)): today identity is `conn.to_string()`, a
placeholder. Derive the *real* display identity from the authenticated token, never
trust the client's claimed identity for anything but display — that's the "never trust
`identity` from the client" horizontal item.

**This doc unlocks these V3 "Done when ALL true" boxes:**

- [ ] Per-topic membership tracks join / leave / removal on **every** disconnect path
  (including abrupt drop). *(§7)*
- [ ] Absence handled via **heartbeat + TTL**: an entry not refreshed within the
  window is swept. *(§4, §5)*
- [ ] Presence changes are published as their own server messages so rooms update
  live. *(§6 — mind the amplification)*
- [ ] An abrupt drop **still leaves the room** — no ghost members linger. *(§7 —
  proven by dropping a socket with no clean leave and asserting the member disappears
  within the TTL)*

---

*Previous: [backpressure](01-backpressure-and-the-slow-consumer.md) (V2) — presence
broadcasts flow through those same bounded mailboxes. Next:
[one logical room across many servers](03-multi-node-fan-out-and-the-echo-loop.md) —
V4, where presence and fan-out both have to cross process boundaries.*
