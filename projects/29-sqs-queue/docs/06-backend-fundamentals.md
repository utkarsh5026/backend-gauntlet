# Backend Fundamentals Woven Through This Project

> The horizontals: the wire protocol and its unusual batch contract, authentication that
> lives in another project, the caps that keep a hostile caller from spending your
> memory, the metrics that are *approximate* on purpose, and the async-runtime discipline
> the boss fight measures. No prior knowledge assumed.
>
> Prepares you for the **horizontal checklist** in [`SPEC.md`](../SPEC.md) — the items
> that aren't any single vertical but show up in all six. Anchored to
> [`protocol.py`](../src/sqs_queue/protocol.py), [`errors.py`](../src/sqs_queue/errors.py),
> [`routes.py`](../src/sqs_queue/routes.py), [`config.py`](../src/sqs_queue/config.py)
> and [`main.py`](../src/sqs_queue/main.py).

---

## The one sentence to hold onto

**Every guarantee this service makes has to survive being expressed through a protocol to
a client you don't control — so each one ends up as a token, a limit, or a check, never
as an assumption about how callers behave.**

---

## 1. The wire protocol, and why it looks like that

Modern SQS speaks **AWS JSON 1.0**: one endpoint, `POST /`, with the verb in a header.

```http
POST / HTTP/1.1
X-Amz-Target: AmazonSQS.SendMessage
Content-Type: application/x-amz-json-1.0

{"QueueUrl": "http://localhost:9029/000000000000/orders", "MessageBody": "hello"}
```

No `/queues/orders/messages`. No `PUT`. The verb is a string in a header and the body is a
document. To anyone raised on REST this looks wrong, and it's worth understanding why it
isn't: AWS's protocols are designed around **generated SDKs**, not around humans reading
URLs. Every action has one shape, the code generator emits one method per action, and the
transport never has to encode meaning. It has outlived several generations of HTTP
fashion by refusing to encode any of them.

