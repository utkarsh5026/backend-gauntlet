# The Jitter Buffer: Smoothness Bought with Latency — From First Principles

> Why every real-time receiver deliberately *delays* playback, how a small
> bounded wait turns a chaotic arrival into a smooth stream, and why the
> 16-bit sequence wrap is the classic bug. No prior knowledge assumed.
>
> Prepares you for **V2** in [SPEC.md](../SPEC.md). Anchored to
> [jitter.rs](../src/jitter.rs) (`JitterBuffer::insert`, `pop_frame`,
> `missing` — your `todo!()`s) and the receiver loop in
> [session.rs](../src/session.rs) that calls them.

---

## 0. The one sentence to hold onto

**A jitter buffer is a purchase: every millisecond of deliberate delay buys a
millisecond of network variance absorbed — and costs a millisecond between
the sender's camera and the receiver's eye.** The entire design is choosing
how much to buy, and refusing to pay forever for a packet that isn't coming.

---

## 1. The problem: the network delivers *when it feels like it*

The sender transmits packets on a metronome. The network does not deliver
them on one. Here's a real-shaped trace — packets sent exactly 20 ms apart
through a path with variable queuing (the boss fight injects 30 ms ± 10 ms of
exactly this):

```
   sent:      p1 ──── p2 ──── p3 ──── p4 ──── p5 ──── p6
   t(ms):     0       20      40      60      80      100

   arrived:   p1      p2         p4  p3       p5              p6(twice!)
   t(ms):     32      51         73  79       112             139,141
   delay:     32      31         (p4:33)      32              39
                                 (p3:39)
```

Same stream, four different sins in 140 ms: **jitter** (delays of 31–39 ms),
**reordering** (p4 beat p3), **duplication** (p6 twice — retransmits and
route flaps do this), and if p3 had never shown up, **loss**. Now consider
the naive strategies:

