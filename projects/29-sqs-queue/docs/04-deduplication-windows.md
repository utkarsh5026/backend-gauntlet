# Deduplication Windows — "Exactly-Once" With a Receipt and an Expiry Date

> Teaches which duplicate a broker can actually remove, why the answer is always a
> *window* rather than a guarantee, and what bounded memory costs. No prior knowledge
> assumed.
>
> Prepares you for **V5** in [`src/sqs_queue/dedup.py`](../src/sqs_queue/dedup.py)
> (`DedupWindow.derive_id/check/claim/expire/size`). Uses the deadline engine from
> [`01-deadline-engines.md`](./01-deadline-engines.md) for expiry, and applies to the
> FIFO queues of [`03-fifo-ordering-keys.md`](./03-fifo-ordering-keys.md).

---

## The one sentence to hold onto

**A broker can remove the duplicate the *producer* knows about, for as long as it can
afford to remember — and that is the entire product, no matter what the marketing page
says.**

---

## 1. The duplicate worth removing

A producer calls `SendMessage`. The response never arrives.

```
   producer                       broker
   ────────                       ──────
   SendMessage  ─────────────────>  received, enqueued ✅
                                    200 OK  ───┐
                <──────  ✗ ✗ ✗ ✗ ✗ ✗ ✗ ✗ ✗ ✗ ──┘  (connection dies here)
   ???
```

The producer is now in the one state distributed systems specialize in: it does not know.
The message may have landed. It may not. And the two responses are indistinguishable from
where it stands.

It has exactly two options, and both are wrong:

| Choice | Failure mode |
|---|---|
| Retry | The message might already be there → **duplicate** |
| Don't retry | The message might never have arrived → **lost** |

Between "the customer got charged twice" and "the customer's order vanished", most people
pick the duplicate. So producers retry, and duplicates are not an edge case — they are
what your queue gets, routinely, whenever the network is having a day.

**But notice something the producer knows and the broker doesn't:** those two sends are
the *same message*. The producer has that information. It just needs a way to say it.

That is a `MessageDeduplicationId`: the producer's way of saying "this is the same send I
tried before". A message carrying an id already seen inside the window is acknowledged
with the **original** message id and not enqueued. The producer sees success either way
and stops retrying. Nothing else in the system ever learns a duplicate happened.

---

## 2. Now the part everyone skips

Read that again with an adversarial eye, because the word "exactly-once" attaches itself
to this feature and drags along promises it cannot keep.

**It removes producer duplicates. It does nothing about consumer duplicates.**

The visibility timeout from [doc 00](./00-receipt-handles-and-leases.md) will still hand
the same message to a second consumer when the first is slow. No dedup id, anywhere,
changes that — it's a different duplicate, created after the send, on the other side of
the queue.

```
   producer duplicates          consumer duplicates
   ───────────────────          ───────────────────
   cause: retried send          cause: lease expiry / crash before delete
   fix:   dedup window (V5)     fix:   idempotent consumers. That's it.
                                       There is no broker feature for this.
```

**And it is five minutes, not forever.** The window is sized to cover a retrying client —
long enough for a producer's retry budget to run out, short enough that the memory stays
bounded (§4). A duplicate arriving at minute six is, as far as this service is concerned,
a new message.

Which means this sentence is true and worth writing down, in these words, in
`docs/29-design.md`:

> *This queue removes duplicate sends carrying the same deduplication id within a
> 5-minute window. It does not prevent a message being delivered more than once, and it
> does not prevent a duplicate send after the window closes. Consumers must be
> idempotent.*

Someone will read "exactly-once" and build a payment on a nightly reconciliation job. The
paragraph above is what stops that being your outage.

This scoping is universal, not a shortcoming of this design:

| System | Scope of its "exactly-once" |
|---|---|
| SQS FIFO | 5-minute dedup window, one queue |
| Kafka EOS | producer id + sequence, within a transaction, within the cluster |
| Pub/Sub | a bounded dedup window inside the subscription |
| Azure Service Bus | a configurable duplicate-detection window keyed on `MessageId` |

Every one of them is *bounded dedup inside one system's boundary*, sold with a phrase that
sounds unbounded. As the repo's own research notes put it: exactly-once always means
exactly-once **processing** within a boundary, never exactly-once delivery.

---

## 3. Where the id comes from

Two sources, and one overrides the other.

**Explicit.** The producer sends a `MessageDeduplicationId`. It knows best: it can use its
own idempotency key, a request id, the primary key of the row that triggered the send.

**Content-based.** The queue derives one from the message itself when the producer didn't
supply one. Deterministic by construction — same content, same id:

```
  body: "order-4417"
  sha256 -> a2d3a3d2774fb38e3e0bfaf9378457b1007dcdd8013a32022aee3e46609ae541
```

Two decisions live in that one line, and both are graded.

**What counts as "the same content"?** Body only, or body plus message attributes? SQS
hashes both, and the reasoning is that a consumer reads attributes: two messages
differing only in an attribute are two different messages to whoever processes them. Hash
only the body and you'll silently suppress one of them.

