# The Fundamentals Woven Through All Six Verticals

> Teaches the backend engineering that the SPEC's horizontal checklist grades — the
> things that are not any one vertical's job but are every vertical's problem. Wire
> compatibility, error taxonomy, secret handling, boundedness, the event loop and the
> GIL, and what to measure. No prior knowledge assumed.
>
> Anchored to [`errors.py`](../src/iam_sts/errors.py),
> [`config.py`](../src/iam_sts/config.py), [`main.py`](../src/iam_sts/main.py) and
> [`routes.py`](../src/iam_sts/routes.py) — the modules that are *already
> implemented*, and are worth reading as worked examples rather than as scaffolding.

---

## The one sentence to hold onto

**In a service that fails by returning 200 to a request that should have been denied,
every ordinary engineering habit — error mapping, bounds, logging, shutdown — is a
security control.**

---

## 1. Wire compatibility: you do not get to design the protocol

The SPEC's protocol criteria all say the same thing: **`boto3` pointed at this
endpoint must work.** Not "an equivalent API". The real one.

That constraint is doing something valuable pedagogically. You cannot design around
a hard part by simplifying the interface. The AWS Query protocol is:

```
POST / HTTP/1.1
Content-Type: application/x-www-form-urlencoded

Action=AssumeRole&Version=2011-06-15&RoleArn=...&RoleSessionName=...
```

…and the response is **XML**, with the real element names. It is a protocol from
2011: form-encoded requests, XML responses, the action in a parameter rather than a
path. Nobody would design it today. Implementing it teaches something a REST API
would not — that **wire compatibility is a spec you satisfy, not a design you
choose**, and most of your career's integrations look like this.

The same rule made [doc 00](./00-proving-possession-without-transmission.md)'s
canonicalization hard: matching `botocore` byte-for-byte is only possible if you
implement what it does, not what would be reasonable.

### Two planes, two ports, two contracts

[`main.py`](../src/iam_sts/main.py) builds **two** FastAPI apps sharing one runtime:

| Plane | Port | Shape | Audience |
|---|---|---|---|
| API / management | 9025 | AWS Query protocol, SigV4 on everything | `boto3`, the `aws` CLI |
| Authorization | 9026 | JSON, versioned path `/2025-01-01/authorize` | projects 23 / 24 / 06 |

The authorization app is deliberately minimal — no management routes, no
`AssumeRole`. Less surface on the thing every request in the fleet touches.
`authz_host` defaults to `127.0.0.1`: this is an internal contract, not a public API.

The date-stamped path is the versioning the SPEC asks for, and the criterion is
specific: *a client written against it does not break when a new policy type is added
to the chain.* Think about what that forbids — a response that enumerates policy
types positionally, or a client that must know the layer count. Additive change must
be invisible.

> **The context dict is part of the contract.** `AuthorizeRequestBody.context` in
> [`routes.py`](../src/iam_sts/routes.py) is where the *calling* service reports the
> facts a condition might test — source IP, TLS, time of day, a resource tag. A
> service that sends an empty context can never be protected by a conditional policy.
> So the completeness of that dict is a contract between this project and its
> callers, and belongs in the versioned interface rather than in a wiki page.

---

## 2. Error taxonomy: the codes *are* the API

[`errors.py`](../src/iam_sts/errors.py) is fully implemented and worth reading as a
model. Every error carries `status_code`, `error_code`, `message` and `retryable`:

| Error | Status | Retryable | Means |
|---|---|---|---|
| `MissingAuthenticationToken` | 403 | no | no signature at all |
| `IncompleteSignature` | 400 | no | malformed `Authorization` header |
| `InvalidClientTokenId` | 403 | no | unknown / inactive / revoked key — **all three** |
| `SignatureDoesNotMatch` | 403 | no | the signature did not verify |
| `ExpiredTokenException` | 403 | no | temporary credentials past expiry |
| `AccessDenied` | 403 | no | **the chain said no — this is success** |
| `MalformedPolicyDocument` | 400 | no | rejected at write time |
| `NoSuchEntity` | 404 | no | |
| `EntityAlreadyExists` | 409 | no | |
| `LimitExceeded` | 409 | no | a documented bound was hit |
| `Throttling` | 429 | **yes** | + `retry-after` |

