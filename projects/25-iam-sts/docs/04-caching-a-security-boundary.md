# The TTL Is Not a Performance Knob

> Teaches why an authorization cache is a different animal from every other cache
> you have built, what "the cache key must cover every input" actually means when
> the inputs are discovered during evaluation, and why precise invalidation is the
> hard half. No prior knowledge assumed — not of LRU, not of cache stampedes.
>
> Prepares you for **V5** in [`authorizer.py`](../src/iam_sts/authorizer.py)
> (`PolicyCompiler.compile` / `.invalidate`, `DecisionCache.get` / `.put` /
> `.invalidate_principal`, `Authorizer.authorize`). It caches the output of
> [doc 02](./02-composing-independent-authorities.md)'s evaluator.

---

## The one sentence to hold onto

**Caching a decision is caching a security boundary: the TTL is not how stale your
data may be, it is the maximum duration of a permission you already revoked.**

---

## 1. The problem before the solution

Every other service in this tier is allowed to be slow sometimes. This one is not,
and the reason is structural rather than a matter of pride:

```
   project 23 (DynamoDB)  ──┐
   project 24 (Lambda)    ──┼──► "may they?"  ──► IAM
   project 06 (S3)        ──┘         │
                                      └── nothing downstream can start
                                          until this answers
```

**IAM's p99 is a floor under everybody else's p99, and its availability is a ceiling
over everybody else's availability.** Project 23 cannot answer a `GetItem` faster
than this answers "may they?". So the entire game is doing less work per decision.

Count the work an uncached decision does, for one request:

| Step | Cost |
|---|---|
| Parse the JSON of every attached policy | tens of µs, per policy |
| Walk five layers × N statements | linear in total statements |
| Per statement: glob the action, glob the resource, evaluate conditions | string work per statement |
| Interpolate policy variables | more string work |

For a realistic principal — ten policies, a hundred statements — that is thousands
of string operations to answer a question whose answer was the same one microsecond
ago and will be the same one microsecond from now.

**But the naive fix is a vulnerability.** "Cache the decision for 60 seconds" means
an operator who detaches a policy watches it keep working for a minute. They will
detach it again, harder, and then open an incident.

So the vertical is: be fast, be correct, and be *current*, three properties that
pull against each other.

---

## 2. Three things you could cache, and what each buys

```
   raw JSON  ──parse──►  PolicyDocument  ──compile──►  CompiledPolicy  ──evaluate──►  Decision
             ▲                            ▲                             ▲
             │                            │                             │
        cache here?                  cache here?                   cache here?
```

| Cache what | Saves | Still pays | Invalidation |
|---|---|---|---|
| Parsed documents | JSON parse | five-layer walk, all matching | on policy edit |
| **Compiled policies** | parse + per-request prep | the walk itself | on policy edit |
| **Decisions** | everything | nothing | on policy edit **and** it must be precise |

The answer is *both* of the bottom two, and they solve different problems.

**Compilation** is about making each evaluation cheap. **Decision caching** is about
not evaluating at all. Keeping them separate matters because they invalidate
differently: a stale *decision* outlives a stale *compilation*, so evicting a
compiled policy alone fixes nothing anyone can observe. The `invalidate` TODO in
[`authorizer.py`](../src/iam_sts/authorizer.py) says exactly this.

### What goes in a compiled policy?

`CompiledPolicy` carries a `policy_id`, a `source_version`, and a TODO for the
interesting part. The naive answer — "the parsed statements" — is barely faster than
re-parsing. The interesting answers precompute what the matcher does per request.
The scaffold docstring names three directions worth thinking about: a set of service
prefixes so an `s3:*` policy is skipped instantly for a `dynamodb:` action;
statements bucketed by effect so denies are checked first; patterns pre-split into
segments so the matcher from [doc 01](./01-the-policy-language-and-its-traps.md) does
no string splitting at all.

Two constraints on whatever you choose:

- **Immutable.** It is shared across every concurrent request touching that policy.
  A mutable compiled artifact is a data race in the one place where a data race means
  *wrong authorization decision* rather than *wrong number on a dashboard*.
