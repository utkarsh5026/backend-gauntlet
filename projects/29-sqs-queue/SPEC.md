<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 29 — Managed Queue Service (SQS)

> Project 04 already taught you that a queue is a lease, a retry policy and a
> dead-letter queue. This one is about what happens when the queue stops being a
> library in your process and becomes **a service that runs someone else's
> workers**. The consumer is now a stranger on the far side of a socket: it can
> vanish mid-message, take a lease it never gives back, delete work it no longer
> owns, or park ten thousand idle connections on you and wait. You cannot call
> its code, you cannot see its stack, and you cannot trust a single field it
> sends. Every guarantee you want — ordering, at-least-once, no-double-delete,
> "don't burn a CPU while nothing is happening" — has to survive being expressed
> entirely through a request/response protocol and enforced against a client
> that is buggy, hostile, or simply gone. That is the rung: not queue theory,
> but queue theory that holds up across a network boundary you do not control.

## What it does (the easy part)

- **Control plane:** `CreateQueue` / `GetQueueUrl` / `ListQueues` /
  `GetQueueAttributes` / `SetQueueAttributes` / `DeleteQueue`. A queue is a name,
  a URL, and a bag of attributes (visibility timeout, retention, delay, redrive).
- **Data plane:** `SendMessage` / `ReceiveMessage` / `DeleteMessage` /
  `ChangeMessageVisibility`, each with a `…Batch` sibling that takes up to 10
  entries and answers with per-entry successes *and* per-entry failures.
- **Standard queues:** at-least-once, best-effort ordering, no throughput ceiling.
- **FIFO queues** (`.fifo` suffix): strict order within a `MessageGroupId`,
  parallelism across groups, and a deduplication window on send.
- **Long polling:** a receive can wait up to 20 seconds for a message to arrive
  rather than returning empty immediately.
- `GET /healthz` for liveness, `GET /metrics` for Prometheus.

> **This is not project 04 again.** 04 owns the *substrate*: `SKIP LOCKED`,
> durable rows, a reaper, backoff. Here the store is **in memory on purpose** —
> durability is 04 and 08, replication is 07 and 09 — because every vertical
> below is about the **protocol and the semantics**, which 04 never touches: a
> lease you can only release with the token you were handed, ordering keys that
> buy parallelism, a dedup window with bounded memory, ten thousand parked
> waiters, and a control plane whose attributes are a versioned contract. If you
> find yourself writing a claim query, you are in the wrong project.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the
> system must do*, never *how*; figuring out the how is the entire point. A box
> only flips to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. Receipt handles — *a lease you can only release with the token you were handed*

In `src/sqs_queue/inflight.py`, build the message lifecycle: **available →
in-flight (invisible) → deleted**, or back to available when the visibility
timeout expires.

The obvious API is `delete(message_id)`, and it is wrong in a way that only shows
up under load. Consider: worker A receives message M and starts a slow job. A's
visibility timeout expires, so M becomes visible again and worker B receives it.
A now finishes and calls `delete(M)` — and deletes the copy **B** is still
working on. B finishes, deletes nothing, and its lease expires; M is delivered a
third time. One slow worker has turned an at-least-once queue into an
at-least-forever queue, and nothing in the logs looks wrong.

The fix is that a receive does not return a message id to delete by — it returns
a **receipt handle**, a token minted for *that specific delivery*. A delete or a
visibility change carries the handle, and a handle that no longer corresponds to
the current delivery is refused. Think about what the handle must therefore
contain, what it must be impossible for a client to fabricate or guess, and what
the right answer is to a handle that is merely *stale* (a slow worker being
honest) versus one that is *malformed* (someone poking at you).

Each delivery also increments the message's **receive count** — the number V6's
redrive policy counts against.

**Done when ALL true:**
- [ ] A receive returns a **receipt handle** distinct per delivery; two deliveries of the same message yield two different handles.
- [ ] `DeleteMessage` with the **current** handle removes the message; with a **superseded** handle it does **not** delete the live delivery.
- [ ] Deleting with the same current handle twice is **idempotent** — the second call is not an error, and cannot delete a later delivery.
- [ ] `ChangeMessageVisibility` extends or shortens the lease **only** for the holder of the current handle; setting it to `0` makes the message immediately receivable again.
- [ ] A handle is **unforgeable**: a client cannot construct a valid handle for a message it has never received.
- [ ] An in-flight message is **invisible** — no concurrent receive returns it until its lease expires or is zeroed.
- [ ] The per-message **receive count** increments once per delivery and is reported on receive.