Three things to extract.

**The retryable split is load-bearing.** A client that retries a non-retryable error
turns one failure into an amplification loop; a client that gives up on a retryable
one turns a blip into an outage. Only `Throttling` sets `retryable`, and the handler
adds `retry-after: 1` for it.

**`AccessDenied` is not an error.** Read its docstring: *"a deny is this service
working correctly."* If denies land in your error rate, a policy problem gets
misdiagnosed as an outage — and the 3am response to "the error rate spiked" is very
different from the response to "someone deployed a bad policy." The SPEC requires
denies counted apart from errors, for the same reason project 24 separates throttles
from failures.

**5xx messages are scrubbed.** Look at `app_error_handler`: for `status_code >= 500`
it discards the instance message and returns the *class default*.

```python
if exc.status_code >= 500:
    log.error("request failed", error=str(exc), kind=type(exc).__name__)
    message = type(exc).message  # authored by us; known safe
```

The detail goes to the log, where you can see it; the caller gets a string you wrote
and vetted. An instance message on an unexpected path may carry a file path, a query
fragment, or a credential — and this is the one service where "helpful error message"
and "information disclosure" are the same sentence.

### One decision the SPEC leaves you

Should `AccessDenied` tell the caller *why*? The real service does, which is
enormously helpful for debugging and mildly helpful to an attacker mapping your
permissions. `AccessDenied.__init__` takes a `reason` — whether it reaches the wire
is yours, and goes in `docs/25-design.md`.

Contrast that with the deliberate *collapsing* in
[doc 00](./00-proving-possession-without-transmission.md) §7: unknown, inactive and
revoked key ids all raise the same error with the same message, because
distinguishing them builds a key-id oracle for an unauthenticated caller. **The rule
is not "always tell" or "always hide" — it is: who learns what, and were they already
entitled to know it?** An authenticated principal learning why *their own* request
was denied is telling them something about a policy that governs them. An
unauthenticated prober learning which key ids exist is telling them something they
had no claim to.

---

## 3. Fail closed, everywhere, provably

The single most repeated criterion in this SPEC:

> An exception, a timeout, an unparseable policy, or an unavailable dependency yields
> a **deny** — proven by fault injection at each layer, not argued from the code.

The outermost expression is already written: `unhandled_error_handler` in
[`errors.py`](../src/iam_sts/errors.py) turns anything unexpected into a refusal.

But an outer net is a backstop, not a design. Each layer has its own version:

| Layer | Fail-closed means |
|---|---|
| [SigV4](./00-proving-possession-without-transmission.md) | A parse error is a refusal, not a skipped check |
| [Policy](./01-the-policy-language-and-its-traps.md) | An unresolvable variable fails closed, never interpolates empty |
| [Evaluation](./02-composing-independent-authorities.md) | An exception mid-chain is a deny with a reason, not skipped layers |
| [STS](./03-self-describing-credentials.md) | A bad MAC and a malformed token raise identically |
| [Cache](./04-caching-a-security-boundary.md) | A cache error is a miss then a deny — never an allow |
| [Audit](./05-revocation-and-the-audit-trail.md) | A full queue sheds visibly; it never blocks the decision |

The generalization: **under partial failure, an authorization service gets slower or
stricter, never more permissive.** Every other service in the tier may degrade by
shedding load. This one degrades by denying.

And "proven by fault injection" is the operative phrase. Reading the code proves
nothing here, because the bug you are looking for is the path you did not think about.

---

## 4. Python: the event loop and the GIL

### No blocking calls on the loop

One blocking call stalls **every** concurrent request, not just its own. The
scaffold's `PYTHONASYNCIODEBUG=1` criterion catches the obvious cases; the ones that
bite are:

| Call | Where it hides |
|---|---|
| File I/O | the audit flush ([doc 05](./05-revocation-and-the-audit-trail.md) §7) |
| CPU-bound HMAC | SigV4, on every request |
| `json.loads` on a large document | policy parsing, if it reached the hot path |
| A synchronous HTTP client | anywhere a "quick lookup" gets added later |

