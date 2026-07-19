# Why Real-time Media Abandons TCP — From First Principles

> The framing idea for this whole project: why a video call runs on UDP, the
> protocol that promises *nothing*, instead of TCP, the protocol that promises
> *everything*. No prior networking knowledge assumed.
>
> This is Card 0 of [CONCEPTS.md](../CONCEPTS.md) — the idea every vertical
> (V1–V4 in [SPEC.md](../SPEC.md)) is a consequence of. Read this one first.

---

## 0. The one sentence to hold onto

**For real-time media, a packet that arrives late is not late data — it is
useless data.** The moment it described has already been shown (or skipped).
Once you accept that *deadlines*, not *delivery*, define correctness, TCP's
guarantees flip from a safety net into a liability — and every mechanism in
this project is you building back *only* the reliability that pays for itself
before the deadline.

---

## 1. What TCP actually promises (and what it costs)

TCP gives every application the same three guarantees, whether it wants them
or not:

1. **Every byte arrives.** Lost packets are retransmitted until acknowledged.
2. **Bytes arrive in order.** The receiver's kernel holds packet #5 hostage
   until #4 shows up, even if #5 arrived first.
3. **The sender slows down** when the network is congested.

For a file download, this is perfect: you cannot open half a zip file, so
"every byte, eventually" is exactly the contract you want. Projects 11–13
leaned on this — HTTP over TCP meant a player could buffer its way over any
network bump.

But look at what guarantee #2 costs when packets are a *live video call*:

```
   wire order sent:      [pkt 4] [pkt 5] [pkt 6] [pkt 7]
   pkt 4 is lost.
   pkt 5, 6, 7 arrive fine — they are sitting in the receiver's kernel.

   TCP's contract: the app may not see 5, 6, 7 until 4 is retransmitted.

   timeline:   0ms      50ms          ~250ms+
               sent     5,6,7 arrive  4 finally retransmitted & delivered
                          │                     │
                          └── three perfectly ──┘
                              good packets held hostage
```

One lost packet stalls delivery of *everything behind it* — including data
that already arrived. This is called **head-of-line blocking**, and it happens
at the transport layer where the application can't see or override it. The
retransmit takes at least one round trip (often much more, with timers and
backoff); meanwhile the call freezes. And by the time packet 4 arrives, the
moment it belonged to — a 33 ms video frame — is long gone.

## 2. The deadline changes everything

Here is the table that separates the two worlds. Same network events, opposite
verdicts:

| Network event | File download (TCP's world) | Video call (this project's world) |
| --- | --- | --- |
| A packet is lost | Retransmit it, whenever. The file is incomplete without it. | *Maybe* recover it — but only if it can arrive before its frame's playout moment. Otherwise, forget it. |
| A packet arrives 400 ms late | Great, slot it in. | Useless. Its frame was due 370 ms ago; the viewer already saw a skip or a concealment. |
| Packets arrive out of order | Kernel reorders silently. Cost: memory. | Reorder *briefly* — but waiting too long for a straggler is itself a stall. |
| Sender exceeds link capacity | TCP throttles automatically. | Nobody throttles you. Your own packets build a queue, delay balloons, then loss cascades. |

The download's definition of success is *completeness*. The call's definition
of success is *timeliness*: **≥ 99.5% of frames played on time, no stall
longer than 300 ms** — that is literally a boss-fight criterion in
[SPEC.md](../SPEC.md). A transport that trades unbounded latency for perfect
completeness optimizes exactly the wrong axis.

## 3. So: UDP, which gives you nothing

UDP is the other transport the internet offers, and its feature list is short:
a datagram you `send_to` *may* arrive at the far socket. That's it. No
ordering, no retransmission, no duplicate suppression, no pacing. In exchange,
nothing is ever held hostage: every datagram that arrives is handed to your
application the instant it lands.

That trade sounds terrible until you notice what it really means: **all the
policy decisions TCP made for you are now yours to make** — and you can make
them *per packet, against a deadline*, which TCP structurally cannot.

