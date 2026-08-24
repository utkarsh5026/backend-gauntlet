# How Secondary Indexes Work — From First Principles

> Why an index in a partitioned store is not a query hint but a **second copy of
> your data**, why one kind of index can be strongly consistent and the other can
> never be, and what "write amplification" costs you in numbers.
> No prior knowledge of database indexing assumed.
>
> Prepares you for **V2** in [SPEC.md](../SPEC.md). Anchored to
> [indexes.py](../src/dynamodb_core/indexes.py), [table.py](../src/dynamodb_core/table.py)
> and [state.py](../src/dynamodb_core/state.py).
>
> Read [00-how-partition-keys-decide-placement.md](00-how-partition-keys-decide-placement.md)
> first — this doc is the answer to the problem that one ends on.

---

## 0. The one sentence to hold onto

**An index is a second table, written by your writes, paid for by your bill, and —
for one of the two kinds — always slightly behind.**

In a relational database "add an index" feels like tuning. Here it is a *storage
decision*: you are duplicating your data under a different key, and something has
to keep the duplicate honest on every single write.

---

## 1. The problem this solves

[Doc 00](00-how-partition-keys-decide-placement.md) ended on a wall. You have an
orders table keyed `pk=customer_id, sk=order_id`, which serves "all of customer 1's
orders" perfectly. Then someone asks for **"all SHIPPED orders, across all
customers."**

```
   partition A (cust#1)      partition B (cust#2)      partition C (cust#3)
   ┌────────────────────┐    ┌────────────────────┐    ┌────────────────────┐
   │ order#1  SHIPPED   │    │ order#4  PENDING   │    │ order#7  SHIPPED   │
   │ order#2  PENDING   │    │ order#5  SHIPPED   │    │ order#8  SHIPPED   │
   │ order#3  SHIPPED   │    │ order#6  PENDING   │    │ order#9  PENDING   │
   └────────────────────┘    └────────────────────┘    └────────────────────┘
          ▲                          ▲                          ▲
          └──── the SHIPPED orders are scattered across every partition ────┘
```

There is no clever read that fixes this, because `status` isn't the address.
`Scan` is the only correct answer, and it's O(table).

So: if the data were *also* stored keyed by `status`, the query would be a normal
`Query` against a partition. That's the entire idea.

```
   the index — same items, filed under a different address
   ┌──────────────────────────────┐   ┌──────────────────────────────┐
   │ partition "SHIPPED"          │   │ partition "PENDING"          │
   │   order#1 (cust#1)           │   │   order#2 (cust#1)           │
   │   order#3 (cust#1)           │   │   order#4 (cust#2)           │
   │   order#5 (cust#2)           │   │   order#6 (cust#2)           │
   │   order#7 (cust#3)           │   │   order#9 (cust#3)           │
   └──────────────────────────────┘   └──────────────────────────────┘
```

One `Query` on partition `"SHIPPED"`. O(matched). Solved — and now you pay for it.

---

## 2. What it costs: write amplification

The index doesn't maintain itself. Every write to the base table must also write
the index, or the index lies.

```
   PutItem(order#10)
        │
        ├──> base table       1 write
        ├──> GSI by status    1 write
        ├──> GSI by region    1 write
        └──> GSI by date      1 write
                              ─────────
                              4 writes for 1 logical operation
```

That ratio is **write amplification**, and it is exactly linear in the index count:

| GSIs | Writes per `PutItem` | Amplification | Boss-fight ±20% band |
| --- | --- | --- | --- |
| 0 | 1 | 1× | 0.8 – 1.2 |
| 1 | 2 | 2× | 1.6 – 2.4 |
| 2 | 3 | 3× | 2.4 – 3.6 |
| 3 | 4 | 4× | 3.2 – 4.8 |

