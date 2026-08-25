# Backend Fundamentals Woven Through This Project

> The horizontal checklist, taught as concepts rather than chores: why the wire
> protocol looks the way it does, what a write-ahead log actually buys you, why
> pagination is harder than it looks, what timing-safe comparison is defending
> against, and what CPython does to all of it.
> No prior knowledge assumed.
>
> Covers the **horizontal checklist** and **cross-cutting scale skills** in
> [SPEC.md](../SPEC.md). Anchored to [routes.py](../src/dynamodb_core/routes.py),
> [errors.py](../src/dynamodb_core/errors.py), [config.py](../src/dynamodb_core/config.py),
> [main.py](../src/dynamodb_core/main.py) and [pyproject.toml](../pyproject.toml).
>
> The five vertical docs (00–05) come first. This one is the connective tissue —
> the things that are true across all of them.

---

## 0. The one sentence to hold onto

**The verticals decide whether the system is correct; the horizontals decide
whether anyone can operate it.**

A store with perfect conditional writes that loses data on restart, can't be
paginated, and gives you no way to see a hot partition forming is not a usable
system. These items aren't garnish on the SPEC — several of them are the difference
between "passes tests" and "survives Tuesday."

---

## 1. Protocols: why one endpoint and a header?

The API looks wrong if you've been taught REST:

```bash
curl -XPOST localhost:8000/ -H 'X-Target: PutItem' -d '{"TableName":"orders", ...}'
curl -XPOST localhost:8000/ -H 'X-Target: Query'   -d '{"TableName":"orders", ...}'
```

One path (`/`), one method (`POST`), and the *operation* in a header. A REST purist
would want `PUT /tables/orders/items/cust%231`. Why doesn't DynamoDB do that?

| REST-style | What breaks for this API |
| --- | --- |
| The key goes in the URL | A key is a **typed, composite** value — `{"pk": {"S": "cust#1"}, "sk": {"N": "42"}}`. URL-encoding that loses the types, and a binary (`B`) key has no sane URL form at all. |
| The verb is the HTTP method | There is no HTTP method for `TransactWriteItems`, `BatchGetItem`, or `Query`. You'd end up with `POST /query` — a header by another name. |
| Resources are addressable | `Query` returns a *computed* result set, not a resource. There's nothing to `GET` twice and expect the same thing. |
| Caching via HTTP semantics | Nothing here is cacheable by an intermediary anyway — every response depends on capacity state and consistency mode. |

So the shape is **RPC over HTTP**: HTTP is the transport, not the model. The
checklist item asks you to mirror it *"so the mental model transfers to the real
thing"* — build against your node, then point the same code at real DynamoDB.

[routes.py](../src/dynamodb_core/routes.py) already implements the dispatch:

```python
@public_router.post("/")
async def data_plane(request: Request, state: StateDep, x_target: TargetHeader = None) -> Response:
    ...
    match x_target:
        case "PutItem":  ...
        case "GetItem":  ...
        case _:
            raise ValidationError(f"unknown operation {x_target!r}")
```

### The error taxonomy is a contract, not a formality

[errors.py](../src/dynamodb_core/errors.py) is fully implemented — study it, because
it encodes decisions the rest of the project depends on:

| Error | Status | Retryable | Means |
| --- | --- | --- | --- |
| `ValidationException` | 400 | ✗ | Malformed request. Retrying sends the same bad request. |
| `ResourceNotFoundException` | 404 | ✗ | No such table/index. |
| `ConditionalCheckFailedException` | 409 | ✗ | The write was **correctly refused**. Re-read, re-decide, then retry. |
| `TransactionCanceledException` | 409 | ✗ | A leg failed; none of it applied. |
| `ItemCollectionSizeLimitExceededException` | 413 | ✗ | Item over the cap. |
| `ProvisionedThroughputExceededException` | 429 | **✓** | Backpressure. Back off and retry. |