**Proof:** a test that reproduces the stale-delete race (slow worker A, expiry,
worker B, A deletes) and asserts B's delivery survives; a forgery test; an
idempotent-delete test. The handle's contents and the stale-vs-malformed decision
are written up in `docs/29-design.md`.

*Concept to internalize:* a lease identified by **delivery generation**, not by
message identity — and why every distributed hand-off protocol (SQS receipt
handles, Kafka fencing epochs, lease tokens in Chubby/etcd) ends up minting a
token that the next generation invalidates.
**Stretch:** make the handle carry its own expiry so an obviously-dead handle is
rejected without a table lookup at all.

### V2. The deadline engine — *one clock for a million timers*

Four different things in this service happen "later": an in-flight message
becomes visible again, a delayed message becomes available, an old message is
dropped at the end of its retention, and a dedup id ages out of its window. In
`src/sqs_queue/timers.py`, build the single structure that drives all four.

The naive version is a loop that walks every message every tick and compares
timestamps. It is `O(n)` per tick, so the cost of an *idle* queue grows with the
number of messages sitting in it — and at a million messages, a service with no
traffic at all is pinned at 100% CPU doing nothing. The other naive version is a
`asyncio.sleep()` task per message, which is `O(n)` in tasks and memory instead,
and falls over in a different place.

What you want is a structure where inserting a deadline and finding the
next-due deadline are both cheap, and where the tick cost is proportional to the
number of things **actually due** rather than the number of things scheduled. A
priority heap is the straightforward answer; a **hierarchical timing wheel** is
the one Kafka's purgatory and every serious timer subsystem reaches for, and the
comparison between them is worth making with numbers.

Then the hard part, which is not the data structure: a deadline that fires must
not race a concurrent operation. A visibility timeout firing at the same instant
its holder deletes the message must produce exactly one outcome, not a
double-delivery and not a lost message. V1's delivery generation is the tool;
using it correctly here is the work. Cancelled and rescheduled deadlines
(`ChangeMessageVisibility` moves one) must not leave garbage behind that grows
without bound.

**Done when ALL true:**
- [ ] Visibility expiry, `DelaySeconds`, retention and the dedup window are **all driven by one structure** — not four loops.
- [ ] Tick cost is proportional to deadlines **due**, not deadlines **scheduled**: CPU while idle is flat as the queue grows from 10K to 1M messages.
- [ ] A rescheduled or cancelled deadline leaves **no unbounded residue** — memory after N reschedules is bounded by live messages, not by N.
- [ ] A deadline firing **concurrently** with the operation it races (delete, visibility change) yields exactly one outcome — never a double delivery, never a lost message.
- [ ] Expiry is **timely under load**: the delay between a deadline and its effect stays bounded while the service is saturated.
- [ ] The engine never blocks the event loop — a tick that has 100K deadlines due does not stall unrelated receives.

**Proof:** a scaling test (10K vs 1M scheduled deadlines) showing flat idle CPU
and bounded memory; a concurrency test firing expiry against a simultaneous
delete; heap-vs-wheel numbers in `docs/29-benchmarks.md`.

*Concept to internalize:* timers as a data-structure problem, why "scan
everything on a tick" is the default that quietly caps your scale, and how
generation checks turn a timer/operation race into a decidable one.

### V3. Long polling — *ten thousand consumers waiting, and nothing burning*

In `src/sqs_queue/polling.py`, implement `ReceiveMessage` with
`WaitTimeSeconds` (0–20): if the queue is empty, the caller **waits** for a
message rather than being told "empty" immediately.

This exists because short polling forces an ugly choice on every consumer: poll
often and pay for a flood of empty responses, or poll rarely and add that latency
to every message. Long polling collapses the choice — but it moves the cost onto
*you*, and now the failure modes are yours.

Get all four right. **Wake exactly enough:** one message arriving must not wake
ten thousand waiters to have 9,999 discover an empty queue and park again — the
thundering herd, in its purest form. **Lose no wakeup:** the interval between "I
checked and it was empty" and "I am now parked" is a race, and a message that
arrives inside it must not leave a waiter asleep until its timeout. **Return
early:** a waiter asked for 10 messages and one arrived should not sit for the
full 20 seconds hoping for nine more. **Leave cleanly:** a client that
disconnects mid-wait must not leave its waiter, its timer, or its slot behind.