*(Computed against the boss criterion "matches the index count within ±20% of your
cost model's prediction.")*

This is why [`ConsumedCapacity`](../src/dynamodb_core/throughput.py) is split by
target rather than reported as one number:

```python
@dataclass(slots=True)
class ConsumedCapacity:
    table: float = 0.0
    indexes: dict[str, float] | None = None
```

> Split by target because a write to a table with three GSIs costs the base table
> one write and each index one more — surfacing that is how write amplification
> stops being invisible.

The reason it's usually invisible is that adding an index is a one-line schema
change with no code change at the call site. Your write path silently gets 4× more
expensive and nothing in your application looks different. Making that number
appear in every response is the fix.

---

## 3. GSI vs LSI: the difference is which partition the entry lands in

This is the conceptual heart of V2, and the scaffold's module docstring lays it
out. Read it slowly — the consistency behaviour is a *consequence*, not a policy:

> * **LSI** keeps the partition key and changes only the sort key. The entry lands
>   in the *same* partition as the base item, so it can be written in the same
>   atomic step and read back **strongly consistent**.
> * **GSI** re-partitions under a different key entirely. Its entry belongs to a
>   different partition, so it cannot join the base write's atomic step without
>   paying a cross-partition commit on every single write.

Draw it:

```
   LSI — same partition key, different sort key
   ┌─ partition hash("cust#1") ──────────────────────────────┐
   │  base entries:  sk=order#1, order#2, order#3            │
   │  LSI entries:   sk=2024-01-05, 2024-01-07, 2024-03-02   │  ← same box
   └──────────────────────────────────────────────────────────┘
        one partition, one atomic write, strongly consistent


   GSI — different partition key entirely
   ┌─ partition hash("cust#1") ─┐        ┌─ partition hash("SHIPPED") ─┐
   │  base item order#1         │  ────> │  GSI entry for order#1       │
   └────────────────────────────┘        └──────────────────────────────┘
        a DIFFERENT box — the write has to cross partitions
```

The whole argument in four steps:

1. To keep a GSI perfectly in sync, the base write and the index write must commit
   **together** — atomically, across two partitions.
2. Atomic commit across partitions means a **distributed commit protocol** (two-phase
   commit or similar): prepare both sides, then commit both sides.
3. That means every single write pays extra round trips, and — worse — if one
   partition is unavailable, the write to the *other* one can't complete either.
   Your base-table availability becomes the product of every index's availability.
4. DynamoDB refuses that trade. It maintains the GSI **after** the base write, and
   hands you eventual consistency plus a separate capacity budget instead.

So "a GSI cannot be strongly consistent" isn't a missing feature. It's the price of
the base table staying fast and available. The scaffold encodes the conclusion as a
one-line property:

```python
    @property
    def is_consistent(self) -> bool:
        """LSI reads can be strongly consistent; GSI reads cannot. Ever."""
        return self._definition.index_type is IndexType.LOCAL
```

Summarised:

| | **LSI** | **GSI** |
| --- | --- | --- |
| Partition key | Same as base | Its own, different |
| Sort key | Alternate | Its own |
| Entry lives | Same partition as the base item | A different partition |
| Maintained | In the base write's atomic step | After the fact |
| Consistency | Strong reads available | **Eventual, always** |
| Capacity | Draws on the base table's | Its own budget (see `IndexDefinition`) |
| Creatable later? | No — the partition layout is fixed at table creation | Yes |

That last row follows from the same logic: an LSI must live inside existing
partitions, so it has to exist when those partitions are laid out.

---

## 4. Eventual consistency, made observable

Your criterion doesn't ask you to *believe* in the lag. It asks you to catch it in
the act:

> The GSI is **eventually consistent** — a test observes a window where base and
> GSI disagree, and then observes convergence. The window is bounded and
> documented.

```
   t0   PutItem(order#10, status=SHIPPED)
   t0   base table:  order#10 present            ✅
   t0   GSI:         partition "SHIPPED" ...     ❌ not there yet
        │
        │   ← the lag window. Real, bounded, and your job to measure.
        ▼
   t1   GSI:         order#10 present            ✅  converged
```

A test that asserts the *disagreement* is unusual to write — you're asserting a
staleness you'd normally consider a bug. That's the point: it forces you to know
how long the window is, instead of hoping it's short. And it's why the API rejects
`ConsistentRead` on a GSI outright (a horizontal-checklist item) rather than
quietly serving stale data to a caller who asked for fresh.

---

## 5. Projections: how much data to duplicate

An index entry doesn't have to carry the whole item. [`ProjectionType`](../src/dynamodb_core/indexes.py):

```python
class ProjectionType(StrEnum):
    KEYS_ONLY = "KEYS_ONLY"
    INCLUDE = "INCLUDE"
    ALL = "ALL"
```

> This is a storage-cost dial. `ALL` makes every index read self-sufficient and
> doubles your storage; `KEYS_ONLY` is cheap but forces a second read against the
> base table for anything else — the "index fetch" that quietly doubles latency.

Say the item is `{pk, sk, status, region, total, notes}` and the GSI is keyed on
`status`:

| Projection | Entry holds | Storage | Query for `status=SHIPPED` needing `total` |
| --- | --- | --- | --- |
| `KEYS_ONLY` | `status`, `pk`, `sk` | Smallest | Index query **+ one base read per item** ← the index fetch |
| `INCLUDE ["total"]` | `status`, `pk`, `sk`, `total` | Middle | Served entirely from the index |
| `ALL` | Everything | ~2× the item | Served entirely from the index |

The "index fetch" is worth internalising because of how it *fails*: a query
returning 100 items does 1 index read plus **100 base reads**. Latency doesn't
degrade gently — it scales with the result size, and it looks fine in a test
returning three rows.

There's a hard floor on what an entry must contain regardless of projection, and
the scaffold flags it:

> Always keep the index's own key attributes AND the base table's key attributes,
> whatever the projection says — without them the entry is unusable.

Without the base key you have a `KEYS_ONLY` hit you can never resolve back to a
real item. The entry would be a pointer with no address.

---

## 6. The four cases `maintain` has to get right

[`SecondaryIndex.maintain`](../src/dynamodb_core/indexes.py) takes
`(old_item, new_item)` and the scaffold maps the encoding:

```python
        `(None, item)` is an insert, `(item, None)` a delete, `(old, new)` an update.
```

Three obvious cases and one that catches everybody:

| Case | Base mutation | Index must |
| --- | --- | --- |
| `(None, new)` | Insert | Add an entry — **unless** the item has no value for the index's key (§7) |
| `(old, None)` | Delete | Remove the entry |
| `(old, new)`, indexed attribute **unchanged** | Update | Update the projected attributes in place |
| `(old, new)`, indexed attribute **changed** | Update | **Delete the old entry, then insert a new one** — the entry moved partitions |

That fourth row is the orphan factory. Walk it concretely:

```
   before:   order#7  status=PENDING     GSI partition "PENDING"  has order#7
   update:   order#7  status=SHIPPED
   naive:    write an entry into partition "SHIPPED"           ✅ new entry
             ...and leave the "PENDING" entry alone            ❌ ORPHAN

   result:   Query(status=PENDING) still returns order#7,
             which is not pending. The index lies, silently, forever.
```

The scaffold is direct about it:

> the case that catches everyone — an update that CHANGES the indexed attribute is
> a delete of the old entry plus an insert of the new one. Handle it and you have
> no orphans; miss it and the index quietly returns items that no longer match.

This is also *why* [`Table.put_item`](../src/dynamodb_core/table.py) returns the
previous item:

> The old item is not a convenience — V2 needs it to remove stale index entries and
> V5 needs it for a `MODIFY` record's `OLD_IMAGE`.

You cannot delete the old entry if you don't know what the old value was. If V1's
`put_item` throws away the old item, V2 is unimplementable.

---

## 7. Sparse indexes: absence as a feature

> An item missing the GSI's key attribute is simply **absent** from that index
> rather than erroring — sparse indexes work.

This reads like error-handling trivia and is actually one of the most useful
patterns the data model offers.

Suppose 10 million orders, of which ~50 are `status=NEEDS_REVIEW` at any time. Make
a GSI keyed on an attribute you only *set* when review is needed:

```
   10,000,000 orders in the base table
            │
            │  only the ones carrying `review_flag` get an index entry
            ▼
   ┌──────────────────────────┐
   │  the review GSI: 50 items │   ← a Query here is 50 items, not 10 million
   └──────────────────────────┘
```

The index is a materialised view of "the rows in state X", maintained for free by
the write path, and its size is proportional to the *interesting* rows rather than
the table. Clearing the flag removes the entry — which is the §6 orphan case doing
useful work.

Erroring on a missing key attribute would destroy this entirely. Absence has to be
a normal outcome.

---

## 8. The design space (your decision)

`docs/23-design.md` must record **"index maintenance (sync vs async + projection)"**.
The axis:

| | **Synchronous** (in the write path) | **Asynchronous** (queued, applied after) |
| --- | --- | --- |
| Write latency | Base write waits for every index | Base write returns immediately |
| Consistency | Index is never behind | Index lags — the window you measure in §4 |
| Failure handling | An index failure fails the write | The queue must be durable, or you lose index updates and the index is *permanently* wrong |
| Backpressure | Natural — slow index slows writes | Needs a **bounded** queue (the cross-cutting requirement) and a policy for full |
| Matches | LSI semantics | GSI semantics |

Note the trap in the async column: if the queue is in-memory and the process dies,
those index updates are gone — not delayed, *gone*, and nothing will ever
reconcile them. Async maintenance implies either durability or a repair mechanism.
Whichever you choose, that's a design-doc paragraph.

A second decision, from `SecondaryIndex.__init__`:

> The entries are just items in a differently keyed table, so the same
> partition-plus-sorted-list shape from V1 works here. **Reusing `Table` itself is a
> legitimate design choice — say so in the design doc if you take it.**

That's explicit permission to reuse, with the condition that you justify it. The
tension is worth thinking through: an index entry has a lifecycle a base item
doesn't (it can be orphaned, it can be sparse, it's derived rather than
authoritative) — does `Table`'s interface model that, or fight it?