The SPEC calls out SigV4 explicitly: it is CPU-bound HMAC work sitting on the request
path. Wherever you move it off the loop, **or deliberately leave it on**, the reason
and the measurement both get recorded. Leaving it on the loop can be right —
offloading has its own cost, and a few HMACs over a few hundred bytes is genuinely
fast. The criterion is that you *measured* rather than assumed.

### The GIL, and measuring it rather than believing anyone

CPython's global interpreter lock means threads do not run Python bytecode in
parallel. But C extensions may *release* it around work that does not touch Python
objects — and `hashlib` does exactly that, above a size threshold, because hashing a
large buffer is pure C.

So the shape of the answer is: **small hashes hold the GIL; large ones release it.**
Which means SigV4's throughput scaling depends on your payload sizes, and the answer
is not the same for a 200-byte `GetCallerIdentity` and a 2 MB `PutObject`.

The SPEC asks for a number, not a belief. The method:

```python
# hash the SAME total bytes in every row; vary only the chunk size and thread count
def bench(nbytes, nthreads): ...  # threads × loops of hashlib.sha256(b"x"*nbytes)


# scaling = t(1 thread) / t(4 threads).   ~1.0 → GIL held.   >1 → GIL released.
```

Two warnings from running this while writing the doc, both worth passing on:

1. **It is noisy.** Keep total work constant across rows, pin iteration counts, and
   run each row several times. A benchmark that varies two things at once measures
   neither.
2. **Behaviour near the threshold is not monotonic.** In a quick run on WSL2 the rows
   just above the release size were *slower* multi-threaded than single-threaded —
   plausibly the cost of releasing and re-acquiring the GIL per call exceeding the
   parallelism it buys, plus scheduler thrash. Whether that reproduces on your
   hardware is exactly the sort of thing that belongs in `docs/25-benchmarks.md`
   rather than in anyone's mental model.

Do not copy a threshold from a blog post — including this one. Measure it on the box
the number will describe.

### Attribute the hot path

The Definition of done requires a `py-spy` flamegraph and a `memray` run naming the
top bottleneck, and stating **how much of a decision is signature verification versus
policy evaluation versus cache lookup**. `make profile` in
[`makefile.py`](../makefile.py) is wired for it.

The reason this is a criterion: intuition about Python performance is reliably wrong.
People optimize the matcher and discover 70% of the time was JSON parsing, or
optimize hashing and discover it was dict allocation. **Choose the optimization target
from data.**

### `pyright` strict

Every `# type: ignore` carries a comment justifying it. There is exactly one in the
scaffold — on `_SharedSignalServer.capture_signals` in
[`main.py`](../src/iam_sts/main.py), because uvicorn types it loosely — and it says
so. That is the standard: an ignore is a documented exception, not a way to make an
error go away.

---

## 5. Bounded, on purpose, all of them together

Six bounded things, all in [`config.py`](../src/iam_sts/config.py):

| Bound | Default | Unbounded means |
|---|---|---|
| `signing_key_cache_size` | 1024 | an unbounded pile of **key material** in the heap, keyed by attacker input |
| `decision_cache_size` | 100000 | [doc 04](./04-caching-a-security-boundary.md) §5's eviction attack |
| `compiled_policy_cache_size` | 4096 | memory grows with policy churn |
| replay window (via `sigv4_clock_skew_seconds`) | 300 s | remember every signature forever |
| `audit_queue_size` | 10000 | a slow disk becomes an authorization outage |
| session table (via `session_reap_interval_seconds`) | 30 s sweep | sessions accumulate past expiry |

Two things worth noticing.

**They are tuned *together*, against the expected decision rate.** At 20k decisions/s
with a 1 s TTL, the decision cache needs room for roughly one TTL's worth of distinct
keys, and the audit queue needs to absorb one flush interval's worth of records. Sizing
each in isolation gives you six numbers that are individually plausible and jointly
wrong.

**The bound on `signing_key_cache_size` is not a performance nicety.** Its values are
key material. And note the cache is keyed by the key *id*, never the secret — putting
a secret in a dict key means it appears in a `repr`, a traceback, and anything that
dumps the mapping.

