<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 24 — Lambda Compute

> "Run this function when something happens" is a one-line pitch hiding a
> multi-tenant operating system. The hard parts are the ones you feel in
> production: the **first** request pays for a whole environment to be built while
> the next thousand reuse one, so your p99 is a function of your traffic *shape*
> rather than your code. Your handler is **frozen** the instant it returns, so the
> background task you fired off finishes minutes later inside someone else's
> request — or never. Concurrency, not CPU, is the unit of capacity, and a burst
> that exceeds it gets a throttle rather than a queue. And the function is *not
> your code calling the platform* — it's the platform handing work to a runtime
> that asked for it, which is why a custom runtime is 200 lines of HTTP.
>
> This project builds that compute plane: the Runtime API, the execution
> environment lifecycle, the sandbox, the concurrency governor, the async
> invocation queue, and the pollers that turn a stream into invocations.

**Explicitly out of scope: the control plane's durability and the network.** No
multi-node placement, no VPC attachment, no image registry, no IAM (that's project
**25** — point it here when you get there). One node, one host, and every hard
problem is still on the table. The event source you poll in V6 is project **23**'s
stream; the router that fans events at you is project **27**.

## What it does (the easy part)
- Register a function: a handler, a runtime, a memory size, a timeout, an env.
- `POST /2015-03-31/functions/{name}/invocations` — **synchronous** invoke, the
  caller waits and gets the handler's return value or its error.
- The same call with `X-Amz-Invocation-Type: Event` — **asynchronous** invoke,
  acknowledged immediately, retried on failure, dead-lettered when it runs out.
- The **Runtime API** on its own listener: `GET /2018-06-01/runtime/invocation/next`
  long-polls for work; `POST .../{id}/response` and `.../{id}/error` return it.
- Execution environments that are **created, reused, frozen, thawed and reaped**,
  with the cold/warm split visible in every response.
- Concurrency limits — account-wide, reserved per function, and provisioned — with
  throttles when a burst outruns them.
- Event source mappings that poll a stream, batch, checkpoint, and report partial
  batch failures.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. The Runtime API — *the sandbox asks for work; the service never pushes*
Everyone assumes Lambda *calls* your function. It does not. Your runtime starts, and
then **it** long-polls the platform asking for an invocation; the platform hands one
back and waits for a `POST` with the result. That inversion is the entire reason a
custom runtime is a shell script with `curl` in a loop, why the init phase can be
billed separately, and why a handler that never posts a response looks identical to
one that hung. Build the invocation broker and the runtime-facing HTTP surface in
`src/lambda_compute/runtime_api.py`.

**Done when ALL true:**
- [ ] A runtime that long-polls `/next` receives exactly one invocation per poll, and the response carries the request id, the deadline, and the invoked function's ARN as headers.
- [ ] A caller's synchronous invoke blocks until the runtime posts a `/response`, and receives that payload **unchanged** — byte for byte, not re-serialized.
- [ ] A runtime posting `/error` produces a **function error** distinguishable by the caller from a **platform error**, with the handler's error type and stack trace preserved.
- [ ] `/next` **blocks** when there is no work rather than returning empty or 404, and a runtime polling for minutes holds one connection and consumes no CPU.
- [ ] An invocation whose runtime never responds hits the **deadline** and the caller gets a distinct timeout error — the environment is not left holding the request forever.
- [ ] Two environments polling `/next` for the same function never receive the same invocation, and no invocation is dropped when one of them dies mid-poll.
- [ ] Posting a response for an unknown or already-completed request id is **rejected**, not silently accepted.

**Proof:** an integration test driving a real runtime process through the full
poll → invoke → respond loop; a test asserting two pollers split N invocations with
no duplicate and no loss; a timeout test; `docs/24-design.md` records how a pending
invocation is handed to exactly one poller.

*Concept to internalize:* inversion of control as an isolation boundary — the
sandbox needs no inbound port, no credentials to the control plane, and no
knowledge of the caller.