The retryable/non-retryable split *is* the client's control flow. A client that
treats 409 and 429 the same either livelocks (retrying a condition that will keep
failing) or gives up on load it should have ridden out. The handler makes it
machine-readable without a lookup table:

```python
    if exc.retryable:
        # Tell the client it may retry, so a correct backoff needs no lookup table.
        headers["retry-after"] = "1"
```

One more detail worth copying into your own services:

```python
    if exc.status_code >= 500:
        log.error("request failed", error=str(exc), kind=type(exc).__name__)
        body = {"__type": AppError.error_code, "message": AppError.message}
```

A 5xx logs the detail server-side and returns a **generic** body. Internal errors
leak internals — stack frames, table names, query fragments — and an attacker reads
error messages for free reconnaissance. 4xx errors are the client's fault and can
say what's wrong; 5xx are yours and shouldn't.

### Pagination is harder than it looks

> **Pagination** via `LastEvaluatedKey` / `ExclusiveStartKey`: a paged `Query` or
> `Scan` returns every item exactly once across pages, **even with concurrent
> writes**.

Offset-based pagination (`LIMIT 10 OFFSET 20`) breaks under concurrent writes, and
it's worth seeing how:

```
   page 1: items 1..10 returned
        │
        │  someone deletes item 3
        ▼
   page 2: OFFSET 10 — but everything shifted left by one.
           The old item 11 is now at offset 10... and item 11 is SKIPPED.

   or, on an insert before the cursor: an item is returned TWICE.
```

Key-based pagination fixes it because the cursor is a **position in the key space**,
not a count: "resume after key K." Deletions and insertions elsewhere don't move K.

Note the contract on [`Page`](../src/dynamodb_core/table.py), which the checklist
grades:

```python
    `last_evaluated_key` is `None` exactly when the result is complete — that is
    the contract the pagination horizontal is graded on, and it is *not* the same
    as "the page came back empty" (a filtered Scan can legitimately return zero
    items and still have more to read).
```

A client that stops paging on an empty page will silently truncate a filtered
`Scan`. Stop on `last_evaluated_key is None`, and only then.

### `ConsistentRead`, honoured or rejected

