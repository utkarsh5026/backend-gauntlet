# How Partition Keys Decide Placement — From First Principles

> A ground-up guide to what a "primary key" actually *is* in a partitioned store,
> and why DynamoDB's most-complained-about rule — **no partition key, no `Query`** —
> is not an API limitation but a consequence of where the bytes live.
> No prior knowledge of DynamoDB, hashing, or storage engines assumed.
>
> Prepares you for **V1** in [SPEC.md](../SPEC.md). Anchored to
> [table.py](../src/dynamodb_core/table.py), [item.py](../src/dynamodb_core/item.py)
> and [state.py](../src/dynamodb_core/state.py).

---

## 0. The one sentence to hold onto

**The partition key is an address, not a hint.**

In a relational database, an index is a performance decision — the query planner
*could* have found your row without it, just slowly. In a partitioned store, the
partition key decides *which physical box the item was filed in*. If you don't say
which box, there is no clever plan that finds the item; there is only "open every
box." That is the whole of `Query` vs `Scan`, and once you see the key as an
address, every other rule in this vertical stops being arbitrary.

---

## 1. The problem: why not just let people query anything?

Start with the naive design, because it's the one everybody reaches for first.

**"A table is a Python dict. Store items in it. Let callers filter on whatever
attribute they like."**

```python
items = {}                       # key -> item
items[("cust#1", "order#1")] = {...}

# and to query:
[i for i in items.values() if i["status"] == "SHIPPED"]
```

This works beautifully on your laptop with 500 items and fails for reasons that
have nothing to do with Python:

| The naive move | What breaks, concretely |
| --- | --- |
| Filter on any attribute | Every query reads **every item**. At 10 million items a "find one order" costs 10 million reads. |
| One dict on one machine | The dataset must fit in one process's memory. There is no next machine to add. |
| Query cost depends on the *result* | A query returning 3 items and a query returning 3 million items look identical to the caller until the latency graph explodes. |
| No notion of locality | "All orders for customer 1" touches items scattered across the whole structure — every read is a cache miss. |
| Adding a machine | Which items move? Nothing in the design says. You'd have to re-derive placement from scratch. |

The last row is the important one. **The moment you want more than one machine,
you need a rule that says where an item lives** — and that rule has to be
computable from the request alone, before you've read anything, because you have
to know which machine to *ask*.

That rule is the partition key.

> This project runs on exactly **one node** — distribution is explicitly out of
> scope, and lives in projects 07 and 09. But the *placement rule* is here in full,
> because every surprising API constraint comes from it, not from the number of
> machines.

---

## 2. What "partition" physically means

A partition is a bucket of items that live together. Which bucket an item goes in
is decided by hashing its partition key:

```
   item {"pk": "cust#1", "sk": "order#7", "total": 42}
           │
           │  hash("cust#1")
           ▼
   ┌────────────┬────────────┬────────────┬────────────┐
   │ partition0 │ partition1 │ partition2 │ partition3 │
   └────────────┴────────────┴────────────┴────────────┘
                      ▲
                      └── every item with pk="cust#1" lands HERE, always
```

Two consequences fall straight out, and they are the two halves of V1:

1. **Same partition key ⇒ same partition ⇒ physically adjacent.** Reading "every
   order for cust#1" reads one bucket. This is why it's cheap.
2. **Different partition key ⇒ possibly a different partition, and you cannot tell
   which without hashing it.** So a request that doesn't name a partition key has
   no bucket to open. It must open all of them.