### V2. Execution environments — *the cold start is a lifecycle, not a latency bug*
An execution environment has phases the billing and the bug reports both follow:
**init** (once — imports, global state, connection pools), **invoke** (many), and
**freeze** the microsecond your handler returns. Frozen means the clock stops mid-
`await`: the background task you didn't await resumes, if ever, inside a *later*
request. Reuse is why your warm p50 is 2ms and your cold p99 is 800ms, and why
module-level state is both the best optimization available and the source of the
weirdest bugs you will ever debug. Build the environment lifecycle and the warm pool
in `src/lambda_compute/environments.py`.

**Done when ALL true:**
- [ ] A first invocation creates an environment and reports **cold**; the next reports **warm** and skips init entirely — both observable per invocation, not inferred from timing.
- [ ] Init runs **exactly once** per environment; module-level state set during init is visible to every subsequent invocation on that environment.
- [ ] An environment serves **exactly one invocation at a time** — concurrent invocations get separate environments, and a test proves no two overlap on one.
- [ ] Work left running when a handler returns is **frozen**: it makes no progress between invocations, and either resumes on the next one or is documented as discarded — whichever you chose, the test asserts it.
- [ ] An **init failure** is reported as such (distinct from an invoke failure), the environment is discarded rather than reused, and the next invocation gets a fresh one.
- [ ] Idle environments are **reaped** after a documented TTL, and the reap is observable — an idle fleet shrinks rather than pinning memory forever.
- [ ] An environment is retired after a failed invocation *only* when the failure could have corrupted it; a plain handler exception reuses the environment (and a test proves the reused one is clean).

**Proof:** tests for cold/warm reporting, once-only init, no-concurrent-reuse, the
freeze semantics you chose, and reaping on a shortened TTL; `docs/24-design.md`
records the freeze model and the reuse-vs-retire policy.

*Concept to internalize:* why "keep it warm" is a scheduling decision rather than a
trick, and why the freeze boundary makes background work an anti-pattern.

### V3. The sandbox — *a tenant you assume is hostile*
The function is arbitrary code from someone you do not trust, and it shares a kernel
with the platform that supervises it. That means a real process boundary, a memory
ceiling that kills rather than swaps, a CPU share proportional to that memory, a
writable `/tmp` that does not survive into another tenant, and a timeout that is
enforced *from outside* — because a handler spinning in a tight loop will not
cooperate with a timer inside itself. Build the isolation boundary and the resource
governor in `src/lambda_compute/sandbox.py`.

**Done when ALL true:**
- [ ] The handler runs in a **separate process**, and a handler that segfaults or calls `os._exit` fails that one invocation without taking down the node.
- [ ] Exceeding the configured **memory limit** kills the environment with a distinct out-of-memory error naming the limit — it does not swap, slow down, or take the node with it.
- [ ] A handler that **ignores** its timeout (a tight non-yielding loop) is still killed at the deadline, and the caller gets the timeout error.
- [ ] `/tmp` is writable, is **shared across invocations on one environment**, and is provably **not** visible to a different environment.
- [ ] The sandbox cannot reach the **control plane**: it can talk to the Runtime API and nothing else on the node, proven by a test that tries.
- [ ] Environment variables and the invocation payload are the **only** channels into the sandbox — the parent's environment, cwd, and open file descriptors do not leak in.
- [ ] Killing an environment reclaims **all** of its resources: no orphan processes, no leaked fds, no growing `/tmp` — asserted after a few hundred create/destroy cycles.

**Proof:** tests for the OOM kill, the uncooperative-timeout kill, `/tmp` isolation
between environments, and the control-plane reachability check; a leak test looping
create/destroy while asserting process and fd counts are flat; `docs/24-design.md`
records the isolation primitives you chose and, honestly, what they do **not**
protect against.

