# The Two Questions a Postmortem Asks

> Teaches why revocation is the price you pay for stateless verification, what makes
> a log *provably* complete rather than merely long, and why a simulator that
> reimplements evaluation is worse than no simulator. No prior knowledge assumed.
>
> Prepares you for **V6** in [`audit.py`](../src/iam_sts/audit.py) (`AuditLog.record`
> / `.flush`, `RevocationRegistry.revoke_access_key` / `.revoke_sessions_for_role` /
> `.is_revoked`, `PolicySimulator.simulate`). It is where the trade made in
> [doc 03](./03-self-describing-credentials.md) comes due.

---

## The one sentence to hold onto

**A credential that needs no lookup to *accept* needs an explicit mechanism to
*reject* — and an authorization log is only useful if it records *why*, not just
*what*.**

---

## 1. The problem before the solution

It is 3am. A key leaked. Two questions get asked, in this order, and neither is
"was IAM fast?"

**Make it stop.** The leaked credential must be dead *now*.

**Who did what.** Every decision it made, its inputs, and the statement that decided
it — reconstructable months later, by someone who was not there.

Building this vertical last is the right order, and not because it is least
important. It is the vertical that makes the other five **accountable**:
[doc 02](./02-composing-independent-authorities.md)'s deciding statement is only
useful if something records it, and
[doc 04](./04-caching-a-security-boundary.md)'s TTL is only a *promise* until
something measures when a revoke actually takes effect.

---

## 2. Why revocation is hard here specifically

Go back to [doc 03](./03-self-describing-credentials.md)'s trade. The session token
is self-describing:

```
verify a session token:
    check the MAC        ← one HMAC
    read the fields      ← the identity is right there
    check expires_at     ← against the clock
    done.  No lookup. No round trip. No shared datastore.
```

That is what made authorization fast enough to sit in front of every request in the
company. And it means:

> **You deliberately removed the step where a revocation check would naturally have
> lived.**

With a database-handle token (Design A in doc 03), revocation is `DELETE` and the
next request fails — free, because you were already paying for a lookup. With a
self-describing token you are not paying for a lookup, so there is nowhere for the
check to go. You have to put one back, and **every design that puts it back costs
some of the latency you bought.**

That is the vertical. Not "implement revocation" but *choose where to give the
latency back, and know exactly how much.*

---

## 3. The design space, priced

Three mechanisms, roughly in order of cost:

### (a) A revoked-session set

Keep the identifiers of revoked sessions; a revoked identity is one whose session
appears among them.

| | |
|---|---|
| Precision | exact — one session |
| Cost | one hash lookup on every authenticated request |
| Growth | unbounded until entries expire — and they *can* expire at the session's own `expires_at`, since after that the token is dead anyway |

That last row is the redeeming detail, and it is the same structural trick as
[doc 00](./00-proving-possession-without-transmission.md)'s replay guard: the
credential's own expiry bounds how long you must remember it. "Remember every
revocation forever" becomes "remember revocations for at most one session lifetime."

### (b) A per-role watermark

Keep, per role, an instant before which every session is dead. A revoked identity is
one whose role has a watermark and whose issue time falls before it.

| | |
|---|---|
| Precision | coarse — **all** sessions for a role, cannot target one |
| Cost | one lookup + one comparison, against a map the size of your role count |
| Growth | bounded by number of roles — small and stable |

This is what the real service exposes as *Revoke sessions*, and it is exactly why
[doc 03](./03-self-describing-credentials.md) insisted the issue time goes **inside
the token**. Without `issued_at` in the token this mechanism cannot exist.

Note the signature: `revoke_sessions_for_role(role_arn, issued_before)` — not
`revoke_all_sessions_for_role`. The parameter matters operationally: an incident
responder revoking at 14:03 wants the sessions that exist *now* dead, and does **not**
want to break the session they themselves create at 14:04 to fix the problem. A
watermark gives you that; a flag does not.

### (c) Ride the decision cache TTL

Do nothing extra. A revoked permission stops working within
`decision_cache_ttl_seconds`.

| | |
|---|---|
| Precision | none — it is a timer, not a decision |
| Cost | **zero** |
| Window | exactly the TTL |