The boss fight checks all of this at once with one blunt criterion: after the load
stops, **RSS returns to within 10% of the pre-run baseline.** Nothing grew without
bound. That single number catches every leak in the table above.

---

## 6. Secrets: make the safe thing the default

Layered, so that no single step has to be remembered:

1. **`SecretStr` in config.** `bootstrap_secret_access_key` and `session_token_key`
   render as `**********`. The most common leak — `log.info("config", cfg=settings)` —
   prints nothing useful.
2. **One deliberate exception**, documented at the site.
   `AssumedRoleCredentials.secret_access_key` is a plain `str` because it is about to
   be serialized into a response on purpose. Wrapping it would add a
   `.get_secret_value()` call that *reads as approval* everywhere someone copies it.
3. **CSPRNG for everything generated.** Session ids, secrets, external ids — `secrets`,
   never `random`. `random` is a Mersenne Twister: observing 624 outputs recovers its
   entire internal state and every future value. It is a fine simulation tool and a
   catastrophic security one.
4. **Constant-time comparison everywhere.** Signatures, token MACs, external ids —
   `hmac.compare_digest`. See [doc 00](./00-proving-possession-without-transmission.md) §7.
5. **Prove it by grep.** A test over a full capture of a signed exchange plus the whole
   audit stream. Code review does not catch the third-party library that logs its
   arguments.

### The tension the SPEC wants written down, not glossed

> Secret access keys are stored so that a dump of the identity store does not yield
> usable credentials, **and the tension between that and SigV4's need for the raw key
> is written down rather than glossed over.**

This is a genuinely hard, genuinely unresolved problem, and noticing that is the
point. Password hashing works because verification only needs to check a *candidate*.
SigV4 verification needs the raw secret to re-derive the signing key — there is no
candidate to compare against. So a one-way hash is not available to you here.

The real options all move the problem rather than solving it: encrypt at rest with a
key held elsewhere (project **28**, KMS), keep the plaintext only in a process that
does nothing else, or accept the exposure and compensate with detection. Every one of
them has a cost. Writing down which you chose and what it does not protect against
is the deliverable — the SPEC is not asking you to solve it.

---

## 7. Observability that distinguishes causes, not just counts

The metrics list is specific, and every entry earns its place by making two situations
that look identical on a dashboard look different:

| Metric | Distinguishes |
|---|---|
| decisions by outcome (allow / explicit deny / implicit deny / error) | a bad `Deny` from a missing `Allow` from a bug |
| decision latency **split by hit and miss** | see [doc 04](./04-caching-a-security-boundary.md) §8 — the average is a lie |
| cache hit ratio | a working cache from a cache that is technically present |
| signature failures **by reason** | skew / unknown key / mismatch / malformed — and **exactly one of those means you are under attack** |
| `AssumeRole` rate, live sessions | a chaining loop from normal traffic |
| audit queue depth + shed count | "keeping up" from "silently behind" |

**Deny reasons must aggregate.** The criterion: *a deploy that breaks a trust policy
must look different from one that breaks a condition key.* Otherwise a spike in denials
is a support ticket instead of a graph.

That is why `AuthorizationResult.reason` is documented as *"safe to aggregate as a
metric label — so keep it a bounded set of phrases rather than an interpolated
string."* An interpolated reason (`f"denied for {principal}"`) is an
unbounded-cardinality label, and unbounded cardinality takes down your metrics backend
rather than your service. Fixed vocabulary in the label; the specifics in the audit
record.

Both planes scrape separately — see `test_authz_plane_has_its_own_metrics` in
[`tests/test_smoke.py`](../tests/test_smoke.py). The boss fight's numbers come from the
authorizer process, so it needs its own `/metrics`.

Tracing: a span per authorization carrying principal, action, resource, decision and
deciding statement — **and never the credential.** `RequestIdMiddleware` is already
outermost in [`main.py`](../src/iam_sts/main.py), so every log line emitted while
serving carries the request id, which is what lets you join a log to a trace to an
audit record.

### Timing side channels are a number, not an assumption