*Concept to internalize:* defence in depth for multi-tenant compute, and why the
real service moved from containers to microVMs — read what that bought and record
which of it your boundary does and does not have.

### V4. Concurrency & scaling — *concurrency is the unit of capacity*
Lambda does not scale on CPU. It scales on **concurrent executions**, and every
limit that pages you is denominated in them: an account ceiling shared by every
function, a **reserved** slice that both guarantees and caps one function, and
**provisioned** environments kept warm at a price. The interesting part is the
failure mode: a burst that outruns the scale-up rate is **throttled**, not queued,
and one runaway function can starve every other function in the account unless you
reserved for them. Build the concurrency governor and the scaling policy in
`src/lambda_compute/concurrency.py`.

**Done when ALL true:**
- [ ] Concurrent invocations beyond the limit are **throttled** with a distinct, retryable error — never queued behind, never silently delayed.
- [ ] **Reserved concurrency** does both jobs: a function with a reservation can always reach it, and can never exceed it, both proven while another function is saturating the account.
- [ ] Without reservations, one function consuming the account limit **starves** the others — reproduced deliberately — and adding a reservation fixes it, measured.
- [ ] **Provisioned concurrency** serves its invocations with **no cold start** until the provisioned count is exceeded, at which point the spillover is cold and reported as such.
- [ ] A burst beyond the instantaneous limit is admitted at a **documented scale-up rate**, and the accepted-vs-throttled split over time matches that documented policy.
- [ ] Concurrency accounting is **exact under load**: after any burst, in-flight count returns to zero, and no slot is leaked by a timeout, a crash, or a client disconnect.
- [ ] Throttled invocations are counted separately from failures in the metrics — a throttle is a capacity signal, not an error rate.

**Proof:** a concurrency test driving real parallel invocations (not sequential
calls) across two functions with and without reservations; a slot-accounting test
that crashes and times out invocations and asserts the counter returns to zero;
`docs/24-design.md` records the limits, the scale-up policy, and the throttle
algorithm.

*Concept to internalize:* why concurrency (not RPS) is the capacity unit, and how
`concurrency = rps × duration` turns a latency regression into a throttling incident.

### V5. Asynchronous invocation — *the queue nobody sees until it retries twice*
An async invoke is acknowledged in milliseconds and then owned entirely by the
platform: it is queued, executed later, **retried twice on failure with backoff**,
and finally dropped onto a dead-letter target. Every one of those is a delivery
semantic your callers depend on without ever having read it: at-least-once means the
handler *will* run twice one day, so idempotency is their problem — but making the
retry and the DLQ observable is yours. Build the invocation queue, the retry policy
and the failure destinations in `src/lambda_compute/async_invoke.py`.

**Done when ALL true:**
- [ ] An `Event`-type invoke returns **202 immediately** with a request id, before the handler has run, and the handler demonstrably runs afterwards.
- [ ] A failing async invocation is retried on a documented **backoff** schedule up to a documented **max attempts**, and every attempt carries the **same** request id.
- [ ] After the final attempt the event lands on a **dead-letter target** with the original payload, the error, and the attempt count intact — nothing is silently dropped.
- [ ] The queue is **bounded**: a producer faster than the consumer is either throttled or sheds explicitly, and memory stays flat — proven by a test, not by hope.
- [ ] The **event age** is observable and a maximum age is enforced: an event that has waited too long is discarded to the DLQ rather than executed against a stale world.
- [ ] Async invocations respect the same **concurrency limits** as sync ones and are throttled into a retry rather than executed over the limit.
- [ ] In-flight and queued events **survive a graceful shutdown** — a SIGTERM does not lose an acknowledged event.

**Proof:** tests for the 202-before-execution ordering, the retry schedule with a
stable request id, DLQ contents after exhaustion, bounded-queue backpressure, and
max-age expiry; a shutdown test asserting no acknowledged event is lost;
`docs/24-design.md` records the delivery guarantee you provide and what it costs.

