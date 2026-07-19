# Distributed Atomicity — Redis + Lua, From First Principles

> **What this teaches:** why a limiter that's perfectly correct on one machine
> silently over-admits the moment you run two, what TOCTOU races are, and how
> pushing the whole decision *into* the store as one Lua script eliminates the
> race without a single lock. No prior Redis or Lua knowledge assumed.
>
> **Prepares you for:** [SPEC V3](../SPEC.md) — the `todo!()`s and the empty
> `BUCKET_LUA` in [redis_limiter.rs](../src/redis_limiter.rs).
> Concept card: [CONCEPTS.md · Card 3](../CONCEPTS.md).

---

## The one sentence to hold onto

> When the race is *between processes*, no care inside any one process can fix
> it — the read-modify-write must become **one indivisible operation at the
> data's home**, and a Lua script inside single-threaded Redis is exactly that.

---

## 1. The problem arrives in two stages

### Stage 1: N instances, N× the limit

Your V1 bucket lives in process memory. Run **three** gateway instances behind a
load balancer, each with its own in-memory bucket for `user-1`, and each happily
enforces the full limit independently:

```
                        ┌────────────┐   bucket("user-1"): 20 tokens
                   ┌──► │ instance A │
  user-1 ──► LB ───┼──► │ instance B │   bucket("user-1"): 20 tokens
                   └──► │ instance C │
                        └────────────┘   bucket("user-1"): 20 tokens

              configured limit: 20        actual limit: 60
```