Free, and it is exactly the sentence that makes
[doc 04](./04-caching-a-security-boundary.md)'s TTL a security parameter rather than
a tuning knob.

**Most real systems use more than one.** They compose: (b) for the blunt "kill this
role's sessions" button, (a) for surgical single-session revocation, (c) as the
backstop that bounds everything else. Pick, measure, and write down the window each
choice actually delivers.

### The cost constraint on `is_revoked`

Read its docstring: *"Consulted on the authentication path. Must be very cheap."*

This runs on **every authenticated request in the company**. A dict lookup or two is
fine. Anything that touches the network here has just put a round trip in front of
every API call in the account — which is precisely the thing
[doc 03](./03-self-describing-credentials.md) worked to eliminate. You would have
paid the full price of Design A while keeping none of its simplicity.

---

## 4. The revocation window is wider than you think

The criterion says the next request using a revoked key fails within the documented
window **at full load**. Under load, an idle-path test is close to meaningless,
because the window contains more than one thing:

```
   revoke() called at t=0
     │
     ├─ requests already past authentication, mid-evaluation      ← still complete
     ├─ decisions computed at t−ε, cached, still inside their TTL ← still served
     ├─ compiled policies referencing the old attachment          ← still resident
     └─ requests arriving at t+ε                                  ← these are the
                                                                     only ones your
                                                                     idle test covers
```

Every one of those is inside the window. The boss fight's ≤ 1s targets — for both a
detached policy and a revoked session — are measured *at full load* precisely because
that is when all four exist simultaneously. The honest number is the time until the
**first correct denial** with load running, not the latency of the revoke call.

---

## 5. What makes a log an audit trail

Anyone can append lines to a file. `AuditRecord` in
[`audit.py`](../src/iam_sts/audit.py) has a specific field set, and each field is
there because someone asks for it during an incident.

| Field | The question it answers |
|---|---|
| `sequence` | "Did I see everything?" — monotonic per writer, so a gap is **detectable** |
| `deciding_policy_type` / `_id` / `_statement_id` | "**Why**?" — not "denied", but "denied by `DenyUnlessMFA` in the boundary" |
| `consulted_context_keys` | "Can I reproduce this?" — the source IP and time of day that produced it are long gone |
| `cached` | "When was this decided?" — a decision *served* at 04:12 may have been *computed* at 04:11 |
| `principal_arn`, `action`, `resource`, `decision` | the what |

Three properties are worth pulling out.

### Allows are recorded, not just denies

The criterion says "**allows included**, not only denies." It is tempting to log
only failures — they are rarer and they are what alerts fire on.

But the postmortem question is *"what did this leaked key do?"*, and the answer is
made entirely of successful requests. A log of denials tells you about the attacks
that failed. The incident is the one that succeeded.

### `sequence` makes the log falsifiable

A log you cannot prove is complete cannot exonerate anyone — and exoneration is half
of what an audit trail is for. "There is no record of that access" means nothing if
records go missing routinely.

A monotonic per-writer sequence lets a consumer say *"I have 1..500 and 502; I am
missing 501"* — a statement about the world, not about the log. That is the
difference between evidence and a text file.

### The record is frozen and flat

`@dataclass(frozen=True, slots=True)`. A record with a mutable field is a record that
can be edited between the decision and the write — by a bug, or by a later stage
"enriching" it. Freeze it at the moment of decision.

---

## 6. Shedding beats dropping, and both beat blocking

`audit_queue_size` defaults to 10,000 in [`config.py`](../src/iam_sts/config.py).
What happens when a burst outruns the writer? Three options, and only one is
acceptable:

| Option | Failure mode |
|---|---|
| **Block** (`await queue.put()`) | Backpressure flows from the *audit* system into the *authorization* system. A slow disk becomes an authorization outage. **Never.** |
| **Drop silently** | A compliance problem nobody notices — the worst kind, because the log still looks fine |
| **Shed explicitly** | Refuse, count it (`shed_total`), and make the gap visible in the stream |