**Done when ALL true:**
- [ ] `WaitTimeSeconds=0` returns immediately (possibly empty); `>0` waits up to that long and returns as soon as a message is available.
- [ ] A waiter is **parked**, not spinning: 10,000 waiters on empty queues hold CPU at **≈0%**, measured.
- [ ] One `SendMessage` into a queue with N waiters wakes a **bounded** number of them — not all N.
- [ ] **No lost wakeup:** a message that arrives during the check-then-park window still returns a waiter promptly (proven with a deliberately interleaved test).
- [ ] A receive returns **as soon as it has at least one** message, without waiting out the remaining time.
- [ ] `MaxNumberOfMessages` (1–10) is honoured and never exceeded.
- [ ] A **disconnecting client** leaves nothing behind: waiter count and timer count return to baseline after N aborted long polls.
- [ ] Wait time is **capped** at 20s regardless of what the caller asks for, and a queue's default applies when the caller asks for nothing.

**Proof:** an idle-CPU measurement at 10K waiters; a wake-fanout test counting
how many waiters woke for one message; an interleaving test for the lost-wakeup
race; a leak test asserting waiter/timer counts return to baseline.

*Concept to internalize:* the park/notify pattern and the lost-wakeup race that
every implementation of it has to answer, why "wake all and let them fight" is
the seductive wrong answer, and long polling as the general shape of
latency-without-load (SQS long poll, Redis `BLPOP`, Postgres `LISTEN`, HTTP long
poll).

### V4. FIFO — *ordering keys, and the parallelism they buy back*

In `src/sqs_queue/fifo.py`, build FIFO queues: strict ordering **within** a
`MessageGroupId`, full parallelism **across** groups.

Start from why strict global FIFO is a trap. If message *n+1* may not be
processed until *n* is done, then the queue has exactly one useful consumer, and
your throughput is one worker's throughput no matter how many you run — you have
bought ordering by giving up the entire point of a queue. The universal
resolution is to make ordering a property of a **key** rather than of the queue:
messages sharing a group are strictly ordered, messages in different groups are
independent, and the number of groups becomes your parallelism ceiling.

The mechanism to work out: what must be true about a group while one of its
messages is in flight, and what a *second* receiver asking for work should be
handed. Then face the consequence squarely — **head-of-line blocking** is not a
bug to fix here, it is the cost of the guarantee: one poison message at the front
of a group stalls that group until it is deleted or redriven, while every other
group keeps running. Demonstrate it rather than avoid it, and make sure the
blast radius is exactly one group.

FIFO also assigns a **sequence number** that increases within a group, and
enforces the `.fifo` naming rule that makes a queue's contract visible in its
name.

**Done when ALL true:**
- [ ] Messages in one `MessageGroupId` are delivered in **send order**, with any number of concurrent receivers.
- [ ] Different groups are delivered **concurrently** — throughput scales with the number of groups, shown in the bench.
- [ ] While a group has a message in flight, no **later** message from that group is delivered to anyone.
- [ ] **Head-of-line blocking is demonstrated and bounded:** a stuck message stalls its own group and **no other** group.
- [ ] Each message carries a **sequence number** that strictly increases within its group.
- [ ] A FIFO queue **requires** a `MessageGroupId` on send and refuses the message without one; a standard queue ignores it.
- [ ] FIFO queues are only creatable with the `.fifo` suffix, and standard queues without it.

**Proof:** an ordering test (1,000 groups × 100 messages, ≥8 concurrent
receivers, zero out-of-order); a head-of-line test showing one stalled group and
others draining; a groups-vs-throughput curve in `docs/29-benchmarks.md`.

*Concept to internalize:* ordering and parallelism as directly opposed, per-key
ordering as the universal escape (SQS `MessageGroupId`, Kafka partition keys,
Pulsar `Key_Shared`, Pub/Sub ordering keys), and head-of-line blocking + hot keys
as the price you agree to pay.

### V5. The deduplication window — *"exactly-once" with a receipt and an expiry date*

In `src/sqs_queue/dedup.py`, build send-side deduplication for FIFO queues: a
message with the same `MessageDeduplicationId` (explicit, or a content hash when
the queue enables content-based dedup) sent again **within the window** is
accepted, acknowledged with the **original** message id, and not enqueued.

This is the honest version of "exactly-once", and understanding exactly how
narrow the claim is matters more than the code. It removes **producer** retries
— the client whose `SendMessage` timed out and sent again — and it does nothing
whatsoever about consumer-side duplicates, which V1's visibility timeout will
still hand you. Nor is it forever: it is a 5-minute window, chosen because that
is roughly how long a retrying client keeps trying, and a duplicate that arrives
at minute six is a new message. Write down what that means for a caller who
believes the marketing.