Honoured where you can offer it; **rejected** on a GSI, which can never provide it
([doc 03 §3](03-how-secondary-indexes-work.md#3-gsi-vs-lsi-the-difference-is-which-partition-the-entry-lands-in)).
Rejecting is the honest move: serving stale data to a caller who explicitly asked
for fresh is a silent correctness bug in *their* system.

---

## 2. Storage & durability: what "acknowledged" has to mean

> Writes are **durable before acknowledgement** (a write-ahead log), and the store
> recovers cleanly from a `kill -9` mid-write.

When you return `200 OK`, the caller stops holding the data. If the process dies a
millisecond later and the write is gone, you lied. That's the entire problem.

### Why a log, and not just "write the item to disk"?

Say a write must update the item, three index entries, and append a stream record.
Writing them in place, one at a time:

```
   crash here ──> item updated, indexes stale, no stream record
                  = a half-applied write, permanently
```

There's no ordering of in-place updates that survives a crash in the middle,
because the crash can land between any two of them.

A **write-ahead log** inverts it. Append a single record describing the *whole*
intended change, `fsync` it, *then* apply the pieces:

```
   1. append {put item X, index entries A/B/C, stream record R} to the log
   2. fsync  ← the write is now durable. THIS is the acknowledgement point.
   3. apply to the in-memory/on-disk structures at leisure
   4. crash? on restart, replay the log from the last checkpoint
```

Two properties make it work:

- **Sequential appends are fast.** One `fsync` on one growing file, versus scattered
  in-place writes with a sync each.
- **Atomicity comes from the log record, not the structures.** A record either
  landed completely or it didn't. Replay applies whole records only — so
  "item + index entries + stream record apply atomically" becomes a property of
  replay rather than something you engineer at each site.

This is why the durability item, the atomicity item, and V5's *"a write is not
acknowledged until its stream record is durable"* are all the same mechanism.
`DATA_DIR` in [.env.example](../.env.example) already says *"Where the write-ahead
log and table files live."*

The property to test is uncomfortable and non-negotiable: **actually `kill -9` the
process mid-write** and check recovery. Reasoning about the window won't find the
bug; the window is small and only real crashes land in it.

### A versioned on-disk format

> The on-disk format is **versioned**, so a future change can be detected rather
> than silently misread.

A version byte at the front of the file costs nothing today. Without it, a future
you changes the layout, an old file gets read with new code, and the bytes *parse* —
into different values. Silent corruption. With it, the same situation is a clean
startup error naming the problem.

This is the cheapest item on the entire checklist and the one whose absence is most
expensive.

---

## 3. Security: two items, both about bounds

### Authentication, and timing-safe comparison

> credentials never appear in logs or errors, and the comparison's **timing-safety**
> is a documented decision.

The subtle half. The natural way to check a credential is `if provided == expected`,
and string comparison **short-circuits at the first differing byte**. So a wrong
first character returns faster than a wrong tenth character. That difference is
measurable, and it lets an attacker recover a secret byte by byte — guess a byte,
keep whichever guess was slowest, move to the next. A 32-byte secret goes from
256³² guesses to ~32 × 256.

The fix is a comparison that always examines every byte. Python ships one:

```python
import hmac
hmac.compare_digest(provided, expected)   # verified: present, returns True/False
```

The SPEC asks for a *documented decision*, not blind adoption — because it's worth
knowing when it matters. Over a network with real jitter, a single-byte timing
difference is hard to exploit; on a local socket or with enough samples, it isn't.
Say which case you think you're in and why.

The other half is simpler and more commonly botched: **never log the credential**.
Not at debug level, not in an error message, not in a request dump. Once a secret
is in a log file it's in your log aggregator, your backups, and everyone's laptop.
[CLAUDE.md](../../../CLAUDE.md) makes this a repo-wide rule.

### Input validation is a memory-safety budget

> item size, attribute count and **depth**, key length and charset are all bounded —
> a pathological item cannot blow the node's memory budget.

`MAX_ITEM_BYTES=409600` is only the first bound. Consider a 100 KB item that is
well under the cap and still hostile:

```json
{"a": {"M": {"a": {"M": {"a": {"M": {"a": {"M": ... 10000 levels ... }}}}}}}}
```

Attribute values nest (`M` and `L` hold other values), so depth is unbounded unless
you bound it. A recursive-descent parser on 10,000 levels of nesting is a stack
overflow; a recursive *serializer* on the way back out is another. Neither is
caught by a size check.

The bounds worth having, and why each exists:

| Bound | Attack it stops |
| --- | --- |
| Item size | Straightforward memory exhaustion. |
| Attribute **count** | 100k tiny attributes: small bytes, huge dict overhead. |
| Nesting **depth** | Stack overflow on parse or serialize. |
| Key length | Unbounded keys bloat every index entry and every stream record. |
| Key charset | Depends on your encoding — see [doc 01 §6](01-how-order-preserving-key-encoding-works.md#6-composing-the-two-halves). |
| Expression size / complexity | A pathological `ConditionExpression` is unbounded CPU **inside** the write path. |

That last row is easy to forget: V3 hands users a small programming language. Bound
what they can send.

**Validate before allocating.** Your V1 criterion says the size cap is enforced
*before* storage for exactly this reason — a check that happens after you've built
the stored form has already paid the memory cost.

---

## 4. Observability: seeing the failure before it pages you

Three items, escalating in ambition.

**Structured spans.** One span per request carrying request id, operation, table,
and consumed capacity. The point of *structured* logging (`structlog`, already a
dependency) is that `log.info("put", table=t, capacity=c)` is queryable —
"p99 capacity by table" is a filter, not a regex over prose.

**Metrics at `/metrics`.** The list is specific:

| Metric | The question it answers |
| --- | --- |
| Consumed RCU/WCU | Am I near my budget? |
| Throttled request count | Is backpressure firing? |
| Per-index write amplification | What are my indexes costing me? ([doc 03 §2](03-how-secondary-indexes-work.md#2-what-it-costs-write-amplification)) |
| Stream lag | Are consumers keeping up? ([doc 05 §7](05-how-change-streams-work.md#7-lag-the-metric-that-tells-you-the-seam-is-healthy)) |
| Item / partition counts | How big is this, and how is it spread? |

**Key distribution across partitions.** This is the ambitious one and the one this
project exists to teach:

> you can *see* a hot partition forming **before** it throttles you.

Every other metric here tells you about the past. Per-partition request rate is a
*leading* indicator — the skew is visible while there's still headroom, which is the
difference between a capacity plan and a 3am page. Aggregate metrics structurally
cannot show it: a table-wide average is exactly the number that reads 8% while one
partition burns
([doc 04 §5](04-how-provisioned-capacity-and-hot-partitions-work.md#5-two-buckets-and-why-the-hot-partition-appears)).

The design tension: you cannot export a Prometheus label per partition key when
there are millions — that's a cardinality explosion that kills the metrics backend,
not just your dashboard. Top-N by rate, a histogram of per-key rates, or a skew
ratio (hottest key ÷ mean) are the usual answers. Pick one and defend it.

---

## 5. Python & runtime: what CPython does to all of this

Five items, and they're where a Python port stops being a translation.

### `pyright` strict, with justified ignores

`typeCheckingMode = "strict"` is already set in [pyproject.toml](../pyproject.toml).
The rule — *"every `# type: ignore` carries a comment justifying it"* — is really a
rule about honesty. An unexplained ignore is indistinguishable from a bug someone
silenced. The scaffold models the style:

```python
def _key(raw: Item, schema: KeySchema):  # noqa: ANN202 - returns ItemKey
```

### No blocking call on the event loop

This is the one that silently destroys a Python service. `asyncio` runs your
coroutines on **one thread**. A blocking call doesn't block one request — it blocks
*every* request, because nothing else can run:

```
   event loop:  [req A]──[req B]──[req C]──[req A]──...   healthy, interleaved

   with a blocking fsync in req B:
   event loop:  [req A]──[req B ████████ 20ms fsync ████████]──[req C]
                                   ↑ every other request waits
```

The offenders in this project are unmissable once you know to look:

| Blocking work | Where it appears |
| --- | --- |
| Disk I/O — the WAL `fsync` | Every write (§2) |
| Key encoding | Every put, every query bound ([doc 01](01-how-order-preserving-key-encoding-works.md)) |
| Expression parsing | Every conditional write ([doc 02](02-how-conditional-writes-work.md)) |
| Index maintenance | Every write, × index count ([doc 03](03-how-secondary-indexes-work.md)) |

`PYTHONASYNCIODEBUG=1` makes the loop warn about slow callbacks — the checklist
requires a clean run under it. The requirement is that moving work off the loop is
*deliberate*, "with the reason recorded," because `run_in_executor` isn't free
either: it costs a thread hop and a context switch per call, which can be worse for
work measured in microseconds.

### Bounded pools and buffers, sized on purpose

`STREAM_BUFFER_SIZE=10000` is a number in a file. Whether it's the *right* number
depends on your write rate — and [doc 05 §5](05-how-change-streams-work.md#5-two-different-limits-and-a-record-can-be-lost-to-either)
does the arithmetic: at 20,000 writes/sec on one key, 10,000 records is **0.5
seconds** of history *(verified)*. The checklist says "tuned together with the
expected write rate," and that half-second is why.

Same for V4's per-partition bucket registry — bounded, or a table with millions of
keys leaks a bucket per key
([doc 04 §7](04-how-provisioned-capacity-and-hot-partitions-work.md#7-the-design-space-your-decisions)).

The general principle: **an unbounded buffer is not a buffer, it's a delayed OOM.**
It converts backpressure (a fast, actionable error) into memory growth (a slow,
fatal one).

### Graceful shutdown

> flushes the WAL and drains in-flight requests on SIGTERM — no acknowledged write
> is lost on restart.

SIGTERM is what a container orchestrator sends before SIGKILL, and it's routine —
every deploy, every rescale, every node drain. Handling it badly means every deploy
risks data. The sequence:

```
   SIGTERM ──> stop accepting new requests
           ──> let in-flight requests finish (with a timeout)
           ──> flush and fsync the WAL
           ──> stop background tasks (stream trimming, bucket eviction)
           ──> exit 0
```

Note the interaction with §2: if acknowledgement already implies durability, most
of this is belt-and-braces. If you buffered anything, this is where the debt comes
due. And the background tasks matter — a trim timer that isn't cancelled keeps the
loop alive and turns your graceful shutdown into a hang, then a SIGKILL.

### The GIL's cost is measured, not assumed

> the contended-write benchmark states whether throughput scales with concurrency,
> and if not, **why**.

Here is the measurement, run on this machine (CPython 3.13.14, GIL enabled), same
pure-Python CPU work in 1, 2, and 4 threads:

| Threads | Elapsed | Per unit of work |
| --- | --- | --- |
| 1 | 0.065 s | 0.065 s |
| 2 | 0.131 s | 0.065 s |
| 4 | 0.263 s | 0.066 s |

*(Verified.)* Elapsed time scales **linearly** with thread count; per-unit time is
flat. That is **zero** parallelism — four threads take four times as long, exactly
as if they ran one after another, plus a little overhead. Only one thread executes
Python bytecode at a time.

What that means concretely for the boss fight's 20,000 writes/sec target: every
CPU-bound step on the write path — key encoding, expression parsing, index
maintenance, serialization — is serialised no matter how many cores the box has.
Threads still help for I/O (a thread waiting on `fsync` releases the GIL), which is
why moving disk I/O off the loop is worth it and moving pure computation off it
often isn't.

The SPEC's position on this is the best thing in it, and it's repo policy:

> Because a Python project's ceiling is lower, **boss-fight numbers are not scaled
> down** on conversion: where CPython can't reach a target, the gap and its cause
> (GIL, GC, allocation, a blocking call on the loop) is the finding, recorded in
> that project's `docs/23-benchmarks.md`.

You are not expected to hit 20,000 writes/sec in CPython on one core's worth of
bytecode. You **are** expected to know exactly what stopped you, with a `py-spy`
flamegraph and a `memray` run naming the top bottleneck (both already dev
dependencies, and both required by the Definition of done). "We got to N and here
is the profile showing why" is a better engineering artifact than a green
checkbox.

Worth knowing before you profile: CPython 3.13 offers a free-threaded (no-GIL)
build, and this interpreter is **not** it (`GIL disabled: False`, verified). If you
want to make that part of the finding, that's a legitimate and interesting
experiment — just report which interpreter produced which numbers.

---

## 6. Mental-model summary

| The instinct | The correction |
| --- | --- |
| "One endpoint plus a header is bad REST." | It's RPC over HTTP. Typed composite keys don't fit in URLs and there's no HTTP verb for `TransactWriteItems`. |
| "An error is an error." | The retryable/non-retryable split **is** the client's control flow. 409 and 429 demand opposite responses. |
| "5xx should explain what went wrong." | Not to the client. Log the detail, return a generic body — error text is free reconnaissance. |
| "`LIMIT`/`OFFSET` is pagination." | It skips and duplicates items under concurrent writes. Paginate by **key position**. |
| "Empty page means done." | Stop on `last_evaluated_key is None`. A filtered `Scan` returns empty pages mid-stream. |
| "Write the data to disk, then reply." | In-place writes can't be atomic across structures. Append to a log, `fsync`, *then* apply. |
| "A format version byte is over-engineering." | It's the cheapest item here, and its absence is silent corruption in a year. |
| "`==` is fine for comparing a secret." | It short-circuits, leaking the secret byte by byte. `hmac.compare_digest`. |
| "A size cap bounds the input." | Depth, attribute count, key length and expression complexity are all separately unbounded. |
| "Aggregate metrics show me the system's health." | A table-wide average is exactly what hides a hot partition. Per-partition rate is the leading indicator. |
| "`async def` makes it non-blocking." | `async def` around a blocking `fsync` blocks the whole loop, every request. |
| "I'll size the stream buffer later." | 10,000 records at 20k writes/s is 0.5 s of history. Size it against the write rate or it's a data-loss bug. |
| "More threads, more throughput." | Verified: 1→4 threads, elapsed 0.065s→0.263s. Zero parallelism on Python bytecode. |
| "Missing the boss number means failing." | Missing it **without explaining why** means failing. The gap with a profile is the deliverable. |

---

## 7. Where you'll build this

Unlike the verticals, these don't live in one module — that's the point of calling
them horizontal:

| Area | Where |
| --- | --- |
| Protocol dispatch, pagination | [routes.py](../src/dynamodb_core/routes.py) — `Query`/`UpdateItem`/`TransactWriteItems` branches still raise |
| Error taxonomy | [errors.py](../src/dynamodb_core/errors.py) — **already complete**; read it, extend it if you add errors |
| WAL, recovery, versioned format | New; `DATA_DIR` in [config.py](../src/dynamodb_core/config.py) is reserved for it |
| Auth, input bounds | [routes.py](../src/dynamodb_core/routes.py) as middleware/dependencies; `MAX_ITEM_BYTES` is the one bound that exists |
| Telemetry, `/metrics` | [main.py](../src/dynamodb_core/main.py) with `common-telemetry`; `prometheus-client` is already a dependency |
| Graceful shutdown | [main.py](../src/dynamodb_core/main.py) lifespan — the same path `tests/conftest.py` exercises |
| Type/lint/test gate | `make verify` = `fmt-check → lint → types → test`, the gate CI runs |

**The gate to keep green as you go:**

```bash
make verify     # ruff format check -> ruff lint -> pyright strict -> pytest
```

Item 4 of the Definition of done is *"`make verify` is green … no
`NotImplementedError` remains on a checked path."* Run it from the first commit, not
the last — `pyright` strict is much easier to satisfy incrementally than
retroactively.

**Also required by the Definition of done:** `docs/23-design.md` recording the four
graded decisions (key encoding & partition layout; index maintenance; condition
evaluation & transaction protocol; capacity model & throttle algorithm),
`docs/23-benchmarks.md` with the boss-fight numbers and hardware, and a committed
`py-spy` flamegraph plus `memray` run naming the top bottleneck.

**Stuck?** `/hint` for graduated nudges, `/quest` to run a vertical with acceptance
tests written before you implement, `/spec-review` once you have something to grade.

---

## 8. The reading order

| Doc | Vertical | Concept |
| --- | --- | --- |
| [00 — Partition keys decide placement](00-how-partition-keys-decide-placement.md) | V1 | The key is an address, not an index |
| [01 — Order-preserving key encoding](01-how-order-preserving-key-encoding-works.md) | V1 | Sort order must survive serialization |
| [02 — Conditional writes](02-how-conditional-writes-work.md) | V3 | Compare-and-set is the whole story |
| [03 — Secondary indexes](03-how-secondary-indexes-work.md) | V2 | An index is a second table you pay for |
| [04 — Capacity & hot partitions](04-how-provisioned-capacity-and-hot-partitions-work.md) | V4 | Capacity is per-partition |
| [05 — Change streams](05-how-change-streams-work.md) | V5 | Per-key ordering as an integration seam |
| **06 — this doc** | horizontal | Everything that spans all of them |

V3 comes before V2 deliberately — it's the SPEC's *Suggested order of attack*,
because index maintenance has to respect conditional writes and retrofitting that
is painful.