- **Versioned.** `source_version` exists so a compiled artifact can be checked
  against the document it came from. A compiled cache that cannot tell it is stale is
  the exact bug this vertical is trying not to have.

### Where compilation runs

"Off the request path" is a criterion. The honest answer is **at write time, in the
control plane**, with the hot path only ever reading an already-compiled artifact.

Compiling lazily on first use is easier and moves a multi-millisecond spike onto
whichever unlucky request arrives first after a change. Right after a deploy that
touches many policies, that is *all of them at once* — a self-inflicted thundering
herd, arriving exactly when you are already watching the dashboard for other reasons.

---

## 3. The cache key is a correctness problem

This is the part that separates an authorization cache from every other cache.

A normal cache key is "the identity of the thing you're fetching". Here the key must
cover **every input the decision depended on** — and some of those inputs are only
discovered *during* evaluation.

Start with the obvious key:

```
key = (principal_arn, action, resource_arn)
```

Now introduce one perfectly ordinary policy:

```json
{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::b/*",
 "Condition": {"IpAddress": {"aws:SourceIp": "10.0.0.0/8"}}}
```

And watch it fail:

```
 t=0   alice, s3:GetObject, arn:aws:s3:::b/x   from 10.0.0.5
         → evaluated: ALLOW      → cached under (alice, s3:GetObject, b/x)
 t=1   alice, s3:GetObject, arn:aws:s3:::b/x   from 203.0.113.9   ← the internet
         → cache HIT             → ALLOW                             ← WRONG
```

The condition was never consulted, because evaluation never ran. The first request
from inside the VPC poisoned the answer for every request from outside it.

**This is a security bug that presents as "it works on my machine."** No error, no
graph, no anomaly — the fast path returned quickly and confidently.

This is why `AuthorizationResult.consulted_context_keys` exists in
[`models.py`](../src/iam_sts/models.py), why `StatementMatch.consulted_keys` carries
it out of [doc 01](./01-the-policy-language-and-its-traps.md)'s matcher, and why
`ConditionEvaluator.evaluate` returns the keys alongside the verdict. It is one
thread running through three verticals, and it exists entirely to make this cache key
completable.

### The chicken and egg

Read the shape of the problem carefully:

> To build the key you need the consulted keys. To learn the consulted keys you must
> evaluate. If you evaluate, you did not need the cache.

Two shapes resolve it, and the SPEC wants you to pick one and defend it:

**(a) Key on the full request context.** Correct by construction — every input is in
the key. But the hit ratio collapses if the context carries anything request-specific:
a timestamp, a request id, a unique source port. Every request becomes a miss and
your cache is a memory leak with a hit-ratio metric.

**(b) Key in two stages.** First look up which context keys *this*
principal+action+resource combination consults, then build the real key from only
those. Faster and a far better hit ratio — with a subtle invalidation requirement:
**the set of consulted keys itself changes when a policy changes.** Add a condition
to a policy and your stage-one map is now wrong, in the generous direction.

Neither is free. Pick, write down why, and make the tests prove it — the SPEC's
cache-key-completeness criterion is a test that flips one context value and demands
a different decision.

---

## 4. Cache the denials too

An easy thing to skip, and it hands an attacker a lever.

Suppose allows are cached and denials are not. Then:

- A legitimate client's repeated requests: **hit**, ~microseconds.
- A hostile client sending garbage: **always miss**, full evaluation, every time.

The attacker now chooses your latency. Every request they send costs you a thousand
times what it costs them, and they never even have to guess a valid resource name —
*novel* is enough. That is a free amplification factor, which is why the boss fight
requires the denied path to be **no more than 20% slower** than the allowed path.

There is a subtlety worth naming: caching denials also caches *implicit* denials —
answers to questions about resources that do not exist. That is a large space, and
an attacker can enumerate it. Which leads directly to:

---

## 5. Eviction is a fairness problem

`decision_cache_size` defaults to 100,000 entries in
[`config.py`](../src/iam_sts/config.py). Under plain LRU, here is a two-line attack:

```
for i in range(200_000):
    authorize(principal=attacker, action="s3:GetObject",
              resource=f"arn:aws:s3:::bucket/{i}")
```