A third, from [state.py](../src/dynamodb_core/state.py): a GSI has its own
`read_capacity`/`write_capacity` in `IndexDefinition`, and your V4 criterion is
*"saturating the index throttles index writes without stopping unrelated
base-table reads."* Separate budgets are how one hot index doesn't take the table
down with it.

**Where I stop:** the maintenance algorithm, the storage layout, and the sync/async
choice are the V2 challenge. `/hint` for nudges, `/quest` for a guided build.

---

## 9. Mental-model summary

| The instinct | The correction |
| --- | --- |
| "An index makes queries faster." | An index is a **second copy of the data**, keyed differently. Reads get faster; writes get more expensive, linearly. |
| "Adding an index is a schema tweak." | It's a storage decision. Three GSIs mean 4× the writes — verified, and reported per-target in `ConsumedCapacity`. |
| "A GSI is stale because of an implementation shortcut." | Because its entry lives in a *different partition*, and cross-partition atomic commit on every write would cost latency and availability. It's a trade, taken deliberately. |
| "An LSI is just a GSI on the same table." | Its entry lands in the **same partition** as the base item, which is exactly what lets it be atomic and strongly consistent. |
| "`ConsistentRead` on a GSI should just do its best." | It's rejected. Serving stale data to someone who asked for fresh is worse than saying no. |
| "`KEYS_ONLY` is the efficient default." | Efficient in storage; a query needing other attributes does one base read **per item returned**. |
| "Updating an item updates its index entries." | Only if you handle the *changed key* case as delete-then-insert. Otherwise you leave orphans that quietly return wrong results. |
| "A missing indexed attribute is an error." | It's a **sparse index** — a materialised view of "rows in state X", sized by the interesting rows, not the table. |