Whichever you choose, **the hash inputs are a contract**. Change what goes into it after
launch and every in-flight producer's retries stop deduplicating — silently, for one
window, exactly during the deploy when things are already interesting.

**And watch the event loop.** Hashing a 256 KB body is real work; doing it inline on every
send is one of the two places in this service most likely to trip the
`PYTHONASYNCIODEBUG=1` slow-callback criterion in [`SPEC.md`](../SPEC.md). Worth measuring
before you assume either way.

---

## 4. Bounded memory is the actual engineering problem

The lookup is a dict. That part is not hard. What's hard is that the dict is consulted by
every send and added to by most of them, and it must **not grow without bound**.

Do the arithmetic at the boss fight's target rate:

```
   10,000 sends/sec  ×  300-second window  =  3,000,000 live entries
```

Measured on this machine, a `dict` mapping a dedup id to a small tuple costs ~260 bytes
per entry:

| Entries | Resident memory |
|---|---|
| 100,000 | ~26 MB |
| 1,000,000 | ~244 MB |
| **3,000,000** | **~761 MB** |

The boss fight's whole-process target is 400 MB. **At the target send rate, the dedup
window alone is nearly twice the entire memory budget** — before a single message body is
stored.

That is a finding, not a failure, and it is the kind the SPEC wants recorded: name the
number, name the cause (per-entry object overhead in CPython — a `str` key plus a tuple
value plus hash-table slack), and say what a different representation or runtime would
change.

The criterion itself is a shape, not a number: memory tracks **window length × send
rate**, never total messages ever sent. Which means expiry has to actually happen — and
through V2's deadline engine, not a scan, or you have rebuilt the 74%-of-a-core problem
from [doc 01 §2](./01-deadline-engines.md) inside the dedup window.

`MAX_DEDUP_ENTRIES` in [`.env.example`](../.env.example) is the hard stop, and its
behaviour is a real decision:

| At the limit | Consequence |
|---|---|
| Refuse the send (`OverLimit`) | Correct, loud, and a caller-visible outage |
| Evict the oldest | Available, and silently stops deduplicating for whoever got evicted |

One fails loudly, one fails silently. There isn't a third option, and picking is part of
the vertical.

---

## 5. Check and claim: the gap you have to notice

`DedupWindow` splits the operation into `check` (has this id been seen?) and `claim`
(record it). That looks like a courtesy split and it hides the interesting race:

```
   send A: check("dedup-1") -> not seen
   send B: check("dedup-1") -> not seen      ← concurrent
   send A: claim("dedup-1")
   send B: claim("dedup-1")
   result: two messages enqueued for one dedup id ✗
```

The SPEC's criterion is **exactly one enqueue**, and the test asserts it under exactly
this concurrency. So one of the following has to be true, and deciding which is the work:
the two calls collapse into one atomic operation, or something else makes the interleaving
impossible, or the window is consulted somewhere that can't be interleaved.

(If you're thinking "the event loop is single-threaded, so there's no gap" — check
carefully where an `await` can appear between the two calls. That's the same reasoning
error as assuming a GIL makes check-then-act safe.)

---

## 6. Two security notes worth a sentence each

**Suppression.** If dedup ids are attacker-chosen or predictable, someone who can guess
yours can *send them first* — and your real message is silently suppressed as a
duplicate. The acknowledgement even looks successful.

**Window flooding.** Someone sending a million distinct dedup ids fills the window and
pushes out everyone else's entries (if you evict) or takes the queue down (if you refuse).

Neither needs an elaborate defence in this project. Both need a sentence in
`docs/29-design.md` saying you thought about them, which is the difference between a
design and a pile of code.

---

## 7. Mental model summary

| Question | Answer |
|---|---|
| Which duplicate does this remove? | The producer's retry |
| Which does it not remove? | The consumer's redelivery. Nothing does |
| Why a window and not forever? | Memory. It's sized to a retrying client's lifetime |
| Where does the id come from? | The producer, or a hash of the content it didn't send |
| What's in the hash? | Your decision — and it's a contract you can't change later |
| What bounds memory? | Window length × send rate. ~260 B/entry, ~761 MB at 3M |
| What happens at the cap? | Refuse loudly or evict silently. Pick |
| What's the hidden race? | check → claim, with two concurrent sends |
| What must the design doc say? | The exact scope, in a sentence a user could act on |

---

## Where you'll build this

[`src/sqs_queue/dedup.py`](../src/sqs_queue/dedup.py) — five
`raise NotImplementedError`s: `derive_id`, `check`, `claim`, `expire`, `size`. The
expiry callback is already wired to the deadline engine in
[`main.py`](../src/sqs_queue/main.py); the send-path call site is marked in
[`routes.py`](../src/sqs_queue/routes.py) under `_send_message`.

Done-when criteria this unlocks (V5 in [`SPEC.md`](../SPEC.md)): duplicate accepted but
not enqueued with the original id returned, expiry after the window, deterministic
content-based derivation, explicit id overriding it, bounded memory across a long run,
dedup applying across groups rather than within one, and the documented scope.

`/hint 29 V5` · `/quest 29 V5`