The engineering problem is that the window is a set that must **not grow without
bound** while remaining correct: entries expire (V2 owns the clock), memory is
proportional to the *window*, not to the *lifetime*, and a lookup on the send
path must be cheap enough to sit in front of every write. Think about what the
dedup id is derived from when content-based, and what an attacker who can choose
your dedup ids could do with them.

**Done when ALL true:**
- [ ] A duplicate send inside the window is **accepted, not enqueued**, and returns the original `MessageId`.
- [ ] The same id sent **after** the window expires produces a **new** message with a new id.
- [ ] Content-based dedup derives the id from the message body **deterministically** — same body, same id, without the client sending one.
- [ ] An explicit `MessageDeduplicationId` **overrides** content-based derivation.
- [ ] Memory used by the window is bounded by the **window length × send rate**, not by total messages ever sent — measured across a long run.
- [ ] Dedup applies **across** message groups, not within one — two groups cannot smuggle the same dedup id past each other.
- [ ] The scope of the guarantee is documented: **producer-retry dedup only**, with consumer-side duplicates still possible and idempotency still required.

**Proof:** a duplicate-send test asserting one enqueue and a shared message id; a
window-expiry test; a long-run memory measurement in `docs/29-benchmarks.md`; the
scope statement in `docs/29-design.md`.

*Concept to internalize:* dedup as a bounded time-windowed set, why every
"exactly-once" in the industry is scoped to one boundary and one window, and the
difference between removing producer retries and removing duplicate *processing*.

### V6. The control plane — *attributes as a contract, redrive as the release valve*

In `src/sqs_queue/control.py`, build the plane that creates and configures
queues, and the redrive path that a poison message eventually falls down.

Three things here are more subtle than they look. **`CreateQueue` is
idempotent**: creating a queue that already exists with the *same* attributes
succeeds and returns the same URL, while creating it with *different* attributes
is a conflict. That single rule is what lets infrastructure-as-code run a
thousand times without a special "does it exist?" branch, and getting the
comparison right (which attributes participate? what about defaults the caller
did not send?) is the whole exercise.

**Attribute changes apply to messages already in the queue** — or don't, and you
must decide which for each one and say why. A visibility timeout lowered while a
message is in flight, a retention period shortened below the age of messages
already sitting there: both have a defensible answer and an indefensible one.

**Redrive** is where V1's receive count pays off: a message received more than
`maxReceiveCount` times is moved to the configured dead-letter queue rather than
delivered again. Note what SQS does *not* do here — there is no exponential
backoff, no retry schedule; the consumer expresses backoff by extending
visibility. Decide whether you agree with that design and write down why. Then
build the way **back**: a dead-letter queue you cannot inspect and redrive from
is a data-loss bug with a friendly name.

**Done when ALL true:**
- [ ] `CreateQueue` with identical attributes is **idempotent** (same URL, success); with conflicting attributes it is a **conflict error**.
- [ ] Every attribute is **validated and bounded** on write — visibility timeout, retention, delay, max message size, `maxReceiveCount` — and an invalid value is refused at `SetQueueAttributes` time, never at delivery time.
- [ ] For each attribute, whether it applies to **already-enqueued** messages is a deliberate, documented decision — and the implementation matches the document.
- [ ] A message received more than `maxReceiveCount` times lands in the **dead-letter queue** and stops being delivered.
- [ ] The DLQ is **inspectable and redrivable** — messages can be moved back to the source queue.
- [ ] Retention drops messages older than the configured period, and the queue's **oldest-message age** is observable.
- [ ] `DeleteQueue` makes the queue unusable immediately and in-flight messages do not resurrect it.
- [ ] Standard and FIFO attributes are enforced separately — a FIFO-only attribute on a standard queue is refused.

**Proof:** an idempotent-create test including the conflicting-attributes case; a
poison-message test showing exactly `maxReceiveCount` deliveries then a DLQ
landing; a redrive-back test; the per-attribute applies-to-existing table in
`docs/29-design.md`.

*Concept to internalize:* control plane vs data plane, idempotent creation as the
API property that makes declarative infrastructure possible, and the dead-letter
queue as the release valve that keeps one bad message from becoming an outage.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols / API