(Project 25 speaks the *older* AWS Query protocol — form-encoded, `Action=AssumeRole` in
the body. Same company, a decade apart. Comparing the two is a free lesson in what
changed and what didn't.)

`parse_target` in [`protocol.py`](../src/sqs_queue/protocol.py) is already written, and
the `Action` enum next to it is deliberate rather than clever:

```python
# NOT this:
handler = getattr(handlers, request_params["Action"])
```

A dispatcher that reaches into a namespace with a caller-supplied string is one typo away
from being an arbitrary-call gadget. An explicit enum means an unknown action is a clean
`InvalidParameterValue`, not an `AttributeError` in a traceback.

### The real bar

The checklist item that matters is not "I implemented the protocol". It's:

```bash
aws --endpoint-url http://localhost:9029 sqs create-queue --queue-name orders
```

**A real SDK, unmodified, against your endpoint.** A protocol only you can speak proves
nothing — it proves you and your tests agree. `boto3` is already a dev dependency in
[`pyproject.toml`](../pyproject.toml) for exactly this, and
[`tests/conftest.py`](../tests/conftest.py) documents the fixture you'll want when you get
there (boto3 is synchronous, so it needs a real socket rather than `ASGITransport`).

---

## 2. Partial failure: the batch contract

`SendMessageBatch` takes up to 10 entries. Three of them are malformed. What's the
response?

The intuitive answer — 400, the request was bad — is wrong, and wrong in a way that
actively causes duplicates:

```
   client sends batch of 10  ->  400 Bad Request
   client retries the batch  ->  the 7 good messages are enqueued AGAIN
```

You rejected 7 valid messages because of 3 bad ones, and the client's only sane recovery
duplicates them.

So the contract is: **HTTP 200, with both lists.**

```json
{
  "Successful": [ {"Id": "1", "MessageId": "...", "MD5OfMessageBody": "..."} ],
  "Failed":     [ {"Id": "2", "Code": "InvalidParameterValue",
                   "Message": "...", "SenderFault": true} ]
}
```

Partial failure is the *normal* case here, not an error case. `BatchResult` in
[`protocol.py`](../src/sqs_queue/protocol.py) models exactly that shape, and the module
docstring says it twice on purpose.

Which leaves a line you have to draw: what makes the **whole batch** unprocessable versus
what's a per-entry failure?

| Whole batch fails | Per-entry failure |
|---|---|
| No entries at all (`EmptyBatchRequest`) | A body over the size cap |
| More than 10 entries | A missing `MessageGroupId` on a FIFO queue |
| Duplicate entry ids (`BatchEntryIdsNotDistinct`) | A receipt handle that doesn't parse |
| A malformed entry id | Anything the action itself refuses |

The rule underneath: **envelope problems fail the batch, content problems fail the
entry.** `parse_batch_entries` already implements the left column; the right column is
yours, inside each batch handler.

And note `SenderFault` in the wire shape above — a real AWS field, and one
`BatchResult.failed` leaves you to populate. It tells the client whether retrying could
*ever* work. Same idea as `retryable` on `AppError` in
[`errors.py`](../src/sqs_queue/errors.py): a client that retries a permanent failure is a
client in an infinite loop, and the flag is what tells them apart.

---

## 3. Errors as an API

`errors.py` is already complete, and three of its decisions are worth reading rather than
skimming, because they're the ones people get wrong.

**Client errors are 400, not 404.** `QueueDoesNotExist` returns 400. That offends HTTP
sensibilities and it's the protocol's convention: the *transport* succeeded, the *request*
was bad. The SDK reads `__type` to decide what happened, not the status code.

**`x-amzn-errortype` is load bearing.** An SDK decides whether to retry by reading it. A
wrong code isn't cosmetic — it's the difference between a client backing off and a client
giving up.

**Never leak an internal message on a 5xx.** The handler logs the instance detail and
returns the *class* default, which you wrote and know is safe. On this service, an
exception message could contain a queue name or a receipt handle the caller had no
business seeing.

And one this project cares about more than most: **the dangerous accident is a 200, not a
500.** A `SendMessage` that returns success after failing to enqueue has silently dropped
a customer's message — and no retry will ever happen, because the client was told it
worked. When in doubt, fail loudly.

---

## 4. Authentication lives in another project

The security checklist says every request is SigV4-authenticated and every action
authorized. Both of those are **project 25**, not this one.

`REQUIRE_SIGV4` in [`.env.example`](../.env.example) is off by default so the scaffold is
pokeable with `curl`. Turning it on is the checklist item, and
`IAM_AUTHZ_URL` points at 25's authorization endpoint on `:9026`.

Two things to get right, and both are placement rather than cryptography:

**Authenticate first, before anything else.** The TODO in `dispatch`
([`routes.py`](../src/sqs_queue/routes.py)) sits above `parse_target` deliberately. An
auth check placed after a queue lookup is an **existence oracle**: an unauthenticated
caller learns which queues exist by watching which error they get. Project 25's own
scaffold makes the same point from the other side, and its smoke tests assert it.

**Authentication is not authorization.** SigV4 tells you *who* is calling. It says nothing
about whether they may call `ReceiveMessage` on `payments`. The checklist is explicit that
sending and receiving are different permissions, checked **per queue** — a service that
authenticates and then lets any valid signature do anything has built a very expensive
`if request.headers.get("authorization")`.

---

## 5. Caps, quotas, and who is paying for what

Look at [`config.py`](../src/sqs_queue/config.py) and notice how much of it is limits.
That's the shape of a service exposed to callers you don't control: every caller-supplied
number is a number *somebody else chose for you*.

Two different jobs hide in there:

**Protocol caps** — bounds on one request:

| Cap | Value | Why |
|---|---|---|
| `max_message_bytes` | 256 KB | larger payloads go to object storage with a pointer (project 06) |
| `max_batch_entries` | 10 | bounds the work one request can demand |
| `max_receive_wait_time_seconds` | 20 s | the maximum time one idle client holds a connection |
| `max_visibility_timeout_seconds` | 12 h | the maximum time a crashed consumer can hide a message |
| `max_delay_seconds` | 15 min | delay is not scheduling — that's project 21 |

**Quotas** — bounds on accumulated *state*, and these are the interesting ones:

| Quota | What it stops |
|---|---|
| `max_inflight_per_queue` (120,000) | a consumer that receives and never deletes, acquiring server state for free |
| `max_waiters` (20,000) | ten thousand idle connections becoming twenty million |
| `max_dedup_entries` | a flood of distinct dedup ids filling the window ([doc 04 §6](./04-deduplication-windows.md)) |
| `max_queues` | one caller consuming the node's namespace |

The distinction that matters: a protocol cap is refused *now*, but a quota is about
something a caller **holds**. That's why `OverLimit` in
[`errors.py`](../src/sqs_queue/errors.py) is marked `retryable` — the caller can get in
once somebody else's messages are deleted. It is the queue applying backpressure, and
answering it correctly is what stops one badly-behaved consumer taking the node down.

Two more from the checklist, easy to skip and both real:

- **Message bodies are never logged.** They carry customer data. Queue names and message
  ids are fine and are what you'll actually want at 3am.
- **A receipt handle must be non-enumerable** — holding one tells you nothing about any
  other message. See [doc 00 §4](./00-receipt-handles-and-leases.md).

---

## 6. Why the metrics are called "approximate"

`ApproximateNumberOfMessages`. `ApproximateNumberOfMessagesNotVisible`.
`ApproximateAgeOfOldestMessage`. AWS put the word in the name, and once you've built this
you know why.

The exact version is a walk:

```python
available = sum(1 for m in queue.messages.values() if m.state is AVAILABLE)  # O(n)
```

At a million messages, with Prometheus scraping every 15 seconds, you have made
`/metrics` the most expensive route in the service — and it's the route you hit *hardest*
when the service is already unhealthy and you're staring at dashboards.

So everyone who has built this maintains counters instead. `MessageCounts.apply` in
[`state.py`](../src/sqs_queue/state.py) is already written and is the entire idea: every
state transition adjusts the number, nothing ever counts.

The cost is honesty about concurrency. A counter read mid-transition is *slightly* wrong.
Hence "approximate" — and now, when the AWS console lies to you a little, you know
exactly which trade produced the lie.

> `Queue.oldest_sent_at` in [`state.py`](../src/sqs_queue/state.py) is deliberately still
> a scan, with a docstring saying so. It's the one gauge the scaffold leaves as an `O(n)`
> walk — a worked example of the problem, waiting to be fixed the same way.

Which numbers to publish, from the observability checklist:

| Kind | Metric | What it tells you |
|---|---|---|
| Gauge | available / not-visible / delayed | depth, and whether consumers are keeping up |
| Gauge | **age of oldest message** | **the** lag signal — if it climbs, you're behind or a group is stuck |
| Counter | sends, receives, deletes, empty receives | traffic shape |
| Counter | expired leases | non-zero means consumers are dying or timeouts are too short |
| Counter | rejected handles, **split stale vs. malformed** | stale = your timeout is short; malformed = a client bug or a prober |
| Histogram | end-to-end latency (send → delete) | what your users actually experience |
| Histogram | messages-per-receive | pinned at 1 under load = batching isn't working |

That "split stale vs. malformed" row is the one people collapse into a single counter and
then can't diagnose. They're opposite problems ([doc 00 §5](./00-receipt-handles-and-leases.md)).

---

## 7. The async-runtime axis

This is the day-job curriculum, and the boss fight measures it directly.

**Nothing blocks the event loop.** One coroutine that occupies the loop for 40 ms stalls
*every* connection for 40 ms — including health checks, which is how a slow function
becomes a restart loop. Two places in this service are most likely to do it:

- hashing a 256 KB body on the send path ([doc 04 §3](./04-deduplication-windows.md))
- a deadline tick with 100,000 entries due ([doc 01 §7](./01-deadline-engines.md)) — which
  is what `MAX_DEADLINES_PER_TICK` exists to bound

`PYTHONASYNCIODEBUG=1` makes the loop complain about slow callbacks. Running the drain
scenario under it and getting a clean log is a Done-when criterion, not a nice-to-have.

**Everything bounded, sized on purpose.** Waiters, in-flight per queue, dedup entries,
deadlines per tick — each with a documented limit *and* a defined behaviour at the limit.
"It grows until something breaks" is not a behaviour.

**Graceful shutdown drains.** The order in [`main.py`](../src/sqs_queue/main.py)'s lifespan
is graded: release parked waiters with an **empty response** (not a reset — see
[doc 02 §5](./02-long-polling.md)), then stop the deadline loop, and deliberately leave
in-flight leases alone. They expire on their own; that's what V1 built.

There's a subtlety already handled for you in `_serve`: `timeout_keep_alive` is set from
`max_receive_wait_time_seconds + 10`. The default is shorter than a 20-second long poll,
so without it uvicorn closes connections out from under waiters that are behaving
perfectly — and from the client's side that looks exactly like the service dropping
requests under load.

**A profiling gate.** `make profile` runs `py-spy` against a live process; `memray` gives
you allocations. The Definition of done asks you to *name the top three costs* and state
the memory-per-message number. The intuitive answer is wrong often enough to be worth
measuring — most people guess their own logic and find the JSON parser.

---

## 8. The uvloop trap (read before you debug it for an hour)

Production runs **uvloop** (`uvicorn[standard]` installs it, and `loop="auto"` picks it).
Pytest runs the **stdlib** loop.

They are not the same implementation. uvloop does not implement the `loop.sock_*` family,
so code using those passes every test and dies in Docker. This project is HTTP-only so
you're unlikely to hit that specific one — but the general lesson stands and is worth
holding: **your tests and your production process are running different event loops.**
Anything you rely on that isn't in the documented `asyncio` surface is a thing you have
only tested on one of them.

---

## 9. Mental model summary

| Question | Answer |
|---|---|
| Why a header verb instead of REST paths? | Generated SDKs, not human-read URLs |
| What proves the protocol is right? | `boto3` / `aws-cli` working unmodified |
| What does a batch with 3 bad entries return? | 200, with `Successful` and `Failed` |
| Envelope vs. content errors? | Envelope fails the batch; content fails the entry |
| Where does auth go? | First, before any lookup — otherwise it's an existence oracle |
| Is authenticated the same as authorized? | No. Per-queue, per-action |
| What's the difference between a cap and a quota? | A cap bounds a request; a quota bounds what a caller *holds* |
| Why "Approximate"? | Counters maintained in `O(1)` — exact would mean an `O(n)` walk per scrape |
| What's *the* lag signal? | Age of the oldest message |
| What must never be logged? | Message bodies |
| What does shutdown owe a parked waiter? | An empty response |

---

## Where you'll build this

Not one module — these thread through all six verticals. Concretely:

- **Protocol & batches:** the `_*_batch` handlers in
  [`routes.py`](../src/sqs_queue/routes.py); the `Failed`-list logic in each
- **Auth:** the `TODO(security)` at the top of `dispatch` in
  [`routes.py`](../src/sqs_queue/routes.py)
- **Caps:** the edge of every handler, using [`config.py`](../src/sqs_queue/config.py)
- **Metrics:** `MessageCounts` is done; the counters and histograms are yours, and
  `Queue.oldest_sent_at` wants making incremental
- **Runtime:** measured, not written — the boss fight's four scenarios plus
  `make profile`

The horizontal checklist in [`SPEC.md`](../SPEC.md) has 28 boxes across Protocols,
Security, Observability and Python & runtime. Each is done when its criterion is
observably true — same rule as the verticals.

`/hint 29` for a nudge on any of them · `/spec-review 29` once you have something to
review.
