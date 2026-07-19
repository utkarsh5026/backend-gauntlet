# Hot-Path Fundamentals — gRPC, Deadlines, p99, and Not Leaking Keys

> **What this teaches:** the backend fundamentals woven through this project's
> [horizontal checklist](../SPEC.md) — why this service speaks gRPC, what a
> deadline really is, why a limiter is judged at p99, how to key limits so the
> right attacker gets caught, and why your logs must never see a raw API key.
> No prior gRPC or observability knowledge assumed. Covers the ⚡ rapid-fire
> round of [CONCEPTS.md](../CONCEPTS.md).
>
> **Anchored to:** [ratelimit.proto](../proto/ratelimit.proto),
> [error.rs](../src/error.rs), [main.rs](../src/main.rs).

---

## The one sentence to hold onto

> This service sits **inside every request** on the platform — so its latency is
> a tax on everything, its failures need precise names, and every fundamental
> here exists to keep that tax tiny and that behavior predictable.

---

## 1. Why gRPC here (and what a `.proto` buys you)

Project 01 spoke HTTP/JSON because browsers were the caller. This service's
callers are **other backend services** — project 01's `POST /api/links` checking
a client's budget before creating a link — and they call it on *every request*.
That changes the calculus:

| Concern | HTTP/1.1 + JSON | gRPC (HTTP/2 + protobuf) |
|---|---|---|
| Encoding | Text; parse `"allowed": true` every call | Binary; fields decoded by position, near-free |
| Contract | Prose docs, drift-prone | [ratelimit.proto](../proto/ratelimit.proto) — compiler-enforced on *both* sides |
| Connections | One in-flight request per connection (without pipelining pain) | **Multiplexed** — thousands of concurrent calls share one TCP connection |
| Deadlines | Roll your own with headers | **Built into the protocol** (§2) |

The `.proto` file is the API. `tonic` (the Rust gRPC library) generates the
server trait and message structs from it at compile time — that's the
`tonic::include_proto!("ratelimit.v1")` in [main.rs](../src/main.rs), fed by
[build.rs](../build.rs). If your `CheckResponse` doesn't match the contract, it
doesn't compile. Cross-language, too: a Go gateway generates its *client* from
the same file.

The contract also encodes semantics worth noticing: `Check` **consumes** budget,
`Peek` **must not** — dashboards polling `Peek` shouldn't rate-limit you. And
`cost=0` is treated as 1 ([main.rs](../src/main.rs) does this mapping) so a
zero-cost check can't become a free infinite probe.

You can poke a running gRPC server by hand, like `curl` for HTTP:

```bash
grpcurl -plaintext -d '{"key":"user-1","cost":1}' localhost:50051 ratelimit.v1.RateLimiter/Check
```

(The stretch item "server reflection" is what lets `grpcurl` discover services
without being handed the `.proto` file.)

---

## 2. Deadlines: fail fast, don't queue

A gRPC client can attach a **deadline** to any call: "I need an answer within
50 ms, or don't bother." The killer feature: the deadline **propagates** — the
server sees it and can check *"can I still make it?"* at each step.

Why is honoring it a checklist item and not politeness? Because the alternative
is the classic overload death spiral:

```
  without deadlines:                     with deadlines honored:

  caller times out at 50ms,             caller times out at 50ms
  BUT the server keeps working ──┐      server sees deadline exceeded
  on the abandoned request       │      → drops the work immediately
                                 ▼
  queue grows with zombie work         queue holds only live work
  → every later request slower         → latency stays bounded
  → more timeouts → more zombies       → overload sheds instead of
  → collapse                             compounding
```

Work done after the caller stopped listening is **pure waste that competes with
live requests** — that's *tail amplification*: one slow moment breeds queued
zombies, which breed more slow moments. A `Check` that can't beat its deadline
should return `DEADLINE_EXCEEDED` *immediately*, not join a queue. And notice
the caller's side of the contract: a gateway waiting unboundedly on its rate
limiter has made the limiter a platform-wide single point of slowness — the
fail-open/closed policy of [doc 02](02-redis-lua-atomic-decisions.md) is what
the gateway falls back to when the deadline fires.

---

## 3. Status codes: failures the caller can *act* on

gRPC has a fixed vocabulary of status codes, and the checklist asks you to map
errors precisely because each code implies a different **caller reaction**:

| Code | Means | Sane caller reaction |
|---|---|---|
| `OK` (+ `allowed=false`) | The limiter *worked*; the answer is "denied" | Honor `retry_after_ms` |
| `INVALID_ARGUMENT` | Your request is malformed (empty key, absurd cost) | Fix the bug; **never retry** — it can't succeed |
| `UNAVAILABLE` | Backend (Redis) trouble; transient | Retry with backoff, or apply local fallback policy |
| `DEADLINE_EXCEEDED` | Ran out of time | Fall back (fail open/closed *at the caller*) |
| `INTERNAL` | Server bug; details deliberately withheld | Alert a human |

Two of these are easy to conflate and must not be: **a deny is not an error.**
A denied `Check` is the service *succeeding* — it returns `OK` with
`allowed=false`. If you returned an error status for denies, callers couldn't
distinguish "you're over budget" from "the limiter is broken," and their retry
logic would do exactly the wrong thing in both cases.

The scaffold already wires the mapping: [error.rs](../src/error.rs) sends
`InvalidArgument` → `INVALID_ARGUMENT` with the real message, but `Backend` →
`UNAVAILABLE` with a *generic* message — full details go to the server log only.
That asymmetry is deliberate: argument errors are the caller's to fix (tell them
everything); backend errors leak infrastructure details to whoever's calling
(tell them nothing useful).