Notice what the hash destroys: **order**. `hash("cust#1")` and `hash("cust#2")`
land wherever they land — adjacent customers are not adjacent partitions. That is
deliberate (it's what spreads load evenly), and it's why you can never range-scan
*across* partition keys. `WHERE pk BETWEEN 'cust#1' AND 'cust#9'` is not a slow
query in DynamoDB. It is not a query at all.

---

## 3. The sort key: order *inside* one bucket

A partition key alone gives you one item per key — a hash map, nothing more. The
sort key is what makes a partition interesting: it holds **many** items, kept in
order.

[`KeySchema`](../src/dynamodb_core/item.py) models exactly this distinction:

```python
class KeySchema(BaseModel):
    partition_key: str
    sort_key: str | None = None      # None => simple key: one item per partition
```

Picture one partition of an orders table, `pk = "cust#1"`:

```
   partition for hash("cust#1")
   ┌──────────────────────────────────────────────────────────┐
   │  sk="order#0001"   {total: 42,  status: SHIPPED}          │
   │  sk="order#0002"   {total: 17,  status: PENDING}   ← sorted
   │  sk="order#0003"   {total: 99,  status: SHIPPED}     by sk
   │  sk="order#0004"   {total:  8,  status: PENDING}          │
   │  sk="ticket#0001"  {subject: "where is my order"}         │
   └──────────────────────────────────────────────────────────┘
```

Because the partition is *sorted*, these become slices rather than scans:

| Request | What it does inside the partition |
| --- | --- |
| `sk = "order#0002"` | Binary search to one position. |
| `begins_with(sk, "order#")` | Find the first `order#`, walk until the prefix stops matching. |
| `between("order#0002", "order#0003")` | Two binary searches; return what's between. |
| `sk > "order#0002"`, `forward=False` | Seek, then walk backwards — this is how "the most recent N" works without storing a reversed copy. |

And notice what is *absent* from
[`ComparisonOperator`](../src/dynamodb_core/table.py) — the scaffold calls it out:

```python
    EQ = "="
    LT = "<"
    ...
    BETWEEN = "between"
    BEGINS_WITH = "begins_with"
```

There is no `contains`, and no `ends_with`. A sorted structure can answer
*prefixes* and *ranges* and nothing else. `ends_with` would require looking at
every item, which is a scan wearing a query's clothes. **The API's limits are the
data structure's limits** — that sentence is most of what V1 is trying to teach.

---

## 4. Why `Query` rejects a missing partition key

Now the rule makes itself. Walk the two requests side by side:

```
  Query(pk="cust#1", sk begins_with "order#")
     hash("cust#1") ──> partition 2 ──> binary search ──> 4 items read, 4 returned
     cost: O(matched)

  Query(sk begins_with "order#")            ← no partition key
     hash(???) ──> no partition to open
     the only correct answer: read partition 0, 1, 2, 3, ... all of them
     cost: O(table)
```

The second one *can* be answered. It just cannot be answered as a `Query`, because
a `Query`'s entire promise is that its cost is proportional to what it returns. So
the API gives that operation a different name — `Scan` — precisely so the cost
difference is visible in the caller's code.

This is worth sitting with, because it's the design principle underneath the whole
project: **DynamoDB refuses to hide an expensive operation behind a cheap-looking
API.** A SQL database will happily accept a query that turns into a full table
scan, and you find out in production. Here, the expensive thing is spelled
differently, so you cannot type it by accident.

Your V1 criterion says it directly:

> A `Query` without a partition key is **rejected**; `Scan` is the only keyless
> read, and its cost is observably proportional to the **table**, not the result.

And "observably" is doing work there — which brings us to `scanned_count`.

---

## 5. `scanned_count`: making the cost visible

[`Page`](../src/dynamodb_core/table.py) carries three fields, and the third is the
teaching one:

```python
@dataclass(slots=True)
class Page:
    items: list[Item]
    last_evaluated_key: ItemKey | None = None
    scanned_count: int = 0
```

A `Scan` that filters `status == "SHIPPED"` over a million items and returns 12 of
them reports `scanned_count=1_000_000, len(items)=12`. The gap between those two
numbers *is* the lesson — and in V4 it's also the bill, because capacity is charged
on what you **scanned**, not what you got back.

The scaffold's docstring is blunt about it:

> Set `scanned_count` even when items are filtered out — the gap between "scanned"
> and "returned" is the number that teaches people to stop using Scan.

---

## 6. A worked example: the same data, keyed three ways

Say you're storing orders and you have three access patterns:

- **A.** "Show me one specific order."
- **B.** "Show me all of customer 1's orders, newest first."
- **C.** "Show me every SHIPPED order across all customers."

Here's what each key design costs. This table is the reason key design happens
*before* you write a line of code:

| Key schema | A (one order) | B (customer's orders) | C (all shipped) |
| --- | --- | --- | --- |
| `pk=order_id`, no sk | ✅ one partition, one item | ❌ Scan — orders for cust#1 are scattered across every partition | ❌ Scan |
| `pk=customer_id`, `sk=order_id` | ✅ if you know the customer; ❌ if you only have the order id | ✅ one partition, sorted, sliceable | ❌ Scan |
| `pk=status`, `sk=order_id` | ❌ | ❌ | ✅ … and now **every SHIPPED order is in one partition**, which is V4's hot-partition nightmare |

Three observations, all of which the SPEC will make you feel later:

1. **No single key schema serves every pattern.** That gap is exactly what a
   secondary index exists to fill — a *second* copy of the data, keyed differently
   (V2, [indexes.py](../src/dynamodb_core/indexes.py)).
2. **A key schema is immutable.** [`TableDefinition`](../src/dynamodb_core/table.py)
   says so in its docstring: changing it means a new table and a migration. You are
   choosing this once.
3. **The "obvious" fix for pattern C is a capacity bug.** Keying by `status` puts a
   fifth of your traffic on one partition key. That's the boss fight, and you
   invented it by trying to make a query fast.

---

## 7. Composite keys: the trap you should know about before you hit it

`ItemKey` holds the key *values* as they came off the wire:

```python
class ItemKey(NamedTuple):
    partition: AttributeValue
    sort: AttributeValue | None = None
```

To store an item you need to turn that pair into something addressable and
sortable. The obvious move — glue them together — is broken, and it's worth seeing
the collision rather than being told about it:

```
  ("a",    "bc")  ──concatenate──>  b'abc'
  ("ab",   "c" )  ──concatenate──>  b'abc'     ← same bytes, different items
  ("user", "1" )  ──concatenate──>  b'user1'
  ("use",  "r1")  ──concatenate──>  b'user1'   ← same again
```

*(Verified — those really do collide.)*

"Add a delimiter" is the next instinct, and it is not sufficient either, because
whatever byte you pick, a key value is allowed to contain it:

```
  pk="a\x00b", sk="c"     ──>  b'a\x00b\x00c'
  pk="a",  sk="b\x00c"    ──>  b'a\x00b\x00c'   ← still collides
```

So the composite key needs an encoding that is **unambiguous** (distinct pairs
never produce the same bytes) *and* **order-preserving** (the byte order matches
the value order, so a range read is still a slice). Getting both at once is the
subject of the next doc — that's `encode_key`, and it is the other half of V1.

→ [01-how-order-preserving-key-encoding-works.md](01-how-order-preserving-key-encoding-works.md)

---

## 8. The type system: why numbers arrive as strings

One more V1 criterion that looks like trivia and isn't:

> Attribute types (`S`/`N`/`B`/`BOOL`/`NULL`/`L`/`M`/`SS`/`NS`) round-trip
> losslessly, and numbers keep their precision (a float is not "close enough").

Look at the wire format in [item.py](../src/dynamodb_core/item.py) — an attribute
is a single-entry map naming its type, and a number comes across as a **string**:

```json
{"pk": {"S": "cust#1"}, "total": {"N": "12345678901234567890"}}
```

That's not clumsiness. Here is what happens if you parse it as a float:

```
  input            12345678901234567890
  via Decimal      12345678901234567890     ✅ exact
  via float        12345678901234567168     ❌ off by 722
```

*(Verified.)* An IEEE-754 double has 53 bits of mantissa, so integers above
`2**53 = 9007199254740992` start losing their low bits — `2**53 + 1` round-trips as
`2**53`. For an order total, an account balance, or the atomic counter you'll build
in V3, that is silent corruption that no test catches unless you write the test on
purpose.

The rule that falls out, and it applies everywhere in this project: **anywhere a
stored number is parsed, use `decimal.Decimal`, never `float`.**

Note too which types can be *keys* — [item.py](../src/dynamodb_core/item.py) pins
it:

```python
KEY_TYPES = frozenset({AttributeType.STRING, AttributeType.NUMBER, AttributeType.BINARY})
```

Only `S`, `N`, `B`. A key has to be totally ordered and comparable, which rules out
documents (`M`, `L`) and sets (`SS`, `NS`, `BS`) — what would it even mean for one
map to sort before another? The type system is enforcing the data structure's
requirements.

---

## 9. The design space (this is your decision, not mine)

V1 asks you to choose a storage layout, and the SPEC grades the choice in
`docs/23-design.md`. The axes:

| Decision | The cheap option | The other option | What it costs you |
| --- | --- | --- | --- |
| Partition addressing | Hash the encoded pk into a dict | Ordered structure over pk | Ordered pk would let you range across partitions — which is a promise DynamoDB deliberately doesn't make. Why not? |
| Within a partition | Keep items in insertion order, filter on read | Keep them in sort-key order | Sorted insert costs on write; filtering costs on *every* read, and breaks the O(matched) promise. |
| Range lookup | Linear walk with a predicate | Seek to both bounds directly | The difference between O(partition) and O(matched + log n). Your "Query touches one partition" test is really testing this. |
| Size cap | Check after building the stored form | Check before storing | The criterion says **before** — a pathological item must not be able to allocate first and be rejected second. |

[`Table.__init__`](../src/dynamodb_core/table.py) sketches the shape the scaffold
expects and names the tools (`bisect.insort`, `bisect.bisect_left/right`) — go read
that TODO. It also gives you permission to start simpler:

> A plain dict keyed by `(pk, sk)` is a fine first step in step 1 of the order of
> attack — but it cannot answer a range query without scanning, which is exactly
> the property this vertical is about.

That's the honest path. Get `PutItem`/`GetItem` round-tripping over a plain dict
first, *then* feel the range query not working, *then* fix the layout. Feeling the
failure is worth more than skipping to the answer.

**Where I stop:** how you encode, what you nest inside what, and how you make
`Query` provably touch one partition are the V1 challenge. Use
`/hint` for a graduated nudge and `/quest` for a guided build with acceptance tests
written before you implement.

---

## 10. Mental-model summary

| The instinct | The correction |
| --- | --- |
| "The partition key is an index on a column." | It's an **address**. It decides where the item is filed, before any reading happens. |
| "`Query` needs the partition key because the API says so." | Because without it there is no partition to open. It's physics, not policy. |
| "`Scan` is just a slow `Query`." | It's a different *cost class*. `Query` is O(matched); `Scan` is O(table). Different names so you can't confuse them. |
| "I can range over partition keys." | Never. The hash destroys order across keys — that's what spreads the load. Ranges live on the **sort** key, inside one partition. |
| "`begins_with` and `contains` are both string filters." | `begins_with` is a slice of a sorted structure. `contains` would be a full walk — which is why the API doesn't offer it. |
| "I'll pick keys later and index my way out." | An index is a **second copy** with its own cost (V2), and a badly chosen key is a capacity ceiling (V4). Key design *is* the design. |
| "Storing a number as a JSON float is fine." | Above `2**53` it silently loses digits. `Decimal`, always. |

---

## 11. Where you'll build this

**Module:** [table.py](../src/dynamodb_core/table.py) — every method currently
raises `NotImplementedError`, and that's your worklist:

| `todo` | What it owes you |
| --- | --- |
| `Table.__init__` | The storage layout — the decision the SPEC grades. |
| `put_item` | Validate key attributes, enforce the size cap **before** storing, place in sort order, return the old item (V2 and V5 both need it). |
| `get_item` / `delete_item` | Point access by **full** key; a partial key is an error, not a scan. |
| `query` | Locate the partition, seek both range bounds, honour `limit`, return a resumable `last_evaluated_key`. |
| `scan` | Whole table, paginated, with an honest `scanned_count`. |
| `__len__` | Live item count — backs the item-count metric and V4's tests. |

Also wired but unfinished: the `Query` branch in
[routes.py](../src/dynamodb_core/routes.py) still has to parse a
`KeyConditionExpression` into a partition value plus an optional
`SortKeyCondition`, and reject the keyless case.

**Done-when criteria this doc unlocks** (from [SPEC.md](../SPEC.md) V1): full-key
round-trip with partial keys rejected; ordered `Query` with range conditions;
keyless `Query` rejected and `Scan` costed against the table; items provably
co-located per partition; size cap enforced before storage; lossless attribute
round-trip with number precision intact.

**Next:** [01-how-order-preserving-key-encoding-works.md](01-how-order-preserving-key-encoding-works.md)
— the encoding that makes all of the above actually sort.