The third is the criterion. **An explicit gap is information; a silent gap is a lie.**
A consumer that can see "records 501–540 were shed under load" knows exactly what it
does not have. A consumer of a silently-dropping log believes it has everything.

This is a generalizable rule for any observability pipeline: **when you cannot keep
up, degrade in a way the consumer can detect.** Metrics do this with counters;
tracing does it with sampling ratios; an audit log does it with a visible gap marker
and a shed counter.

---

## 7. Off the hot path means genuinely off it

The criterion: enabling audit changes authorization p99 by **less than 5%**, measured
both ways.

That budget rules out a lot. At 20,000 decisions/sec, 5% of a 2 ms p99 is 100 µs —
and 100 µs is not much room for formatting, serializing, or acquiring anything.

So `record` must be genuinely cheap: **no formatting, no serialization, no `await` on
anything that can block.** Enqueue a frozen object. Format at *flush* time, in the
background task, where the cost is amortized over a whole batch.

And the flush itself: file I/O is blocking, and it runs on the same event loop
serving 20k decisions/sec. `await`ing a blocking write there stalls **every**
in-flight request, not just the audit path. The write belongs in a thread (or a
separate process).

> This is the classic version of the SPEC's "no blocking call on the event loop"
> criterion, and it has a nasty property: **it will not show up in a test and it will
> show up as latency spikes under load.** A unit test flushes one record to a fast
> local disk in microseconds. Production flushes ten thousand to a disk that
> occasionally takes 200 ms, and every concurrent request pays for it. See
> [doc 06](./06-backend-fundamentals.md) §4.

The flush loop is already wired in [`main.py`](../src/iam_sts/main.py)
(`_audit_flush_loop`), on `audit_flush_interval_seconds`. So is the shutdown ordering,
and it is graded: cancel the loops **first**, then flush. Tearing down before the
final flush drops records for decisions that were already served — precisely the gap
an auditor would find.

---

## 8. Tamper-evidence, and being honest about what it does

The criterion: a record altered or removed after the fact is **detectable**.

The usual mechanism is a **hash chain** — each record covers the previous record's
hash:

```
  record 1 ── h1 = H(r1)
  record 2 ── h2 = H(r2 ‖ h1)
  record 3 ── h3 = H(r3 ‖ h2)
                    ▲
     edit r2 ───────┘  now h3 does not recompute. Re-walking the file finds it.
```

Cost: one hash per record. Cheap.

Now the part the SPEC actually grades — `docs/25-design.md` must record what this
does **and does not** protect against:

| Claim | True? |
|---|---|
| Detects an edited record | ✅ the chain breaks at that point |
| Detects a removed record | ✅ same |
| **Prevents** tampering | ❌ — it detects, it does not prevent |
| Survives an attacker who can rewrite the whole file | ❌ — they recompute the chain and it verifies perfectly |

That last row is the honest limit. A hash chain protects against *partial* tampering
by someone who cannot rewrite everything. Against an attacker with full write access
to the file it is worthless — you need the chain head published somewhere they cannot
reach (a second system, an append-only store, a periodic external anchor).

Writing down what a control does *not* do is a real engineering skill and one this
SPEC keeps asking for. A control whose limits are undocumented gets trusted past
them, which is worse than not having it.

---

## 9. The simulator, and why parity is the whole criterion

`SimulatePrincipalPolicy` answers *"would this be allowed?"* without performing the
action. Two constraints define it.

### It must not be a second implementation

The criterion: the same decision **and the same deciding statement** as the live path
— "the same code, not a parallel implementation that will drift."

A reimplemented simulator drifts. Silently. And the day it matters is the day someone
trusts it about production: they simulate, see `Allow`, ship the change, and discover
the real evaluator disagrees.

This is why `PolicySimulator` holds a `PolicyEvaluator` rather than any logic of its
own, and why `build_state` in [`main.py`](../src/iam_sts/main.py) constructs **one**
evaluator and hands the same reference to both the `Authorizer` and the
`PolicySimulator`. That shared reference *is* the parity guarantee, structurally.

The proof the SPEC asks for is decision-and-reason parity across a **random policy
corpus** — property-style, not a handful of examples, because drift shows up in the
cases nobody thought to write down.

### It must not use the cache