| Strategy | What the viewer sees |
| --- | --- |
| **Play on arrival** | p2 plays 19 ms after p1, p3 plays 6 ms after p4 *and out of order* — stutter, judder, corrupt decode order. Motion looks like a flip-book in a wind tunnel. |
| **Wait for every packet in order** (TCP's posture) | The first true loss stalls playout *forever* (or until a retransmit lands, hundreds of ms later). One bad packet freezes the call. |

Both extremes fail. The jitter buffer is the instrument that lives between
them, and its knob — the **target delay** — is the explicit price you pay.

## 2. The idea: hold briefly, release in order, give up on schedule

The mechanism, end to end:

```
                        ┌───────────────────────────────┐
   network ──insert()──▶│    held, ordered by sequence  │──pop_frame()──▶ decoder
   (chaotic)            │  ┌────┬────┬────┬─gap─┬────┐  │  (metronomic)
                        │  │ 41 │ 42 │ 43 │ 44? │ 45 │  │
                        │  └────┴────┴────┴─────┴────┘  │
                        │   each waits target_delay,    │
                        │   then leaves in order        │──missing()──▶ [44] → V3 NACKs it
                        └───────────────────────────────┘
```

Three verdicts greet every arriving packet (Card 2's "three timing
verdicts"), and each increments a specific counter in `JitterStats`:

| Verdict | Condition | Action | Counter |
| --- | --- | --- | --- |
| **On time** | New sequence, not yet due for playout | Insert, ordered by sequence | `buffered_packets` |
| **Duplicate** | This sequence is already held (or already played) | Discard | `duplicates` |
| **Too late** | Its playout moment already passed — we skipped or concealed it | Discard | `late` |

And one verdict on the release side: a gap whose wait has expired is
**skipped** (`skipped` counter), because "wait a little, then give up" is the
only posture that neither stutters nor stalls. The scaffold's `pop_frame`
rustdoc states the contract precisely: release the next *complete frame*
(consecutive packets up to a marker — V1's framing invariants earn their keep
here) once the head has aged past `target_delay`; past the window with an
unfilled gap, skip it so the buffer **never stalls forever**.

Follow the trace from §1 through a 60 ms target delay: p1 arrives at t=32 and
is scheduled for t≈92; p2 (t=51) for t≈112; p3, despite arriving *after* p4
at t=79, plays in correct order at t≈132. All the chaos happened *inside the
window*; the decoder never saw it. That's the trick in one sentence: **the
buffer converts delay variance into constant delay.**

## 3. The wrap: why ordering by raw sequence is a time bomb

The sequence number is 16 bits: after 65535 comes **0**. Order packets by the
raw `u16` and the stream works perfectly… until the wrap, where:

```
   actual order:    65534  65535    0      1
   sorted as u16:     0      1    65534  65535    ← 0 and 1 sort FIRST
```

Packets from the *future* sort before packets from the *past*. Depending on
your release logic, the buffer either plays them wildly early, or treats
65534/65535 as impossibly late and drops them. And here's why this is the
classic bug ([CONCEPTS.md](../CONCEPTS.md) Card 2's trap): at ~1.5 Mbps with
~1200-byte packets you send ~156 packets/s, so the wrap arrives after
65536 / 156 ≈ **420 seconds — seven minutes in**. Every short test passes.
The demo works. The hour-long call breaks "randomly" at minute seven. (The
scaffold randomizes the initial sequence, so it can also break in the first
minute — a kindness, honestly.)

The cure is **unwrapping**: map the repeating 16-bit value onto an
ever-increasing 64-bit index by tracking which "lap" you're on:

```
   raw (u16):        65534   65535   0       1       2
   unwrapped (u64):  65534   65535   65536   65537   65538   ← monotonic
```

The scaffold has already committed to this shape — `packets:
BTreeMap<u64, …>` is *keyed by the unwrapped sequence*, with `base_sequence`
as the anchor and `highest` tracking the frontier. What it deliberately does
not tell you: how to decide, for an incoming raw `u16`, whether it belongs to
the current lap, the next one (a wrap just happened), or the previous one (a
straggler from before the wrap). That decision — a comparison in a circular
space where "ahead" and "behind" are ambiguous — is the interesting part of
`insert`, and it's yours. (`nack_packs_across_wrap` in V3 faces the same
circular-space reasoning; solve it well once.)

## 4. Sizing the window: the RFC 3550 jitter estimate

A fixed target delay is wrong on every network except the one you tested on:
too small on hotel Wi-Fi (stutter), needlessly laggy on a LAN. To size the
window — or just to *see* whether it's sized right — you measure the jitter
you're actually receiving.

RFC 3550's estimator compares, for each pair of packets, the spacing at
arrival vs. the spacing at send (which you know from the RTP timestamp —
media clock, doc [01](01-rtp-making-a-stream-out-of-datagrams.md)):

```
   D = (arrival_now − arrival_prev)  −  (timestamp_now − timestamp_prev)
       └── wire spacing ──┘             └── intended spacing ──┘

   J ← J + (|D| − J) / 16        ← exponentially smoothed magnitude
```

If the network were perfectly steady, every D would be 0. Each packet's |D|
is one sample of "how much the network deviated"; the /16 smoothing means one
outlier barely moves J, but a sustained shift shows up within a couple dozen
packets. Note the units trap: both spacings must be in the *same* clock, and
the RFC keeps J in **clock-rate ticks** — `JitterStats.jitter` says so
explicitly, and at 90 kHz, 30 ms of jitter reads as **2700 ticks**, not 30.
This J is also exactly what V3's Receiver Report carries back to the sender,
and what the `/metrics` jitter gauge exposes.

## 5. The design space you own

The scaffold fixes the interfaces; these choices are the vertical:

- **The unwrap decision** (§3) — current lap, next lap, or last lap? Your
  comparison rule, your tie-breaks.
- **The release policy** in `pop_frame` — how you find "the next complete
  frame", when a head gap's clock starts, what "aged past target_delay"
  anchors to. The Done-when bound: added latency stays within the configured
  budget (the boss fight caps it at **≤ 150 ms** against 30 ms p95 network
  jitter).
- **Static vs. adaptive target delay** — a static target can pass V2; an
  adaptive one (grow when J rises, shrink when calm) is what production
  stacks like WebRTC's NetEq do, with time-stretching to hide the
  adjustments (Card 2's depth probe). Either way, `docs/14-design.md` must
  record the policy.
- **The capacity policy** — `JITTER_CAPACITY = 4096` packets is wired in
  [session.rs](../src/session.rs) (≈ 6 MB worst case at MTU-sized packets:
  the OOM guard). *What* you evict when a flood hits the cap — newest?
  oldest? — decides whether an attacker flooding future sequences can push
  out legitimate packets. Hostile-peer thinking, on a buffer.

## 6. Mental-model summary

| Concept | Hold onto |
| --- | --- |
| What the buffer *is* | A variance-to-constant-delay converter; the target delay is the price |
| Too small / too large | Stutter (packets miss their slot) / lag (conversation drifts apart) |
| The three insert verdicts | On-time → hold; duplicate → count & drop; late → count & drop |
| The release rule | In order, complete frames, after target_delay — then *give up* on gaps |
| The wrap | Raw u16 ordering breaks at 65535→0 (~7 min in at 1.5 Mbps); unwrap to a monotonic u64 |
| The jitter estimate | Smoothed \|arrival spacing − timestamp spacing\|, in ticks; sizes the window, feeds RR + metrics |
| The gap list | `missing()` is V3's shopping list — this buffer *finds* losses, V3 *buys them back* |

**Where you'll build this:** the three `todo!()`s in
[jitter.rs](../src/jitter.rs) — `insert`, `pop_frame`, `missing`. They unlock
V2's five **Done when ALL true** boxes: reordering across the wrap, de-dup,
paced complete-frame release within the latency bound, late/lost handling
that never stalls, and gap reporting + the jitter estimate. The receiver loop
in [session.rs](../src/session.rs) already calls all three on its playout and
feedback ticks — the moment `insert` stops panicking, packets start flowing
through your buffer. `/hint 14` when stuck; `/quest` to build it against
acceptance tests.