*Concept to internalize:* at-least-once delivery as a contract, and why "we'll just
retry" is a design decision with an idempotency bill attached.

### V6. Event source mapping — *the poller that turns a stream into invocations*
For a stream, nothing calls Lambda — Lambda calls **you**. A managed poller reads a
shard, accumulates a **batch** (by size or by time window, whichever fills first),
invokes the function once with the whole batch, and only then **checkpoints**. The
subtleties are where the data loss lives: one poor error-handling choice turns a
single bad record into an infinitely retried batch that blocks its shard forever
(the "poison pill"), and one careless checkpoint turns a partial failure into
silently skipped records. Build the poller, the batching policy and the checkpoint
protocol in `src/lambda_compute/event_source.py`. The source is project **23**'s
stream.

**Done when ALL true:**
- [ ] The poller batches by **both** `batch_size` and a **batch window**, invoking on whichever fills first — a low-rate stream is not stalled waiting for a full batch.
- [ ] A checkpoint advances **only** after the batch is successfully processed; killing the poller mid-batch **replays** that batch rather than skipping it.
- [ ] Records for one partition key are delivered **in order**, and the shard does not advance past an unprocessed record.
- [ ] **Partial batch failure** works: a handler reporting item-level failures causes only those records (and everything after them in the shard, per the documented policy) to be retried — successfully processed records are not re-delivered.
- [ ] A **poison-pill record** does not block its shard forever: a documented policy (bisect, max retries, or on-failure destination) evicts it, and the shard makes progress — reproduced in a test.
- [ ] Per-shard **concurrency** is respected: one invocation in flight per shard by default, and raising the parallelisation factor keeps per-key ordering intact.
- [ ] **Iterator age** is a metric, and a consumer falling behind is visible as a growing age before it becomes data loss.
- [ ] A mapping can be **disabled and re-enabled** and resumes from its checkpoint, not from the horizon.

**Proof:** tests for the window-vs-size race, replay-on-crash, ordering under
concurrent producers, partial batch failure, and poison-pill eviction; an
integration test against a real project-23 stream if you have it running;
`docs/24-benchmarks.md` records iterator age under sustained load;
`docs/24-design.md` records the checkpoint protocol and the poison-pill policy.

*Concept to internalize:* checkpointing as the boundary between at-least-once and
data loss, and why the batch is the unit of both efficiency and failure.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] The invoke API mirrors the **real Lambda shape** — `POST /2015-03-31/functions/{name}/invocations`, the invocation type chosen by `X-Amz-Invocation-Type`, `X-Amz-Function-Error` on a handler error — so the mental model transfers to the real service.
- [ ] The Runtime API mirrors the **real `/2018-06-01/runtime/` contract**, closely enough that a runtime written against the real one works here unmodified.
- [ ] Errors use Lambda's **named exception types** (`TooManyRequestsException`, `ResourceNotFoundException`, `InvalidRequestContentException`, `RequestTooLargeException`) with correct status codes and a documented retryable/non-retryable split.
- [ ] The **payload size cap** is enforced before execution, with a distinct error and different limits for the sync and async paths.
- [ ] A **client disconnect** mid-invocation is detected: the concurrency slot is released and the outcome (kill or complete) is documented.

### Isolation & security
- [ ] Function code and env vars are treated as **untrusted input** end to end — nothing from a function reaches a log line, an error body, or a metric label unescaped or unbounded.
- [ ] Per-function **env var secrets never appear** in logs, error responses, `/metrics`, or the process table of another tenant.
- [ ] The blast radius of a single hostile function is **stated and tested**: what it can exhaust, what it can observe, and what stops it.
- [ ] Invocations are **authenticated**, and the Runtime API accepts connections **only** from the environment it belongs to — a second environment cannot poll another's queue.