Every one of those is a miss, gets cached, and evicts something. After 100k
iterations the cache contains **nothing but the attacker's denials**, and every other
account in the fleet is now running on the miss path. One principal, no valid
permissions, took the hit ratio to zero for everybody.

The SPEC's criterion — "one noisy principal cannot evict the whole account's working
set, reproduced deliberately" — is this attack, and the boss fight runs it.

The design space, without picking for you:

| Approach | Idea |
|---|---|
| **Segment by principal** | Each principal gets a bounded share; a noisy one evicts only itself |
| **Admission policy** | Only cache on *second* sighting — one-shot keys never get in |
| **Frequency-aware eviction** | Evict by how often, not how recently, so a scan cannot displace a hot set |

There is a general principle worth extracting: **an unbounded key space plus LRU
equals a cache an attacker controls.** Whenever the key contains attacker-supplied
text — a resource name, a URL, a filename — the shape of your eviction policy is a
security property. This is the same lesson `ReplayGuard`, `SigningKeyCache` and the
audit queue each teach in their own vertical, which is why the SPEC's cross-cutting
section says *every* cache and buffer here is bounded on purpose.

---

## 6. Invalidation, and the herd you cause by fixing it

`decision_cache_ttl_seconds` defaults to **1.0**. That number is a sentence:

> *A revoked permission keeps working for at most one second.*

It is logged at startup in [`main.py`](../src/iam_sts/main.py) next to the ports —
the one number worth seeing on boot, because it is a security promise rather than a
tuning parameter.

But TTL alone is a *ceiling*, not a mechanism. The boss fight wants a detached policy
enforced within ≤ 1s **at full load**, which means explicit invalidation on write.
And that is where precision matters, in both directions:

```
   flush everything          flush too little
   ─────────────────         ────────────────
   ✅ definitely correct      ✅ hit ratio stays high
   ❌ hit ratio → 0           ❌ a revoked permission still works
   ❌ every request in the
      account hits the miss
      path simultaneously
      = self-inflicted
        thundering herd
        on a policy edit
```

Neither end is acceptable. `invalidate_principal` is the shape of the answer: an
index from principal → cached keys, so a policy edit drops exactly the affected
entries. That index is **memory you spend to buy precise invalidation**, and it is a
good trade because the alternative is measured in either seconds of vulnerability or
seconds of outage.

The SPEC measures this as "the size and shape of the hit-ratio dip" after a policy
edit — a graph, not an assertion. A vertical cliff to zero means you flushed
everything; a barely visible notch means you were precise.

> **Measure invalidation under load, not in an idle test.** An idle test proves the
> code path runs. Under load there are in-flight requests that already read the old
> policy, decisions computed microseconds before the write landed, and a queue of
> pending evaluations. All of that is inside the window, and none of it shows up when
> the system is quiet.

---

## 7. Two failure modes to design against up front

### Stampede

N concurrent requests miss on the same key. All N evaluate. All N write the same
answer.

At 20,000 decisions/sec against a popular key, "N" is not two — it is however many
requests arrive during one evaluation. The cold phase of the boss fight flushes the
cache *while load continues*, which is built to expose exactly this.

The fix is **single-flight**: the first miss for a key evaluates, and everyone else
waits on that same in-flight result. Worth designing in from the start, because
retrofitting it means threading a shared awaitable through code that assumed it owned
its own evaluation.

### Failing open

If the evaluation raises, the answer is **deny**. Never "allow because the cache was
empty and something went wrong."

This is [doc 02](./02-composing-independent-authorities.md)'s fail-closed rule
arriving at a new layer, and the cache introduces a fresh way to violate it: a cache
lookup that throws, a deserialization error on a cached entry, an eviction racing a
read. Each is a place where "I could not determine the answer" can be mistaken for
"there was no objection."

**Under partial failure, an authorization service gets slower or stricter — never
more permissive.**

---

## 8. Measure hits and misses separately

The boss fight is explicit:

- ≥ 20,000 decisions/sec sustained for 60s
- **p99 ≤ 2 ms on cache hits**
- **p99 ≤ 15 ms on misses**
- reported separately — "an average across the two is the exact lie this boss exists
  to teach you about"

