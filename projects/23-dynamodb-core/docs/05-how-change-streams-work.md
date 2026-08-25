# How Change Streams Work — From First Principles

> Why a store nobody can subscribe to is a dead end, why "ordered per key,
> unordered across keys" is the strongest cheap guarantee a partitioned store can
> make — and exactly enough for a consumer — and what a shard iterator really is.
> No prior knowledge of change data capture, log-based integration, or Kafka-style
> systems assumed.
>
> Prepares you for **V5** in [SPEC.md](../SPEC.md). Anchored to
> [streams.py](../src/dynamodb_core/streams.py),
> [routes.py](../src/dynamodb_core/routes.py) and
> [config.py](../src/dynamodb_core/config.py).

---

## 0. The one sentence to hold onto

**A stream turns "the current state" into "every change that produced it" — and
that turns a database into something other systems can build on without polling
it.**

The ordering guarantee follows the storage: strictly ordered within a partition
key, unordered across them. That is not a weakened promise. It's the strongest one
available without a global sequencer, and it happens to be precisely what a
consumer needs.

---

## 1. The problem: how does anything else find out?

Your table works. Now the rest of the company wants in:

- Search needs orders indexed in Elasticsearch.
- Analytics needs them in the warehouse.
- Email needs to fire when an order ships.
- The cache needs invalidating when an item changes.

Without a stream, every one of those is a polling loop:

```
   every 60s:  Scan the whole table, diff against what I saw last time
```

Which is broken in more ways than it looks:

| Polling problem | Why it's fatal |
| --- | --- |
| Cost | A full `Scan` per consumer per interval. Four consumers = four table scans a minute, billed on `scanned_count` (see [doc 04](04-how-provisioned-capacity-and-hot-partitions-work.md#3-metering-the-cost-is-not-the-same-as-limiting-it)). |
| Latency | Change visible after up to one polling interval. Shorten it and the cost goes up linearly. |
| **Missed changes** | If an item changes twice between polls, the consumer sees one change. If it's created and deleted between polls, it sees **nothing at all**. |
| Deletes | A `Scan` shows what *is*. A row that vanished leaves no trace — you can only infer it by diffing full snapshots. |
| Coupling | Every consumer needs table credentials, knowledge of the schema, and the ability to hammer your production table. |

The missed-change row is the one you can't engineer around. The state doesn't
contain its own history, so any state-polling design is lossy by construction.

The fix is to have the database emit the changes as they happen — **change data
capture**. Every mutation appends a record. Consumers read the record log.

```
   PutItem(order#7)  ──> item stored ──> record appended: INSERT order#7
   UpdateItem(#7)    ──> item updated ──> record appended: MODIFY order#7 (old, new)
   DeleteItem(#7)    ──> item removed ──> record appended: REMOVE order#7 (old)
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        ▼                     ▼                     ▼
                   search indexer        warehouse ETL        email sender
                   (own position)        (own position)       (own position)
```

Each consumer keeps its own position. They don't know about each other, they can't
slow each other down, and a new one can be added without touching the producer.
That's the "integration seam" the SPEC keeps calling it.

---

## 2. What a record has to carry

[`StreamRecord`](../src/dynamodb_core/streams.py):

```python
@dataclass(slots=True)
class StreamRecord:
    sequence_number: str
    event_name: EventName        # INSERT | MODIFY | REMOVE
    key: ItemKey
    created_at: float
    old_image: Item | None = None
    new_image: Item | None = None
```

Two images, because different consumers need different things:

| Consumer | Needs |
| --- | --- |
| Search indexer | `new_image` — the document to index |
| Cache invalidator | `key` alone |
| Audit log | **both** — "who changed what from what to what" |
| Analytics | both, to compute a delta without reading the table back |

[`StreamViewType`](../src/dynamodb_core/streams.py) makes it a dial:

```python
    KEYS_ONLY = "KEYS_ONLY"
    NEW_IMAGE = "NEW_IMAGE"
    OLD_IMAGE = "OLD_IMAGE"
    NEW_AND_OLD_IMAGES = "NEW_AND_OLD_IMAGES"
```

> The cost dial again: `NEW_AND_OLD_IMAGES` lets a consumer compute a diff without
> reading the table back, at the price of every record carrying two copies of the
> item.

Same shape as `ProjectionType` in [doc 03](03-how-secondary-indexes-work.md#5-projections-how-much-data-to-duplicate)
— pay storage now to avoid a read later, or don't. A `KEYS_ONLY` stream forces
every consumer to read the table back, which reintroduces load on the thing you
were trying to decouple from.

Your criterion: *"a `MODIFY` carries both images."* Which requires `put_item` to
return the old item — the same requirement V2 had for orphan removal
([doc 03 §6](03-how-secondary-indexes-work.md#6-the-four-cases-maintain-has-to-get-right)).
Three verticals now depend on that one return value.

One criterion that seems small and isn't:

> a delete of a non-existent item appending **nothing**

`DeleteItem` on a key that isn't there is a successful no-op — but *nothing
changed*, so there is no change to report. Emitting an empty `REMOVE` would tell a
downstream consumer that an item it may never have seen was deleted. The scaffold:
*"A delete of a non-existent item is not a change — append nothing and say so by
returning early rather than writing an empty REMOVE."*

---

## 3. Ordering: why per-key, and why that's enough

This is the conceptual centre of V5.

### Why not order everything globally?

A single global counter across all writes would mean every write, on every
partition, coordinating on one number. That's a single point of contention — the
exact thing partitioning exists to avoid — and it would be the hot partition from
[doc 04](04-how-provisioned-capacity-and-hot-partitions-work.md), except now it's
unavoidable and applies to every write in the system.

The scaffold states the consequence on `sequence_number`:

> `sequence_number` is monotonic **within a partition key**. Do not promise more
> than that: a global counter would be a single point of contention and would imply
> an ordering across keys that the storage layer never provided.

Note the second clause. A global sequence number wouldn't just be expensive — it
would be **dishonest**. The items were written to independent partitions with no
coordination; there is no fact of the matter about which happened "first." Stamping
a total order onto them invents information.

### Why per-key ordering is sufficient

Because everything that cares about order cares about it *per entity*:

```
   order#7:  INSERT(pending) ──> MODIFY(paid) ──> MODIFY(shipped) ──> REMOVE
             └─────────── these MUST arrive in this order ───────────┘

   order#7 vs order#99:  unrelated. Any interleaving is fine.
```

If a consumer processed `order#7`'s events out of order it would conclude the order
was shipped before it was paid. If it processes `order#7` and `order#99` in either
order, nothing is wrong — they're different orders.

That's the whole argument, and it generalises: **ordering matters within an entity,
not across entities.** Which is exactly the guarantee partitioning already gives
you for free.

### Shards

One shard per partition key, ordered within it:

```
   shard for hash("cust#1")     ──> [seq 1] [seq 2] [seq 3] ──>  strictly ordered
   shard for hash("cust#2")     ──> [seq 1] [seq 2]         ──>  strictly ordered
   shard for hash("cust#3")     ──> [seq 1]                 ──>  strictly ordered
        no ordering relationship between shards
```

The scaffold's suggested shape says it directly:

```python
        #   self._shards: dict[bytes, deque[StreamRecord]]
```

Shards are addressed by the **encoded partition key** — the same encoding from
[doc 01](01-how-order-preserving-key-encoding-works.md). Non-canonical encoding
here means two shards for one logical key, which means the per-key ordering
guarantee quietly evaporates. Fourth vertical to depend on that encoding.

Your criterion asks for honesty as much as correctness: *"the ordering guarantee
across different keys is documented (and **no stronger than what you actually
provide**)."* Over-promising in a doc is a bug that surfaces in someone else's
system months later.

---

## 4. Shard iterators: whose job is it to remember?

A consumer reads with an iterator, not an offset it invents:

```python
    def read(self, iterator: str, *, limit: int = 100) -> tuple[list[StreamRecord], str | None]:
```

You pass an iterator, you get records **and the next iterator**. Two design points:

**The consumer holds the position, not the server.** The server doesn't track who
has read what. That's what lets N consumers read at N different positions with no
server-side state per consumer, and it's why a consumer can rewind. (The cost:
the consumer must persist its position somewhere, or it restarts from wherever its
iterator type says.)

**`None` next-iterator means the shard is closed** — it will never yield more. Not
"nothing right now"; that's an empty batch with a valid next iterator. A consumer
must distinguish "caught up, poll again" from "this shard is finished."

[`ShardIteratorType`](../src/dynamodb_core/streams.py) is where a consumer starts:

| Type | Starts at | Used for |
| --- | --- | --- |
| `TRIM_HORIZON` | The oldest retained record | Bootstrapping a new consumer, or replaying after a bug |
| `LATEST` | Only changes from now on | A consumer that only cares about live traffic |
| `AT_SEQUENCE_NUMBER` | Exactly that record | Resuming from a persisted position |
| `AFTER_SEQUENCE_NUMBER` | Just past it | Resuming *after* the last record you successfully processed |

The last two are the difference between at-least-once and at-most-once redelivery
on restart, and it's worth knowing which one your consumer wants.

`TRIM_HORIZON` is the one that makes streams powerful: **replay**. A new search
index can be built from the stream alone, without touching the table. A consumer
that had a bug can be pointed back and re-run. That's why your criterion requires
replaying *the whole retained window* — replay-from-the-beginning is a feature, not
an error path.

The endpoint is already wired in [routes.py](../src/dynamodb_core/routes.py):

```python
@public_router.get("/streams/{table_name}")
async def read_stream(table_name: str, state: StateDep, iterator: str, limit: int = 100) -> Response:
    """Read a batch of change records — the seam a consumer polls."""
```

---

## 5. Two different limits, and a record can be lost to either

This is the subtlest part of V5 and the scaffold flags it twice.
[config.py](../src/dynamodb_core/config.py):

```python
    stream_retention_hours: float = Field(default=24.0, gt=0)
    stream_buffer_size: int = Field(default=10_000, gt=0)
```

Two independent bounds:

| Bound | Type | Enforced by |
| --- | --- | --- |
| `retention_seconds` = 86,400 s (24 h) | **Time** | `trim()`, called periodically |
| `buffer_size` = 10,000 records | **Count** | The deque's `maxlen`, immediately on append |

> note that time-based trimming and the deque's size bound are two **DIFFERENT**
> limits, and a record can be lost to either.

The count bound is the one that will surprise you, because it's per shard, and a
shard is a partition key. Do the arithmetic: at the boss fight's sustained
20,000 writes/sec concentrated on **one** key, a 10,000-record buffer holds
**0.5 seconds** of history *(verified)* — not 24 hours. A consumer that pauses for
a second has already lost data.

That interacts directly with the boss criterion *"a stream consumer keeps up at the
sustained write rate with lag ≤ 500ms."* Under those numbers, the 500 ms lag target
and the buffer's 0.5 s of headroom are the same constraint. The cross-cutting
requirement — *"the stream buffer and WAL are bounded, and a slow consumer cannot
grow memory without limit"* — is what forces the bound to exist; sizing it against
your write rate is what stops it from being a data-loss bug. **Tune them together**,
as the Python checklist item says.

### Losing data is allowed. Losing it *silently* is not.

```python
        # A `collections.deque(maxlen=buffer_size)` gives you the bound for free —
        # and note what the bound MEANS: the oldest record is dropped, so a
        # consumer that falls too far behind loses data and must be told (that is
        # the trimmed-iterator error below), not silently served a gap.
```

An iterator pointing at trimmed data must raise a **distinct** error. The
alternative — quietly returning the oldest surviving record — hands the consumer a
gap it has no way to detect. It would build an index missing rows it doesn't know
are missing, and every downstream system would inherit the corruption.

Fail loudly. A consumer that knows it fell behind can re-bootstrap from
`TRIM_HORIZON`. One that wasn't told cannot do anything.

---

## 6. The atomicity requirement

The hardest V5 criterion:

> A write is not acknowledged until its stream record is durable — a crash mid-write
> never leaves a committed item with no record (or vice versa).

Two failure modes, both bad:

```
   item committed, record lost         record written, item lost
   ────────────────────────────        ──────────────────────────
   The change happened but no          Consumers act on a change
   consumer will ever hear.            that never happened. The
   The search index is missing         search index has a document
   a document, permanently, and        for an order that does not
   nothing will reconcile it.          exist.
```

The scaffold puts the ordering constraint on the caller:

> The caller must invoke this atomically with the item write — a committed item
> with no record (or a record with no item) is the bug the V5 crash test hunts for.

This is step 4 of the write-path sequence from
[doc 02 §6](02-how-conditional-writes-work.md#6-the-write-path-ordering-is-the-design):
condition → write → indexes → stream record, all atomic. And it's the same
requirement the storage horizontal states: *"Item + index entries + stream record
for one mutation apply atomically — recovery never surfaces a half-applied write."*

The Proof asks for a **crash-injection test** — actually killing the process
mid-write and checking recovery, not reasoning about it. That's the only way to
find the window, because the window is small and only real crashes land in it.

---

## 7. Lag: the metric that tells you the seam is healthy

```python
    def lag(self, iterator: str, now: float) -> float:
        """Seconds between the newest record and where this iterator sits."""
```

Lag is *the* health metric for a stream, because a stream fails gradually. Nothing
errors while a consumer falls behind — it just gets further behind, until it hits
the retention or buffer bound and starts losing records. By then it's too late.

Watching lag gives you the whole story in one number, and it's the leading
indicator: rising lag is the alert, lost records are the incident.

The boss requires **≤ 500 ms** at the sustained write rate, plus replay from
`TRIM_HORIZON` losing no record. Note that those two test opposite things: lag tests
the consumer keeping up *live*; replay tests the history being *complete*.

---

## 8. The design space (your decisions)

`docs/23-design.md` must record **"the retention window and shard model."**

| Decision | The tension |
| --- | --- |
| Shard = partition key? | Simple and gives per-key ordering directly. But millions of keys means millions of shards, each with a deque — the same unbounded-growth problem as V4's bucket-per-key, with the same answer needed. |
| Sequence-number format | Must be monotonic per shard and encode a position an iterator can resume from. Opaque string, or structured? Opaque is safer (you can change it); structured is debuggable. |
| Iterator encoding | It's handed to clients and comes back. Can a client forge one? Should it be signed? (The security horizontal is watching.) |
| When to `trim` | Periodically, on a timer, or lazily on read? A timer is a background task to shut down gracefully; lazy trimming means a quiet stream never trims. |
| Buffer sizing | §5's arithmetic. Sized against the *expected write rate per key*, not a round number that felt safe. |

**Where I stop:** sequence-number assignment, iterator encoding and the atomic
append protocol are the V5 challenge. `/hint` and `/quest` as usual.

---

## 9. Mental-model summary

| The instinct | The correction |
| --- | --- |
| "Consumers can just poll the table." | Polling misses changes entirely — two updates between polls look like one; a create-then-delete looks like nothing. State has no history. |
| "The stream should be globally ordered." | That needs a global sequencer: a single point of contention, and it would **invent** an ordering the storage never provided. |
| "Per-key ordering is a weak guarantee." | It's exactly what consumers need — order matters *within* an entity, not across entities. |
| "A delete always emits a REMOVE." | Deleting a non-existent item changed nothing, so it emits nothing. |
| "Retention is the only limit." | Two limits: 24 h by time *and* 10,000 records per shard by count. At 20k writes/s on one key that's **0.5 s** of history, not 24 h. |
| "A consumer that falls behind gets the oldest available record." | It gets a **distinct error**. Silently skipping hands it an undetectable gap. |
| "Write the item, then append the record." | Two steps means a crash between them. The criterion is atomicity — proven by crash injection, not by reasoning. |
| "The server tracks each consumer's position." | The *consumer* holds its iterator. That's what makes N independent consumers and replay possible. |
| "Empty batch means the shard is done." | Empty batch + valid iterator = caught up, poll again. `None` iterator = closed forever. |

---

## 10. Where you'll build this

**Module:** [streams.py](../src/dynamodb_core/streams.py):

| `todo` | What it owes you |
| --- | --- |
| `Stream.__init__` | Per-shard storage, one shard per partition key, bounded. |
| `append` | Next sequence number **for this key**, images cut to `view_type`, nothing appended for a no-op delete. |
| `read` | Decode iterator → records in order → encode next position; distinct error on trimmed data. |
| `trim` | Time-based retention, returning how many went. |
| `lag` | Seconds between newest record and this iterator — backs the metric and the boss target. |

The `GET /streams/{table_name}` endpoint in
[routes.py](../src/dynamodb_core/routes.py) is already wired to `stream.read`, and
`TableContext.stream` in [state.py](../src/dynamodb_core/state.py) is already
threaded through. What's missing is the *append* call in the write path — step 4 of
the sequence in the `PutItem` TODO.

**Done-when criteria this doc unlocks** (from [SPEC.md](../SPEC.md) V5): exactly one
record per mutation with the right event type and nothing for a no-op delete;
strict per-key ordering with the cross-key guarantee documented honestly;
`TRIM_HORIZON` replay and `LATEST` tailing; view types carrying exactly what they
promise with `MODIFY` carrying both images; trimmed data failing distinctly rather
than silently skipping; and item+record atomicity under crash.

**Next:** [06-backend-fundamentals-in-this-project.md](06-backend-fundamentals-in-this-project.md)
— the horizontal checklist: the wire protocol, durability, pagination, security,
observability, and what CPython does to all of it.