The limit silently becomes `N ×` intended — and worse, it *changes when you
autoscale*. The obvious fix: move the bucket state somewhere shared. Enter Redis
(this project's [docker-compose.yml](../docker-compose.yml) runs one on host port
6302, per the repo's port convention).

### Stage 2: shared state resurrects a race — TOCTOU

Naive shared-state flow: `GET` the bucket from Redis, decide in Rust, `SET` it
back. Correct-looking, and broken. Watch two instances race for the **last
token** (this is the interleaving from the header of
[redis_limiter.rs](../src/redis_limiter.rs)):

```
   time ──►
   instance A                     instance B                    Redis: tokens
   ─────────────────────────      ─────────────────────────     ─────────────
   GET  ─────────────► "1"                                            1
                                  GET  ─────────────► "1"             1
   1 ≥ 1 → decide ALLOW               │
        │                         1 ≥ 1 → decide ALLOW
   SET 0 ────────────►                │                               0
                                  SET 0 ────────────►                 0
   ▲                              ▲
   └── both admitted. One token spent twice. ──┘
```

Both instances **checked** a value that was stale by the time they **used** it —
Time-Of-Check-To-Time-Of-Use, **TOCTOU**. The race window is the gap between the
`GET` and the `SET`, and it's not a bug in either instance's code: each one, read
alone, is flawless. The race lives *between* them, in the interleaving. That's
why Card 3 says no amount of care in your Rust fixes it — Rust's borrow checker
guards memory within one process; nothing in the language can order two
processes' round-trips to a third machine.

Under load this isn't rare, it's *systematic*: the busier the key, the more
concurrent checks, the more over-admission — the limiter fails hardest exactly
where it matters most. The SPEC's Done-when demands a concurrency test proving N
simulated instances hammering one key **never over-admit**.

---

## 2. Why `INCR` isn't enough

Redis already has atomic single commands. `INCR key` increments and returns the
new value, atomically — no TOCTOU. For a plain fixed-window counter, `INCR` +
`EXPIRE` genuinely suffices.

But a token bucket's update isn't an increment. Per [doc 00](00-token-bucket.md),
every check must:

1. **read** two fields (`tokens`, `last_refill`),
2. **compute** the refill from elapsed time, clamp at capacity,
3. **conditionally deduct** — only if `tokens ≥ cost`,
4. **write back** both fields.

That's a read → arbitrary computation → conditional write. `INCR` can express
none of the middle. And composing several atomic commands (`HGET` then `HSET`)
does **not** yield an atomic sequence — other clients' commands interleave
between yours, which is precisely the race from §1. Atomic pieces don't add up
to an atomic whole.

You need the entire decision to execute as one unit. Redis gives you exactly
that: **server-side scripts**.

---

## 3. The idea: ship the decision to the data

A Lua script sent to Redis runs **atomically**: Redis executes commands on a
single thread, and a script occupies that thread from its first instruction to
its last — no other client's command can interleave anywhere inside it. Your
check-and-deduct becomes indivisible *by construction*:

```
                    instance A                instance B
                        │                         │
                        │  EVALSHA(script,        │
                        │   key, args)            │  EVALSHA(...)
                        ▼                         ▼
              ┌───────────────────────────────────────────┐
              │            Redis (single thread)          │
              │                                           │
              │   run A's script: read → refill math →    │
              │   deduct → write → reply {allowed=1}      │
              │   ─────────── then, and only then ─────── │
              │   run B's script: read → refill math →    │
              │   sees the truth A left → reply {denied}  │
              └───────────────────────────────────────────┘
```

There is no race window because there is no *window*: the check and the use are
one operation. Notice what you did **not** add: no lock, no retry loop, no
compare-and-swap. The serialization point is the data's home — Redis's single
thread was already serializing every command; the script just makes *your whole
decision* one command. One network round-trip per check, which matters on a hot
path where the SPEC budgets your p99 in fractions of a millisecond.

**The trap Card 3 names:** reaching for a distributed lock instead — `SETNX` a
lock key, `GET`, decide in Rust, `SET`, release. It "works," at 2× or more the
round-trips, plus a new failure genre: what if the lock holder crashes? Now you
need lock TTLs, and then fencing against expired-but-still-running holders…
**The script *is* the lock, for free** — with none of those failure modes,
because nothing distributed ever holds anything.

**The cost of atomicity** (nothing is free): while your script runs, Redis runs
*nothing else* — not other keys, not other databases, not health pings. A slow
script doesn't slow one key; it stalls **every client of that Redis**. Hence the
iron rule: scripts stay tiny — a few reads, arithmetic, a few writes. No loops
over unbounded data. Your bucket script is naturally microseconds; keep it that
way.

**In the wild:** this pattern — "push the decision to the store" — is how GitHub
and Shopify rate-limit, what `redis-cell` packages up, and the same shape as
distributed locks and dedup guards. It also foreshadows this repo's later rungs:
`FOR UPDATE SKIP LOCKED` (project 04) and transaction isolation (project 18) are
the same TOCTOU lesson wearing different storage engines.

---

## 4. The script lifecycle: `EVAL`, `EVALSHA`, `NOSCRIPT`

Sending the script text on every call works (`EVAL <script> ...`) but re-uploads
the same bytes millions of times. Redis caches scripts by content hash:

| Step | Command | What happens |
|---|---|---|
| Load once | `SCRIPT LOAD <text>` | Redis caches the script, returns its **SHA-1** |
| Hot path | `EVALSHA <sha> ...` | Runs the cached script — you send 40 hex chars, not the script |
| Cache miss | `EVALSHA` → error `NOSCRIPT` | The cache doesn't have it (fresh Redis, restart, `SCRIPT FLUSH`) |
| Recover | `EVAL <text> ...` | Runs **and** re-caches; subsequent `EVALSHA` works again |

The SHA-1 is literally the hash of the script's bytes — verified locally:
`printf 'return 1' | sha1sum` → `e0e1f9fabfc9d4800c877a703b823ac0578ff8db`, and
that's exactly the SHA a real Redis returns from `SCRIPT LOAD "return 1"`. Same
bytes, same SHA, on every Redis on earth — which is why clients can *precompute*
it without asking the server.

The part that separates a demo from production is the **`NOSCRIPT` fallback**.
Redis's script cache is in-memory and vanishes on restart. If your limiter only
ever calls `EVALSHA`, it works for weeks — then Redis restarts at 3 a.m. and
**every check fails** with `NOSCRIPT` until someone redeploys. The Done-when box
"loaded once, called by SHA, **falling back to `EVAL` on `NOSCRIPT`**" is
insisting your hot path self-heals: catch `NOSCRIPT`, `EVAL` once (re-caching
it), carry on. That's a *tested* path, not a comment.

**A design decision hiding in the ARGV list** — the scaffold's sketch passes
`now_ms` *from the Rust side* as an argument. Whose clock is that? Each of your N
instances has its own, and they skew. The alternative is asking Redis for its own
time inside the script (it has a `TIME` command). One choice makes the script a
pure function of its inputs (deterministic, replay-friendly); the other gives
every instance a *single shared clock* and makes skew irrelevant. Both are used
in production. Which matters more for a token bucket's refill math — and what
does N instances disagreeing about `now` by 100 ms do to budget? That's yours to
reason through in `docs/02-design.md`.

---

## 5. TTLs: don't let the keyspace grow forever

Every key that ever sends one request creates a bucket hash in Redis. Keys are
API keys, user ids, **client IPs** — an unbounded, attacker-influenced set (an
IPv4 scan alone is 4 billion potential keys). Without expiry, Redis memory only
ever grows; idle buckets from last month sit there forever, until Redis hits its
memory limit and starts evicting or refusing writes — taking your limiter down.

The fix is built into Redis: attach a TTL (`PEXPIRE` in the scaffold's sketch) to
every bucket key, **refreshed on every touch**. Active keys never expire (each
check renews them); a key that goes quiet self-evicts after the TTL. Steady-state
Redis memory becomes proportional to *recently active* keys, not *ever-seen*
keys.

The design question is the TTL's length: it must be **at least** as long as the
bucket takes to refill completely (expire sooner and a drained bucket can
resurrect **full** early — expiry *is* a state reset, so premature expiry
manufactures budget: the exact drift sin from [doc 00](00-token-bucket.md), now
in distributed form). Beyond that floor, longer just means idle keys linger.
Deriving the floor from `capacity / rate` is a one-line argument for your design
doc.

---

## 6. Fail open or fail closed? (There is no dodge)

Redis *will* be unreachable sometimes — restarts, network blips, upgrades. At
that moment every `Check` on the platform needs an answer *now*, and "the
limiter" can't shrug. Two coherent answers:

| | **Fail open** (allow) | **Fail closed** (deny) |
|---|---|---|
| Behavior with Redis down | Everyone gets in, unlimited | Nobody gets in |
| Protects | **Availability** — the platform keeps serving | **Abuse-resistance** — the limit is never exceeded |
| Sacrifices | The limit itself (attack window while down) | The entire platform (limiter outage = total outage) |
| Sane for | Convenience limits, fairness tiers, internal callers | Fraud/abuse gates, paid quota enforcement, login endpoints |

Neither is "correct" — they protect different things, which is why the SPEC
demands the choice be **explicit and configurable** rather than an accident of
whatever your error handling happens to do. The scaffold already carries the
policy: `fail_open: bool` on `RedisLimiter`, fed by the `FAIL_OPEN` env var in
[main.rs](../src/main.rs). Your job in `check()` is to catch the *transport*
error and honor it — and to notice which errors it should **not** swallow (is an
empty key a Redis failure? is a `NOSCRIPT` recoverable? [error.rs](../src/error.rs)
maps `InvalidArgument` and `Backend` to different gRPC codes for a reason).
And log the degraded decision — a limiter silently failing open is an outage
nobody notices until the abuse report.

This decision returns in project 10 as the circuit breaker's half of the same
question. Depth probes worth sitting with (Card 3): what happens platform-wide
when Redis p99 jumps 0.5 ms → 20 ms — and what would a local L1 bucket in front
of Redis (the SPEC's stretch item) buy and cost in consistency? Could you shard
buckets across N Redis nodes — what must your key→node routing guarantee?

---

## 7. The design space — what the `todo!()`s leave to you

Three build sites in [redis_limiter.rs](../src/redis_limiter.rs), each with the
scaffold's sketch and a real decision left open:

1. **`BUCKET_LUA`** — the atomic refill-and-deduct, in Lua. The scaffold gives
   the shape (KEYS/ARGV, read-or-start-full, refill, conditional deduct, write +
   `PEXPIRE`, return the triple). The discipline: **every** piece of arithmetic
   stays inside the script — one calculation done in Rust reopens the window.
2. **`check()`** — the `EVALSHA`-with-`NOSCRIPT`-fallback dance, decoding the
   script's reply into a [`Decision`](../src/limiter.rs), and the fail-open/closed
   branch on transport errors (which must *not* eat argument errors).
3. **`peek()`** — report state **without consuming budget** (it backs the `Peek`
   RPC in [ratelimit.proto](../proto/ratelimit.proto)). Sounds trivial; the
   subtlety is that a truthful answer still needs the refill math applied to
   *stale-at-rest* state. Where does that math run, and does it write anything?

Plus the proofs: the N-writers concurrency test (no over-admission on one
hammered key) and the Redis-down policy test. Stuck at any of them — `/hint` for
graduated nudges, `/quest` for the guided build with acceptance tests up front.

---

## 8. Mental model summary

| Concept | One-liner |
|---|---|
| N-instance drift | Per-instance state enforces N× the limit, and N changes with autoscaling |
| TOCTOU | The value you checked went stale before you used it; the race lives *between* processes |
| Why not `INCR` | Refill is read → compute → conditional write; atomic pieces don't compose into an atomic whole |
| Lua atomicity | Single-threaded Redis runs a script start-to-finish; the decision becomes one indivisible command |
| The cost | Your script blocks *everything* — scripts must stay tiny |
| `EVALSHA`/`NOSCRIPT` | Call by content-SHA; the cache dies on restart, so the `EVAL` fallback is a production requirement |
| TTL | Unbounded, attacker-influenced keyspace → every key expires; floor the TTL at full-refill time |
| Fail open/closed | Availability vs abuse-resistance — explicit, configurable, tested, logged |
| The anti-pattern | A distributed lock: more round-trips, new failure modes; the script already *is* the lock |

## Where you'll build this

**Module:** [src/redis_limiter.rs](../src/redis_limiter.rs) — `BUCKET_LUA`,
`todo!("V3: atomic Redis+Lua rate-limit check")`, and
`todo!("V3: peek at Redis bucket state without consuming")`.

**Done-when criteria this unlocks** ([SPEC V3](../SPEC.md)):

- [ ] State in Redis; decision runs as one atomic Lua script — no TOCTOU window
- [ ] Script called by SHA (`EVALSHA`), `EVAL` fallback on `NOSCRIPT`
- [ ] N concurrent instances never over-admit (concurrency test)
- [ ] Idle keys self-evict via TTL
- [ ] Explicit, configurable, tested fail-open/fail-closed policy

V1's math, made indivisible at the data's home — that's the whole vertical.