---

## 10. Where you'll build this

**Module:** [indexes.py](../src/dynamodb_core/indexes.py):

| `todo` | What it owes you |
| --- | --- |
| `SecondaryIndex.__init__` | Index storage — reuse `Table` or roll your own, and justify it. Entries must carry the base table's key. |
| `project` | `KEYS_ONLY` / `INCLUDE` / `ALL`, always keeping both key sets. |
| `maintain` | The four cases in §6, especially changed-key = delete + insert. Sparse absence is normal, not an error. |
| `query` | Ordered read over the index's own key; reject `ConsistentRead` on a GSI. |

`TableContext` in [state.py](../src/dynamodb_core/state.py) already holds
`indexes: dict[str, SecondaryIndex]`, and the `PutItem` write path in
[routes.py](../src/dynamodb_core/routes.py) has the ordering TODO from
[doc 02 §6](02-how-conditional-writes-work.md) — index maintenance is step 3 of
four.

**Done-when criteria this doc unlocks** (from [SPEC.md](../SPEC.md) V2): base write
queryable by GSI key; projections returning exactly what they promise; an observed
and bounded lag window with convergence; strongly-consistent LSI read-after-write;
no orphaned entries including the changed-key case; sparse indexes working.

**Next:** [04-how-provisioned-capacity-and-hot-partitions-work.md](04-how-provisioned-capacity-and-hot-partitions-work.md)
— where the write amplification you just built becomes a number on a bill, and one
key takes the whole table down.