- [ ] The **AWS JSON protocol**: `X-Amz-Target: AmazonSQS.<Action>` with a JSON
  body, and AWS-shaped errors (`__type`, `x-amzn-errortype`) with the real error
  codes (`QueueDoesNotExist`, `ReceiptHandleIsInvalid`,
  `InvalidParameterValue`, `OverLimit`).
- [ ] **`boto3` / `aws-cli` work against it unmodified** — pointed at this
  endpoint with `--endpoint-url`, a real SDK can create a queue, send, receive,
  delete, and read attributes. This is the criterion that makes the whole project
  honest; a protocol only you can speak proves nothing.
- [ ] **Batch APIs**: `SendMessageBatch` / `DeleteMessageBatch` /
  `ChangeMessageVisibilityBatch` take up to 10 entries and return **partial
  results** — per-entry `Successful` and `Failed` lists, with a 200 status even
  when some entries failed. (Partial failure is the interesting case; a batch API
  that is all-or-nothing is not this API.)
- [ ] **Message attributes** (typed key/value metadata) round-trip intact,
  including binary values, and are counted against the size limit.
- [ ] Queue **URLs** are opaque, contain the account and queue name, and a URL
  from a deleted queue is refused rather than silently recreated.

### Security / abuse protection

- [ ] Every request is **SigV4-authenticated** — the verifier from project 25,
  used as a library or called over its authorization endpoint. An unsigned
  request reaches no queue.
- [ ] Every action is **authorized**, not just authenticated: sending to a queue
  and receiving from it are different permissions, checked per queue.
- [ ] Everything caller-controlled is **validated and capped**: message body size
  (256 KB), batch entry count (10), attribute count and size, queue name charset
  and length, `WaitTimeSeconds` ≤ 20, `VisibilityTimeout` ≤ 12h, `DelaySeconds`
  ≤ 15 min.
- [ ] A receipt handle is **unguessable and non-enumerable** — it leaks nothing
  about other messages, and a forged one is refused.
- [ ] **Per-queue quotas**: in-flight message limits and a request rate limit, so
  one caller cannot starve another or pin the process.
- [ ] Message bodies are **never logged**; queue names and ids are.

### Observability

- [ ] The three approximate gauges SQS publishes, and why they are *approximate*:
  `ApproximateNumberOfMessages`, `…NotVisible`, `…Delayed` — maintained in `O(1)`,
  never by counting.
- [ ] `ApproximateAgeOfOldestMessage` — **the** lag signal. If it climbs,
  consumers are behind or a group is stuck.
- [ ] Counters: sends, receives, deletes, empty receives, expired leases,
  dedup hits, DLQ arrivals, rejected receipt handles (split by stale vs
  malformed — they mean different things).
- [ ] Histograms: end-to-end latency (send → delete), long-poll wait duration,
  and messages-per-receive (a distribution stuck at 1 means batching is not
  working).
- [ ] A `tracing`-style span per request carrying queue name, action, and
  message count; the request id from `common_telemetry` on every line.

### Python & runtime

- [ ] `pyright --strict` clean, `ruff` clean.
- [ ] **No blocking call on the event loop** — verified with
  `PYTHONASYNCIODEBUG=1` under load, with no slow-callback warnings; hashing a
  256 KB body and firing 100K deadlines both stay off the critical path.
- [ ] Bounded everything, sized on purpose: waiter counts, in-flight per queue,
  dedup window, deadline queue — each with a documented limit and a defined
  behaviour at the limit (reject, not grow).
- [ ] **Graceful shutdown**: stop accepting new receives, release parked waiters
  with an empty response rather than a dropped connection, and let in-flight
  leases stand.
- [ ] A **profiling gate**: a `py-spy` flamegraph and a `memray` run under boss
  load, with the top three costs named in `docs/29-benchmarks.md` — and the
  memory-per-message number stated.

## Cross-cutting scale skills

- Protocol design against an untrusted client: every guarantee expressed as a
  token, a limit, or a check — never as an assumption about caller behaviour.
- Race analysis: for each of (expiry × delete), (expiry × visibility change),
  (send × park), a written argument for why exactly one outcome is possible.
- Timer scale: the difference between `O(n)`-per-tick and `O(due)`-per-tick,
  felt at a million messages.
- Fairness: many waiters, one message, and a defined answer to who gets it and
  how many wake up.
- The honest scope of "exactly-once", stated in a sentence you would defend to a
  user who is about to build a payment on it.

## Definition of done

The project is **done when ALL true:**