### Delivery & durability
- [ ] The async queue and every event source mapping checkpoint **survive a restart** — no acknowledged event is lost and no checkpoint moves backwards.
- [ ] **Graceful shutdown** drains in-flight invocations up to a deadline, stops accepting new ones, and reports what it abandoned.
- [ ] Delivery guarantees are **written down per path** (sync, async, event source) — at-most-once, at-least-once, or exactly-once — and each is backed by a test.

### Observability
- [ ] A span per invocation carrying the request id, function name, **cold/warm**, init duration, execution duration and billed duration — the same fields the real `REPORT` line has.
- [ ] Metrics at `/metrics`: **invocations, errors, throttles, duration histogram, concurrent executions, cold-start count and ratio, async queue depth, iterator age, DLQ count.**
- [ ] The **cold-start ratio is visible per function**, so a deploy that wrecks init time shows up as a graph rather than a support ticket.
- [ ] Function `stdout`/`stderr` are captured, **attributed to the right request id**, and bounded — a chatty handler cannot exhaust the node's disk or memory.

### Python & runtime
- [ ] **`pyright` strict passes clean** — every `# type: ignore` carries a comment justifying it.
- [ ] **No blocking call on the event loop:** runs clean under `PYTHONASYNCIODEBUG=1`; process spawning, waiting and `/tmp` I/O are moved off the loop *deliberately*, with the reason recorded.
- [ ] **Bounded pools and buffers sized on purpose:** the warm pool, the async queue, the per-invocation log buffer and the batch buffers all have explicit limits, tuned together with the expected invocation rate.
- [ ] **Graceful shutdown** drains in-flight invocations and stops the pollers on SIGTERM — no acknowledged invocation is lost on restart.
- [ ] **The GIL's cost is measured, not assumed:** the benchmark states whether invocation throughput scales with concurrency, and if not, why — and whether moving the supervisor's hot path off the loop changed it.
- [ ] **Process spawn cost is measured:** the benchmark separates the platform's environment-creation cost from the function's own init, because only one of those is yours to fix.

---

## Cross-cutting scale skills (every project carries these)
- **Backpressure & bounds:** the concurrency limit *is* the backpressure; the async
  queue, warm pool and log buffers are bounded, and a slow consumer or a chatty
  function cannot grow memory without limit.
- **Graceful shutdown:** stop the pollers, drain in-flight invocations, checkpoint,
  and report what was abandoned.
- **Benchmarks with numbers:** `bench/` + `docs/24-benchmarks.md` — warm and cold
  latency distributions, throughput by concurrency, and the cost of an environment.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load test lives in `bench/`, the
   numbers in `docs/24-benchmarks.md`.
3. `docs/24-design.md` records the five decisions the SPEC grades: **the invocation
   hand-off protocol, the environment freeze & reuse policy, the isolation
   primitives (and their honest limits), the concurrency & scale-up policy, and the
   checkpoint / retry / DLQ semantics per invocation path.**
4. `make verify` is green — `ruff` clean, `pyright` **strict** with zero errors, and
   `pytest` passing; no `NotImplementedError` remains on a checked path.
5. A **profile** is committed: a `py-spy` flamegraph and a `memray` run in
   `docs/24-benchmarks.md`, naming the top bottleneck and stating how much of the
   warm-path overhead is the supervisor's rather than the function's.

## 🐉 Boss fight — The Cold Front

> The feature goes live at 9am and the traffic graph is a wall. There are no warm
> environments, because at 8:59 there was no traffic — so every one of the first
> thousand requests pays to build a world before it runs a line of your code. Your
> p99 is measured in seconds. Your concurrency limit arrives a moment later and
> starts throttling the requests that *would* have been warm. The dashboard says
> the CPU is bored. The front rolls through, the fleet warms, latency collapses to
> nothing, and the postmortem asks the only question that matters: **how fast can
> you go from zero to warm, and what happens to everyone who arrives first?**

