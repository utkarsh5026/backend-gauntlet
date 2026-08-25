# Ordering Keys — Why FIFO Costs You Parallelism, and How to Buy Some Back

> Teaches why strict global ordering is a trap, how per-key ordering resolves it, and
> why head-of-line blocking is the price rather than the bug. No prior knowledge assumed
> — not of Kafka partitions, not of SQS FIFO.
>
> Prepares you for **V4** in [`src/sqs_queue/fifo.py`](../src/sqs_queue/fifo.py)
> (`GroupIndex.next_sequence/selectable/block/unblock/on_send`). Depends on the delivery
> lifecycle from [`00-receipt-handles-and-leases.md`](./00-receipt-handles-and-leases.md).

---

## The one sentence to hold onto

**Ordering and parallelism are the same resource — every guarantee you make about order
is a consumer you can no longer use — so make the guarantee about a *key*, not the queue.**

---

## 1. Why you'd want ordering at all

Most queue work doesn't care. "Send this email", "resize this image", "recompute this
cache entry" — run them in any order, run them concurrently, nobody notices.

Then you hit the work that does:

```
   order 4417:  CREATED  ->  PAID  ->  SHIPPED  ->  DELIVERED
```

Process `SHIPPED` before `PAID` and your order state machine rejects it, or worse,
accepts it. Process `CANCELLED` before `CREATED` and you've cancelled an order that does
not exist yet. The events are only meaningful in sequence.

So the queue offers to preserve it: **FIFO** — first in, first out, guaranteed.

---

## 2. The trap: what strict global FIFO actually costs

Take the guarantee literally. Message *n+1* may not be *processed* until *n* is done. Not
"delivered after" — **processed after**, because delivering both concurrently is exactly
how they get processed out of order.

That means: at any instant, one message from the queue is being worked on. One.

```
   16 consumers connected to a strictly-ordered queue
   ────────────────────────────────────────────────────
   consumer 1:   [ working on message 1 ]
   consumer 2:   idle
   consumer 3:   idle
   ...
   consumer 16:  idle

   throughput = 1 / (time to process one message)
```

Sixteen consumers, fifteen idle. Your throughput is one worker's throughput and adding
workers changes nothing at all — the guarantee has eaten every one of them.

**You bought ordering by giving up the entire reason you put a queue there.** And no
amount of tuning gets it back, because ordering and parallelism are not two knobs; they
are the same knob with labels on opposite ends.

---

## 3. The resolution: make ordering a property of a key

The insight is that you almost never need *global* order. You need order 4417's events in
sequence. You do not care whether they interleave with order 9312's — those two orders
have nothing to do with each other.

So: attach a **group id** to each message. Messages sharing a group are strictly ordered.
Messages in different groups are entirely independent.

```
   group "order-4417":   CREATED -> PAID -> SHIPPED        strictly ordered
   group "order-9312":   CREATED -> PAID                   strictly ordered
   group "order-2280":   CREATED                           strictly ordered
                              ▲
        ...but these three groups run fully in parallel, on three consumers.
```

Now your parallelism ceiling is **the number of distinct groups**, and you got there
without weakening the guarantee that anybody actually needed.

Every system that offers ordering at scale converged on this:

| System | The key | Parallelism ceiling |
|---|---|---|
| SQS FIFO | `MessageGroupId` | number of groups |
| Kafka | partition key → partition | number of partitions |
| Pulsar | `Key_Shared` subscription key | number of keys |
| Google Pub/Sub | ordering key | number of keys |

---

## 4. The decision you're handing your users

Here is the part worth sitting with, because it is not a coding decision — it is an API
design decision whose consequences land on somebody else.

**Your users choose the group id, and that choice sets their throughput ceiling.**

| Group id | Distinct groups | Effective parallelism | Verdict |
|---|---|---|---|
| `order_id` | millions | millions | ✅ good — natural independence |
| `customer_id` | ~100,000 | ~100,000 | ✅ good, unless one customer is huge |
| `region` | 4 | **4** | ❌ four consumers, forever |
| `"default"` | 1 | **1** | ❌ strict global FIFO, accidentally |

That last row is the one to design against. A consumer that sends without a group id —
because the field seemed optional — must not be quietly assigned a shared default, or you
have serialized their entire workload behind an ordering constraint nobody asked for and
nobody can see. The SPEC's criterion is explicit: a FIFO queue **refuses** a message with
no `MessageGroupId`, and `GroupIndex.on_send` in [`fifo.py`](../src/sqs_queue/fifo.py)
carries that note.

And the hot-key problem is the same table read from the other side. If one group's
*production* rate exceeds one consumer's *processing* rate, that group backs up
regardless of how much total capacity you have — because only one consumer may work it.
No autoscaler can fix a hot key. `GroupState.depth` exists so you can at least *see* it:
one group's depth climbing while others stay flat is the signature.

---

## 5. The mechanism: what must be true while a group is in flight

The rule you're implementing sounds small and changes the shape of receive:

> While a group has a message in flight, **no later message from that group may be
> delivered to anyone.**

Notice where the constraint sits. It is on the **group**, not the message. So a receive
is no longer *"give me the oldest available message"* — it is *"give me the oldest
available message **from a group that isn't blocked**"*.

```
   receive() on a standard queue:      receive() on a FIFO queue:
   ─────────────────────────────       ──────────────────────────
   take the oldest available            find groups with no in-flight message
                                        take the head of one of those
```

That is why `GroupIndex.selectable(limit)` exists as its own method. Getting the answer is
easy — walk every group, check each one. Getting it *without* walking every group is what
the bench measures, because the boss fight runs 1,000 groups and the real world runs more.

`block` and `unblock` are the pair that maintain it, and `unblock` takes a **message id**
as well as a group for a reason that should feel familiar by now: a stale unblock — from
a superseded delivery, exactly the case
[doc 00](./00-receipt-handles-and-leases.md) is about — must not release a group that a
*different*, live delivery is legitimately holding. The fencing idea shows up again.

---

## 6. Head-of-line blocking is the guarantee, seen from behind

A message at the front of a group fails. And fails. And fails.

```
   group "order-4417":   [ POISON ] -> [ msg 2 ] -> [ msg 3 ] -> [ msg 4 ]
                             ▲
                    received, fails, lease expires, received again, fails...
                    and messages 2-4 cannot be delivered to anyone, ever, meanwhile.
```

The instinct is to call this a bug and route around it. It is not a bug. **It is the
guarantee.** You promised message 2 would not be processed before message 1. Message 1 is
not done. Delivering message 2 would break exactly the promise the user chose this queue
for.

So the SPEC asks you to **demonstrate** it, not avoid it — and to prove the blast radius:

- one stuck group stalls **that group**
- every other group keeps draining at full speed

If a poison message in `order-4417` slows down `order-9312`, you have a bug. If it stalls
`order-4417` until it is deleted or dead-lettered, you have a correct FIFO queue.

Which is also the connection to V6: the dead-letter queue is what *ends* head-of-line
blocking. After `maxReceiveCount` deliveries the poison message leaves the queue, the
group unblocks, and messages 2–4 flow. Without redrive, a FIFO group with a poison head
is stalled permanently. See
[`05-control-plane-and-redrive.md`](./05-control-plane-and-redrive.md) §5.

---

## 7. Two smaller rules that complete the contract

**Sequence numbers.** Each message gets one that strictly increases within its group. On
the wire it is a *decimal string*, not an integer, because it outgrew 64 bits — a detail
worth noticing, since it tells you the real service has been running long enough to
overflow one.

The subtle requirement is "never reused". A group that empties completely and then
receives a new message must not restart at zero, or a consumer deduplicating on sequence
number silently drops it. `GroupState.next_sequence` in
[`fifo.py`](../src/sqs_queue/fifo.py) is where that lives.

**The `.fifo` suffix.** FIFO-ness is in the queue *name*, not in an attribute. That looks
like a quirk and it is a deliberate design property: the contract is visible at every call
site. A developer reading `orders.fifo` in a config file knows what they're getting
without a `GetQueueAttributes` round trip — and cannot accidentally flip it later, since
`QueueKind` in [`models.py`](../src/sqs_queue/models.py) is fixed at creation.

---

## 8. Mental model summary

| Question | Answer |
|---|---|
| What does strict global FIFO cost? | Every consumer but one |
| What replaces it? | Per-key ordering: strict within a group, parallel across groups |
| What sets the parallelism ceiling? | The number of distinct groups — chosen by your *user* |
| What's the worst group id? | A constant. It's strict global FIFO wearing a disguise |
| What changes about receive? | Select from unblocked *groups*, not from messages |
| Is head-of-line blocking a bug? | No — it's the guarantee. The blast radius is the criterion |
| What ends head-of-line blocking? | The DLQ (V6) |
| Can an autoscaler fix a hot key? | No. One group, one consumer |

---

## Where you'll build this

[`src/sqs_queue/fifo.py`](../src/sqs_queue/fifo.py) — five
`raise NotImplementedError`s: `next_sequence`, `selectable`, `block`, `unblock`,
`on_send`. `is_fifo_name` is already done. Call sites are marked in
[`routes.py`](../src/sqs_queue/routes.py) under `_send_message`, `_receive_message` and
`_delete_message`.

Done-when criteria this unlocks (V4 in [`SPEC.md`](../SPEC.md)): in-order delivery within
a group under concurrency, parallel delivery across groups, the in-flight blocking rule,
bounded head-of-line blast radius, per-group sequence numbers, the required
`MessageGroupId`, and the `.fifo` naming rule.

**The test that proves it:** 1,000 groups × 100 messages, ≥8 concurrent receivers, zero
out-of-order. Then a second test that jams one group and watches the other 999 drain.

`/hint 29 V4` · `/quest 29 V4`