1. Every vertical + horizontal box above is checked, each with its **Proof** artifact.
2. **The boss falls** (below), with numbers in `docs/29-benchmarks.md`.
3. A `docs/29-design.md` covering: what a receipt handle contains and why it is
   unforgeable; the deadline structure you chose and the heap-vs-wheel numbers;
   the lost-wakeup argument for the long-poll path; the per-attribute
   applies-to-existing-messages table; and the exact scope of the dedup
   guarantee.
4. `make verify` is green (ruff, pyright strict, pytest) and no
   `raise NotImplementedError` remains on a checked path.

## 🐉 Boss fight — The Waiting Room

> Ten thousand consumers are connected and every one of them is waiting. Nothing
> is happening. This is the state your service spends most of its life in, and
> it is the one nobody load-tests. Then a producer wakes up and fires a burst
> into a hundred queues at once, and every decision you made about parking,
> waking and timers is settled in about four seconds: either the right handful of
> waiters return with work, or ten thousand of them wake up, find nothing, and
> park again — and you discover that your queue service's busiest moment is the
> one where it has nothing to do.

**Arena:** `bench/` load client against a `make run` server (uvicorn + uvloop,
`LOG_LEVEL=warn`), on one host, numbers and hardware recorded. Four scenarios:
**idle** (10K parked waiters, no traffic), **burst** (a send storm into a queue
with waiters parked), **drain** (sustained send → receive → delete), and **FIFO**
(1,000 groups, ≥8 receivers).

**The boss falls when ALL true:**
- [ ] **Idle:** 10,000 parked long-poll waiters across 100 queues hold **< 3% CPU**
  and **< 400 MB RSS**, sustained for 5 minutes.
- [ ] **Burst:** with N waiters parked, one message wakes **≤ 2** of them, and the
  p99 send→waiter-return latency is **≤ 25 ms**.
- [ ] **Drain:** **≥ 10,000 messages/sec** end-to-end (send → receive → delete)
  sustained for 60s using 10-entry batches, at **p99 ≤ 50 ms**.
- [ ] **Timers:** with **1,000,000** messages scheduled (delayed or in flight),
  idle CPU is within **2×** of the same measurement at 10,000 — the curve is flat,
  not linear.
- [ ] **FIFO:** 1,000 groups × 100 messages with 8 concurrent receivers completes
  with **zero out-of-order deliveries** and **≥ 5×** the throughput of a
  single-group run.
- [ ] **Correctness under load:** across the whole drain run, **zero** messages
  delivered twice while their lease was live, and **zero** messages lost.
- [ ] **No slow-callback warnings** under `PYTHONASYNCIODEBUG=1` during the drain
  run.

> Where CPython cannot reach one of these, **that gap is the finding**: name the
> number you got, the cause (GIL, GC, allocation, a blocking call, per-message
> object overhead), and what a different runtime would change. Do not lower the
> bar — record the distance to it.

**Proof:** methodology, hardware, and before/after numbers in
`docs/29-benchmarks.md`, reproducible via `bench/`.

## Suggested order of attack

1. Get the boring path working: `CreateQueue`, `SendMessage`, `ReceiveMessage`
   with `WaitTimeSeconds=0`, `DeleteMessage` — no leases, no timers.
2. Make the delete safe (V1): receipt handles, and the stale-delete test that
   proves it. Write that test first; it is the one that shows you the bug.
3. Add the deadline engine (V2) and hang visibility expiry off it. Now a crashed
   consumer's message comes back.
4. Turn on long polling (V3). Measure idle CPU at 10K waiters before you tune
   anything — the first number is usually a surprise.
5. Add FIFO (V4), then dedup (V5) on top of it.
6. Fill in the control plane and redrive (V6).
7. Put SigV4 (project 25) in front, add the caps and the gauges, then point
   `boto3` at it and fix everything that breaks.
8. Benchmark, profile, document.

## Run it

```bash
uv sync                     # from the repo root — one lockfile for the workspace
cp .env.example .env
make run                    # or: uv run sqs-queue

# The scaffold serves /healthz and /metrics. Every action raises
# NotImplementedError — that panic is the worklist.
curl -sS localhost:9029/healthz

curl -sS -XPOST localhost:9029/ \
  -H 'x-amz-target: AmazonSQS.CreateQueue' \
  -H 'content-type: application/x-amz-json-1.0' \
  -d '{"QueueName":"orders"}'

# Once the protocol horizontal is done, this is the real bar:
aws --endpoint-url http://localhost:9029 sqs create-queue --queue-name orders
```