---

## 4. Health checks: a standard way to say "I'm alive"

Kubernetes and load balancers constantly ask "should I send traffic here?" For
HTTP they probe `/healthz`; gRPC standardizes the equivalent as a tiny service —
`grpc.health.v1.Health` — that your server hosts *alongside* `RateLimiter`.
LBs, k8s probes, and `grpcurl` all speak it natively; implementing it means your
service can be drained, rolled, and load-balanced by standard tooling with zero
custom glue.

The interesting decision is what "healthy" *means* for this service: is a
limiter with Redis down healthy? If you fail open, arguably yes — it still
answers. If you fail closed, reporting healthy means k8s happily routes traffic
into a wall of denials. Your health answer and your failure policy
([doc 02 §6](02-redis-lua-atomic-decisions.md)) have to tell one consistent story.

---

## 5. Why p99, not average

The rapid-fire card asks: *why is a limiter judged at p99?* Two compounding
reasons.

**It taxes everything.** Every request to every service pays the limiter's
latency before doing its own work. A limiter averaging 1 ms doesn't add 1 ms to
one endpoint — it adds ~1 ms to *the entire platform's floor*.

**Fan-out multiplies the tail.** p99 = the latency your slowest 1% see. Feels
ignorable — but a page load that fires 15 API calls rolls the dice 15 times.
The chance at least one call eats a p99-or-worse limiter decision is
1 − 0.99¹⁵ ≈ **14%** (verified). One request in a hundred being slow becomes
*one page in seven*. Users experience your tail, not your average — and every
hop that fans out amplifies it. This is why the SPEC's Definition of done wants
a **histogram** of decision latency and bench numbers reporting p50/p99, not a
mean: an average happily hides a catastrophic 1%.

(Same logic drives the observability checklist: a `tracing` span per `Check`
with decision + backend latency, counters for allowed/denied/Redis errors/script
cache hits — the four numbers you'll actually want mid-incident.)

---

## 6. Keying: per-identity *and* per-IP are different defenses

The checklist says limit on identity (API key / user) **and separately on IP**.
Not redundancy — they catch **different attackers**:

| Attacker | Per-identity limit | Per-IP limit |
|---|---|---|
| One noisy tenant hammering with their own key | ✅ caught | ❌ they may rotate IPs |
| Credential-stuffing botnet: 10,000 IPs, each trying many *different* accounts | ❌ each identity sees a trickle | ✅ each IP's volume shows |
| Shared-NAT office: 200 legit users behind one IP | ✅ each user judged fairly | ⚠️ must not lump them into one bucket |

The shared-NAT row is the subtle one: if IP were your *only* key, one office's
200 employees share one bucket and starve each other — that's why identity is
primary. But identity alone is blind to the botnet spreading load across
thousands of stolen identities from few machines — that's what the IP dimension
sees. Two dimensions, two buckets per request, both must pass. The mechanism
needs nothing new: `key` is just a string — callers send `apikey:abc123` and
`ip:203.0.113.7` as two `Check` calls (or you namespace however you choose; the
limiter is policy-free by design).

And the validation half of the checklist: empty key or absurd `cost` gets
`INVALID_ARGUMENT` (the scaffold's `check()` already rejects empty keys). A
garbage key silently accepted is a bucket nobody intended, billed to no one.

---

## 7. Log hygiene: a key in a log is a leaked key

The `key` this service handles is often literally an **API credential**. Logs
are the leakiest place in an infrastructure: shipped to third-party aggregators,
retained for months, readable by half the org, attached to bug reports. Writing
`key=sk_live_abc123` into a `tracing` span puts a production credential in all
of those places at once — your limiter's logs become a credential dump.

The checklist's fix: log a **hash or truncation** of the key, never the raw
value. A hash keeps the property you actually need in logs — *"these 500 denials
were the same client"* (same input → same hash) — while being useless to steal.
Truncation (`sk_live_a…`) trades a little collision-blindness for a little
human-debuggability. Either is fine; raw is never. Same discipline as this
repo's CLAUDE.md rule ("never log secrets"), applied where it's most tempting to
violate — the one field every span in this service naturally wants to record.

---

## 8. Mental model summary

| Concept | One-liner |
|---|---|
| gRPC + proto | Binary, multiplexed, compile-time contract — for service-to-service hot paths |
| `Check` vs `Peek` | Consumes vs observes; observation must never cost budget |
| Deadlines | Work after the caller gave up is waste that attacks your live traffic — fail fast |
| Status codes | Each code implies a caller reaction; a **deny is `OK`**, not an error |
| `INVALID_ARGUMENT` vs `UNAVAILABLE` | "You can't retry this" vs "you may retry this" — don't blur them |
| Health checks | `grpc.health.v1` so standard tooling can probe you; keep it consistent with your fail policy |
| p99 | Fan-out turns a 1% tail into ~14% of 15-call pages; averages hide the 1% |
| Dual keying | Identity catches the noisy tenant; IP catches the distributed botnet; NAT explains why identity is primary |
| Log hygiene | Hash/truncate keys — correlation without credential leakage |

## Where you'll apply this

No single `todo!()` — these are the [horizontal checklist](../SPEC.md) items,
woven in as you build V1→V3: deadline handling and health service around
[main.rs](../src/main.rs)'s server setup, status-code discipline through
[error.rs](../src/error.rs), hashed keys + decision fields in your `tracing`
spans, and the p50/p99 story in `docs/02-benchmarks.md` when you run
`ghz` against `Check` for the Definition of done.