Why it is a lie: at a 95% hit ratio, an average is 95% hit latency. It looks
excellent and tells you nothing about the 5% of requests that are slow — which are
disproportionately the *new* ones, from *new* principals, doing *new* things. The
people having a bad time are invisible in the mean, and they are exactly the people
whose experience you are trying to reason about.

This is why `AuthorizationResult.cached` exists. A latency histogram cannot be split
by hit and miss after the fact if the result does not say which it was. Mark it at
the point of decision or lose the ability forever.

`CacheStats` in [`authorizer.py`](../src/iam_sts/authorizer.py) carries hits, misses,
evictions, invalidations and entries — the five numbers that let you read the cache's
behaviour at an instant rather than reconstructing it from a histogram afterwards.
Evictions and invalidations in particular tell two different stories: a rising
eviction count under steady load is §5's attack; a spike in invalidations is a
control-plane storm.

---

## 9. The two planes must not share a bottleneck

The boss fight runs **control-plane writes at ≥ 100/sec throughout**, and requires
hot-path p99 to hold.

That is a criterion about structure, not speed. If policy writes take a lock the
read path also needs, or compilation happens inline on the request that observes the
change, or invalidation walks the whole cache — then writing policies makes
authorizing slow, and an operator responding to an incident degrades the service they
are trying to protect. At precisely the wrong moment.

Separating the planes is why compilation belongs at write time, why the compiled
artifact is immutable (readers never need a lock), and why invalidation must be
indexed rather than a scan.

---

## 10. Mental model

| Idea | Why |
|---|---|
| Compile once, off the request path | Lazy compilation is a herd after every deploy |
| Compiled artifacts immutable + versioned | Shared concurrently; must detect staleness |
| Cache decisions, not just documents | Documents still leave five layers to walk |
| Key covers every consulted context key | Otherwise one IP's answer serves every IP |
| Cache denials equally | Otherwise the attacker chooses your latency |
| Bounded + fair eviction | Otherwise one principal evicts the fleet |
| TTL stated as a security bound | It *is* the revocation window |
| Precise invalidation | Flush-all is a self-inflicted herd on a policy edit |
| Single-flight on miss | Cold cache under load = N duplicate evaluations |
| Fail closed on cache error | Slower or stricter, never more permissive |
| `cached` flag on the result | Hit/miss p99 is unrecoverable after the fact |

---

## Where you'll build this

[`src/iam_sts/authorizer.py`](../src/iam_sts/authorizer.py) — six
`NotImplementedError`s plus a design TODO on `CompiledPolicy`'s fields:
`PolicyCompiler.compile` / `.invalidate`, `DecisionCache.get` / `.put` /
`.invalidate_principal`, `Authorizer.authorize`.

Note how thin `Authorizer` is meant to be. All the *judgement* lives in
[doc 02](./02-composing-independent-authorities.md)'s pure evaluator; everything here
is about not doing that work twice. Keeping the split means a cache bug cannot
silently become a policy bug — and it is what lets V6's simulator call the evaluator
directly and provably match this path.

The SPEC's step 8 is emphatic about ordering: *only now* make it fast. Point project
23, 24 or 06 at the authorizer and watch a real request get correctly denied
**before** the optimization work. A cache over a correct evaluator is a performance
project; a cache over an evaluator you are still debugging is two problems wearing
one stack trace.

This doc unlocks the V5 criteria about compilation off the hot path, hit/miss
reporting, negative caching, bounded fair eviction, cache-key completeness, plane
separation, and degrading to slower-never-wrong.

What it deliberately leaves to you: what actually goes in `CompiledPolicy`, which of
the two cache-key shapes from §3 you pick, and which eviction policy survives the
boss fight's eviction attack. Those are the graded decisions, and they go in
`docs/25-design.md` and `docs/25-benchmarks.md`. `/hint` for nudges, `/quest` to
build it as a session.

Next: [revocation and the audit trail](./05-revocation-and-the-audit-trail.md) —
where the bill for all this speed comes due.