The criterion: *whether a deny is distinguishable from a nonexistent principal by
response time is a number in the benchmark doc and a deliberate decision.*

Read that carefully — it does not say "make them identical." It says measure the gap
and decide. Perfect timing equalization is expensive and sometimes not worth it. An
unmeasured gap you never decided about is the problem.

---

## 8. Graceful shutdown is a correctness property

Already implemented in [`main.py`](../src/iam_sts/main.py)'s lifespan, and the
**order is graded**:

```
SIGTERM
   │
   ├─ 1. cancel background loops   (audit flush, session reaper)
   ├─ 2. await their cancellation
   ├─ 3. final audit flush         ← records for decisions already served
   └─ 4. log what was flushed / abandoned
```

Reverse steps 1 and 3 and you drop records for decisions that were already answered —
**precisely the gap an auditor would find**, and one that appears only on deploys,
which is when it is hardest to notice.

`_SharedSignalServer` exists because two uvicorn servers share one process and one
signal; without it, a `SIGTERM` would be handled twice and one app would be torn down
under the other.

> **Read `_scaffold_guard` and then plan to forget it.** It catches
> `NotImplementedError` from background tasks so `make run` boots and stays up while
> you work through the SPEC. It deliberately does not swallow anything else — a real
> bug still crashes loudly. Once a vertical is built, its task runs for real and the
> guard becomes a no-op.

---

## 9. Consistency, stated honestly

Three criteria that are all the same skill: **say what the window is, then measure
it.**

- The decision cache TTL as a security bound — *"a revoked permission survives at most
  N seconds"* — verified under load.
- The propagation window from a control-plane write to every authorizer observing it,
  including **read-your-writes**: an operator who detaches a policy and immediately
  simulates must not see the old answer. (Which is exactly why
  [doc 05](./05-revocation-and-the-audit-trail.md)'s simulator bypasses the cache.)
- Invalidation precision, measured as the size and shape of the hit-ratio dip.

The common thread is that eventual consistency is fine and **undocumented** eventual
consistency is not. An operator who knows the window is 1 second waits a second. An
operator who does not know assumes zero, sees stale behaviour, and escalates.

---

## 10. Mental model

| Fundamental | The version specific to this service |
|---|---|
| Wire compatibility | `boto3` must work — a spec you satisfy, not a design you choose |
| Error taxonomy | Retryable split; a deny is **success**, not an error |
| Error messages | Scrub 5xx to the class default; log the detail |
| Information disclosure | Ask *who learns what, and were they entitled to know it* |
| Fail closed | Slower or stricter, never more permissive — proven by fault injection |
| Event loop | One blocking call stalls every request |
| GIL | Measure it; don't copy a threshold — including from this doc |
| Bounds | Six of them, tuned together; RSS returns to baseline |
| Secrets | Safe by default (`SecretStr`), one documented exception, proven by grep |
| Observability | Distinguish *causes*; bounded-cardinality labels |
| Shutdown | Ordering is a correctness property |
| Consistency | Undocumented eventual consistency is the bug |

---

## Where this shows up

Everywhere, which is the point — but concretely: the horizontal checklist in
[`SPEC.md`](../SPEC.md), graded the same way as the verticals (observable criterion +
Proof). `make verify` runs the gate CI runs: `fmt-check → lint → types → test`.

The already-implemented modules are the worked examples. Read
[`errors.py`](../src/iam_sts/errors.py) for the taxonomy and the 5xx scrub,
[`config.py`](../src/iam_sts/config.py) for `SecretStr` and every bound in one place,
and [`main.py`](../src/iam_sts/main.py) for lifespan ordering, dual-plane wiring, and
the dependency graph in `build_state` — which reads as the SPEC's own structure, with
the shared evaluator that makes
[doc 05](./05-revocation-and-the-audit-trail.md)'s parity criterion achievable at all.

Two of these are genuinely open questions rather than exercises: the tension in §6
between storing keys safely and SigV4 needing the raw secret, and the §7 timing
side-channel decision. The SPEC grades that you named the tension and chose
deliberately — not that you solved it.

Back to the start: [proving possession without transmission](./00-proving-possession-without-transmission.md).