**Arena:** `bench/` load generator against `make run`, over four phases against a
fixed account concurrency limit: **(1)** cold — 0 → 1,000 concurrent invocations of
a function with a deliberately non-trivial init, **(2)** warm — sustained load once
the fleet has settled, **(3)** noisy neighbour — a second, runaway function fighting
for the same account limit, with and without a reservation, and **(4)** stream —
an event source mapping draining a backlog from a project-23 stream.

**The boss falls when ALL true:**
- [ ] ≥ **2,000 warm invocations/sec** sustained for 60s, with the platform's own overhead **p99 ≤ 10ms** on top of the handler's measured runtime.
- [ ] **Cold-start p99 ≤ 250ms** for the platform's share of it — environment creation and hand-off, excluding the function's own init, which is measured and reported separately.
- [ ] The 0 → 1,000 burst loses **nothing**: every invocation either completes or returns an explicit throttle, and the accepted-vs-throttled split over time matches the documented scale-up rate within **±10%**.
- [ ] At steady state the **environment reuse ratio is ≥ 95%**, and cold-start count returns to ~0 while load is sustained.
- [ ] **Concurrency accounting is exact:** after every phase, in-flight returns to **0** and total accepted + throttled + failed equals total offered — no leaked slots, no lost invocations.
- [ ] The noisy neighbour **starves** the victim function without a reservation, and with one the victim holds **≥ 95%** of its reserved concurrency throughout — both measured in the same run.
- [ ] **Zero cross-invocation leakage** under the whole burst: no invocation observes another's `/tmp`, env, globals, or payload — asserted by the functions themselves, not by inspection.
- [ ] The stream phase drains the backlog with **iterator age ≤ 1s** at steady state, and a kill -9 mid-batch **replays without loss** and without duplicating a checkpoint.
- [ ] Memory is **flat** across the whole run: after the fleet is reaped, RSS returns to within **10%** of its pre-burst baseline — no environment, process, or fd leaked.

**Proof:** methodology + numbers in `docs/24-benchmarks.md` (hardware noted, commands
reproducible via `bench/`), with the cold and warm latency distributions plotted
separately — an average across both is the exact lie this boss exists to teach you
about. Where CPython cannot reach a target, the **gap and its cause** — GIL
contention, process spawn cost, GC pauses, allocation, or a blocking call on the
loop — is the finding, and it is written down rather than rounded away.

## Suggested order of attack
1. One function, one environment, synchronous invoke, no sandbox — get the **Runtime API** loop closing end to end against a runtime you write in the same process (V1).
2. Move the runtime into a **real subprocess** and make the loop survive it; add init/invoke phases and the cold/warm report (V1 → V2).
3. Add the **warm pool**: reuse, freeze semantics, reaping, and the one-invocation-at-a-time rule (V2).
4. Add **timeouts and memory limits** enforced from outside, then `/tmp` and the reachability boundary (V3).
5. Add the **concurrency governor** — account limit first, then reserved, then provisioned — and reproduce a noisy neighbour on purpose (V4).
6. Add the **async path**: 202, queue, retries, DLQ, max age (V5).
7. Add the **event source mapping** against project 23's stream: batching, checkpointing, partial failures, poison pills (V6).
8. Add auth, per-request log attribution, the metrics above; benchmark, document, tune.

## Run it
```bash
make setup && make sync    # .env from .env.example, then the venv
make run                   # control plane on :9001, Runtime API on :9002

# register a function, then invoke it synchronously
curl -XPOST localhost:9001/2015-03-31/functions \
  -H 'content-type: application/json' \
  -d '{"FunctionName":"hello","Handler":"examples.hello.handler","MemorySize":128,"Timeout":3}'

curl -XPOST localhost:9001/2015-03-31/functions/hello/invocations \
  -H 'content-type: application/json' -d '{"name":"world"}'

# the same call, asynchronously — 202 immediately, runs later
curl -i -XPOST localhost:9001/2015-03-31/functions/hello/invocations \
  -H 'X-Amz-Invocation-Type: Event' -d '{"name":"world"}'
```