The price is that you now own four failure modes the kernel used to hide:

| Failure mode | What the network does | Who handles it here |
| --- | --- | --- |
| **Loss** | Drops your datagram silently | V3 — NACK-based selective retransmission ([rtcp.rs](../src/rtcp.rs)), for the losses that still matter |
| **Reordering** | Delivers 7 before 6 | V2 — the jitter buffer reorders by sequence ([jitter.rs](../src/jitter.rs)) |
| **Duplication** | Delivers the same datagram twice | V2 — the jitter buffer de-dups ([jitter.rs](../src/jitter.rs)) |
| **Jitter** | Varies the delay packet-to-packet | V2 — the playout delay absorbs it ([jitter.rs](../src/jitter.rs)) |
| *(and the fifth)* **Congestion** | Drops more and delays more when you send too fast | V4 — bandwidth estimation + pacing ([congestion.rs](../src/congestion.rs)) |

And one prerequisite before any of that is possible: a bare datagram doesn't
even tell you *which packets belong together, in what order, at what moment
they should play*. That metadata is V1 — the RTP header
([rtp.rs](../src/rtp.rs)) — the thin layer that turns lonely datagrams into a
*stream*.

## 4. Reliability as an economic decision

The deepest idea in this project, stated once here and rebuilt concretely in
V3: on UDP, **reliability is not a property of the transport — it is a
per-packet purchase**, and each purchase has a price and a deadline.

Recovering a lost packet costs (at minimum) one round trip: the receiver
notices the gap, sends a NACK back, the sender retransmits. On a path with
30 ms each way, that's ≥ 60 ms. So for every gap the receiver asks itself:

```
   is (time until this packet's playout deadline)  >  (~one RTT + margin) ?

      yes → worth buying: NACK it        (V3's recover loop)
      no  → sunk cost: skip/conceal it   (V2's give-up path)
```

TCP answers "yes" to every packet, always, and makes everything behind the
gap wait for the answer. This project answers per packet — which is why the
boss fight can demand **≥ 90% of lost packets recovered before their
deadline** *and* **no stall longer than 300 ms** at the same time. Those two
goals are only compatible when recovery is selective.

## 5. Where this leaves you

Everything you build in this project is one of TCP's jobs, re-implemented
with a deadline in the loop:

| TCP's version | This project's version | Vertical |
| --- | --- | --- |
| Byte-stream sequencing | RTP sequence number + media timestamp + marker | V1 |
| Kernel reorder buffer (unbounded wait) | Jitter buffer (bounded wait, then give up) | V2 |
| Retransmit everything, always | NACK only what's still useful, from a bounded cache | V3 |
| Congestion window (built-in) | Bandwidth estimator + token pacer (yours) | V4 |

One caution to carry forward: **do not rebuild TCP by accident.** Every time a
design choice tempts you to wait longer, retry harder, or buffer deeper "to be
safe", you are trading away the latency that is this domain's entire currency.
The SPEC's criteria are written to catch exactly that — bounded added latency,
bounded memory, deadline-bounded retransmission.

## 6. Mental-model summary

| Question | Answer to hold onto |
| --- | --- |
| Why not TCP? | Its in-order, total-reliability contract turns one lost packet into a stall for everything behind it — and delivers data past its deadline, which is worthless. |
| Why UDP? | Not because "fast" — because it *delegates the policy to you*, and only you know the deadlines. |
| What do you owe in exchange? | Loss, reordering, duplication, jitter, and congestion are now application problems. |
| What is the unit of decision? | One packet vs. one deadline. Recover it if it can still arrive in time; abandon it if not. |
| What's the failure to avoid? | Rebuilding TCP: unbounded waits, unconditional retransmits, unbounded buffers. |

**Where you'll see this next:** [SPEC.md](../SPEC.md)'s intro paragraph is
this doc in miniature. Doc [01](01-rtp-making-a-stream-out-of-datagrams.md)
starts the build: the 12 bytes that make a stream out of datagrams.