The simulator answers what the policies say **now**, not what they said within the
TTL. Same evaluation, different freshness requirement.

Think about who is asking. Someone edits a policy and immediately simulates to check
their work. If the simulator served a cached decision, they would see the *old*
answer and conclude their edit did nothing — or worse, that it worked when it did
not. The one tool for reasoning about a change must not be subject to the staleness
window the change is fighting.

`/2025-01-01/simulate` and `/2025-01-01/authorize` in
[`routes.py`](../src/iam_sts/routes.py) share `authorization_request` and
`result_to_body` for exactly this reason: two endpoints that assemble their own
context, or format their own responses, are two endpoints that will eventually
disagree about what a deny looks like.

### One thing worth adding beyond correctness

The scaffold TODO suggests it: report every statement that was **considered**, not
only the one that decided. `ImplicitDeny` is a correct answer and a useless one —
"why did nothing match?" is the actual question people bring to a simulator, and
answering it requires showing the near-misses.

---

## 10. No secrets, ever, proven by grep

Two criteria, one in V1 and one here, and they are graded the same way: **assert it
with a test that greps the output, not by reading the code.**

Never in an audit record, a log line, an error body, a metric label or a trace
attribute:

- a secret access key,
- a session token,
- anything interpolated *out of* a credential.

The scaffold makes the default safe rather than relying on memory:
`bootstrap_secret_access_key` and `session_token_key` in
[`config.py`](../src/iam_sts/config.py) are `SecretStr`, whose `__repr__` renders as
`**********`. So the most common way a secret reaches a log aggregator forever —
`log.info("config", cfg=settings)` — prints nothing useful.

`AssumedRoleCredentials.secret_access_key` in [`sts.py`](../src/iam_sts/sts.py) is
the deliberate exception, and its docstring says why: it is about to be serialized
into a response body on purpose. That is the one place a secret legitimately crosses
the wire, and wrapping it there would only add a `.get_secret_value()` call that
*reads as approval* at every other site someone copies it to.

The test is a grep over a full capture of a signed exchange plus the whole audit
stream. Code review does not catch the third-party library that logs its arguments.

---

## 11. Mental model

| Idea | Why |
|---|---|
| Revocation is the price of stateless verification | You removed the lookup where the check would have lived |
| Three mechanisms, different prices | Exact set / role watermark / TTL backstop — most systems compose them |
| `is_revoked` must be a dict lookup | It runs on every request in the company |
| The window includes in-flight + cached | Measure under load; idle tests measure nothing |
| Log allows too | The incident is made of successful requests |
| `sequence` makes gaps detectable | A log you can't prove complete can't exonerate anyone |
| Shed explicitly, never block or drop | Blocking = outage; silent drop = a lie |
| Enqueue cheap, format at flush, write off-loop | 5% of 2 ms is 100 µs |
| Hash chain detects, does not prevent | And is worthless against full file rewrite — say so |
| Simulator shares the evaluator instance | Parity is structural, not aspirational |
| Simulator bypasses the cache | It must answer *now*, not within the TTL |

---

## Where you'll build this

[`src/iam_sts/audit.py`](../src/iam_sts/audit.py) — six `NotImplementedError`s:
`AuditLog.record` / `.flush`, `RevocationRegistry.revoke_access_key` /
`.revoke_sessions_for_role` / `.is_revoked`, `PolicySimulator.simulate`. The flush
loop, the shutdown ordering and the shared evaluator are already wired in
[`main.py`](../src/iam_sts/main.py).

This doc unlocks the V6 criteria about record completeness, absence of secrets, key
deactivation and session revocation under load, ordered gap-detectable streams,
simulator parity, audit-off-the-hot-path, and tamper-evidence.

What it leaves to you — and these are the graded decisions for `docs/25-design.md`
and `docs/25-benchmarks.md`: which revocation mechanisms you compose and the window
each actually delivers, your tamper-evidence scheme and an honest statement of its
limits, and the measured audit-on/audit-off p99 delta. `/hint` for nudges, `/quest`
to build it as a session.

Next: [the backend fundamentals](./06-backend-fundamentals.md) woven through all six
verticals.
