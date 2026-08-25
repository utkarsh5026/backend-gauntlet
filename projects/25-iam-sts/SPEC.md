<!-- status:
state: not-started       # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 25 — IAM + STS

> Every other service in this tier is allowed to be down sometimes. This one is
> not, because it is in the path of *every request to every other service* — its
> p99 is a floor under everybody's p99, and its availability is a ceiling over
> everybody's availability. That would be hard enough. The part that makes it
> genuinely different is the failure mode: authorization does not fail by
> crashing. It fails by returning **200 OK** to a request that should have been
> denied, and no graph anywhere goes red. The incident starts weeks later, in a
> log nobody was reading.
>
> So three problems have to be solved at once, and they pull against each other.
> **Correct:** a policy language with wildcards, negations and conditions, five
> independent policy types that compose, and one rule — deny wins — that has to
> hold in every combination. **Fast:** tens of thousands of decisions a second,
> which means caching the decision, which means caching a security boundary.
> **Current:** an operator who detaches a policy expects it to *stick*, and every
> millisecond of cache TTL is a millisecond a revoked permission still works.
>
> This project builds that: SigV4 verification, the policy language and its
> matcher, the evaluation chain, `AssumeRole` and temporary credentials, the
> cached hot path, and the revocation and audit trail that make the other five
> accountable.

**Explicitly out of scope: federation and the console.** No SAML/OIDC identity
providers, no MFA devices, no web console, no multi-region replication of the
identity store (that is projects **07** and **09**), and no key management — KMS
is project **28**. Organization SCPs are *evaluated* here but not managed here.
One account, one node, and every hard problem is still on the table.

This is the **security horizontal for the whole tier**. Its payoff is not a green
test suite: it is pointing project **23**'s data plane, project **24**'s invoke
path, or project **06**'s object store at this service's authorization endpoint
and watching a real request get correctly denied.

## What it does (the easy part)
- Register the nouns: users, roles, and policies — identity, resource, boundary
  and trust policies, plus organization SCPs.
- **Verify a SigV4 signature** on an incoming request and answer *who is this*,
  without the secret key ever having crossed the network.
- **`AssumeRole`**: check a trust policy, mint temporary credentials with an
  expiry, a session policy and session tags, hand back a session token.
- **`GetCallerIdentity`**: the "who am I actually" call that ends most IAM
  debugging sessions.
- **Authorize**: given a principal, an action, a resource and a request context,
  return `Allow` / `Deny` **and the statement that decided it**.
- **`SimulatePrincipalPolicy`**: the same answer, without performing the action.
- Deactivate a key, revoke a session, and produce an audit trail an auditor can
  replay.

> **How to read this SPEC.** Every challenge below lists **Done when ALL true** —
> observable criteria you can check off — and a **Proof**: the test/bench/doc that
> *demonstrates* it (not "I think it works"). The criteria describe *what the system
> must do*, never *how*; figuring out the how is the entire point. A box only flips
> to ✅ when its Proof exists.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. SigV4 — *the secret never crosses the wire*
The password-shaped string in `~/.aws/credentials` is never sent anywhere. What
travels is a keyed hash over a **canonical rendering** of the request, computed
with a key derived through four chained HMACs — date, then region, then service,
then the literal `aws4_request` — so a signature scraped off the wire is useless
tomorrow, in another region, against another service. Canonicalization is where
all the bugs live: two semantically identical requests must produce two
byte-identical canonical forms, and the moment your normalization disagrees with
the client's by one space inside a header value, every request from every real
SDK fails with `SignatureDoesNotMatch` and no further clue. Build the verifier in
`src/iam_sts/sigv4.py`.

**Done when ALL true:**
- [ ] A request signed by a **real AWS SDK** (`botocore`'s signer, or the `aws` CLI) verifies unmodified — you did not relax canonicalization to make it pass.
- [ ] Changing **any signed byte** invalidates the signature — a signed header's value, a query parameter, one byte of the body — while changing an **unsigned** header does not. Every signed field is covered, proven field by field.
- [ ] The secret access key never appears in a request, a log line, an error body, a metric label or a trace — proven by grepping a full capture of a signed exchange, not by reading the code.
- [ ] The derived signing key is **scoped**: a signature valid for one date, region or service is rejected for the other two, each proven separately.
- [ ] A request whose timestamp is outside the **clock-skew window** is rejected with a distinct error naming skew, and the window — including whether it is asymmetric, and why — is documented.
- [ ] A **replayed** byte-identical signed request is rejected, and the mechanism is documented together with what it costs in memory and in coordination.
- [ ] Signature comparison is **constant-time**, and a test demonstrates the comparison cannot short-circuit on the first differing byte.
- [ ] `UNSIGNED-PAYLOAD` and **presigned URLs** (`X-Amz-Expires` in the query string, expiry capped) each verify correctly or are explicitly rejected — no unhandled variant is silently accepted.

**Proof:** a test that signs with `botocore.auth.SigV4Auth` and verifies here; a
per-field tamper table asserting every signed field is covered; skew tests at both
edges of the window; a replay test; `docs/25-design.md` records the
canonicalization rules and the replay-prevention mechanism.

*Concept to internalize:* a message authentication code as **proof of possession
without transmission** — and why deriving a key by scope turns one long-lived
secret into a fleet of narrow, self-expiring ones.

### V2. The policy language — *a tiny grammar with an enormous blast radius*
A policy is JSON, which makes it look like configuration. It is a language:
`Action`, `Resource`, `Principal`, their `Not*` inversions, wildcards, condition
operators, and variables that interpolate the caller into the ARN they are allowed
to touch. Every one of those is a place to be accidentally generous. `s3:Get*` and
`s3:*` are one character apart and several incidents apart. `NotAction` inverts
the intuition of everyone who reviews it. A condition key that is simply *absent
from the request* silently makes some operators pass and others fail, and which is
which is not guessable. Build the document model and the matcher — the matching of
one statement against one request — in `src/iam_sts/policy.py`. Composing many
policies is V3's problem, not this one's.

**Done when ALL true:**
- [ ] `Action`, `Resource` and `Principal` matching handles `*` and `?` wildcards, and case sensitivity is **correct per segment** — the service prefix and the resource part do not follow the same rule, and your tests state which is which.
- [ ] ARNs are parsed **structurally**, never by string prefix: an ARN differing only in partition, region or account never matches, and a crafted ARN with `:`, `/` or `*` inside a segment cannot escape into the next one.
- [ ] `NotAction` / `NotResource` / `NotPrincipal` evaluate correctly, and one test exists whose entire purpose is to demonstrate `NotAction` granting far more than its author intended.
- [ ] **Condition operators** cover the string, numeric, date, boolean, IP-address and ARN families, plus the `...IfExists` suffix and the set forms (`ForAllValues:` / `ForAnyValue:`), with multi-valued context keys handled correctly.
- [ ] A condition on a key **absent from the request** resolves per the documented rule for that operator, and a test pins the negated operators — those are the ones that surprise people, and the surprise is an accidental allow.
- [ ] **Policy variables** (`${aws:username}`, `${aws:PrincipalTag/team}`) interpolate from the request context, and an unresolvable variable **fails closed** rather than interpolating an empty string into a wildcard.
- [ ] Malformed, oversized or over-nested policy documents are rejected **at write time** with a useful error — an unparseable policy is never stored to be discovered at evaluation time, when the only safe thing left to do is deny.
- [ ] Wildcard matching has **bounded cost**: no pattern-and-input pair blows up the matcher, asserted by a timing test on adversarial input rather than by inspection.

**Proof:** a table-driven test over (statement, request) pairs with the expected
match, including the `NotAction` trap and the absent-key cases; a property test
over ARN parsing asserting no segment can escape; a bounded-cost test on
adversarial wildcards; `docs/25-design.md` records the matching semantics you
implemented **and the ones you deliberately did not**.

*Concept to internalize:* why authorization languages are deny-by-default with no
way to negate the default — and why a wildcard is a promise about a namespace that
has not been invented yet.

### V3. The evaluation chain — *five authorities, any one of which can say no*
"Does the identity policy allow it?" is not the question. The real one runs a
gauntlet: an organization **SCP** at the ceiling, the **resource** policy on the
thing being touched, a **permission boundary** on the principal, a **session
policy** riding on the temporary credentials, and finally the **identity**
policies. An explicit `Deny` at any layer ends it immediately. An `Allow` is only
an allow if every layer that *must* allow does — and **which layers must allow
changes depending on whether the call is same-account or cross-account**. That
asymmetry is the single most misunderstood rule in AWS, and it is precisely what
makes cross-account access safe. Build the chain in `src/iam_sts/evaluation.py`.

**Done when ALL true:**
- [ ] An **explicit `Deny` wins** over any number of `Allow`s, proven at every one of the five layers independently.
- [ ] With no matching statement anywhere the result is **deny**, reported as an *implicit* deny and distinguishable from an explicit one — because the fix for each is completely different.
- [ ] **Same-account** access is granted when *either* the identity policy or the resource policy allows; **cross-account** requires **both**. Both directions proven in one test, in both directions.
- [ ] A **permission boundary** caps without granting: a principal holding `Allow *` with a narrow boundary can do only what the boundary permits, and a boundary alone grants nothing at all.
- [ ] An **SCP** that does not allow an action makes it unavailable to *every* principal in the account — the account root included — proven.
- [ ] A **session policy** can only narrow the assumed role's permissions, never widen them, even when it explicitly names an action the role does not have.
- [ ] Every decision carries the **deciding statement**: which policy type, which policy, which `Sid`, and which effect ended the evaluation — for allows exactly as much as for denies.
- [ ] Evaluation is **total and fail-closed**: any error inside it — a bad policy, a missing context key, an unexpected exception — produces a **deny with a reason**, never an allow and never a 500.

**Proof:** a matrix test over {same-account, cross-account} × {identity
allow/deny/silent} × {resource allow/deny/silent} with the expected decision in
every cell; independent boundary, SCP and session-policy tests; a fault-injection
test asserting an exception inside the evaluator yields a deny;
`docs/25-design.md` records the evaluation order you implemented beside the rule
it models.

*Concept to internalize:* authorization as the **intersection of independent
authorities** — and why "deny wins, and the default is deny" is the only
composition rule that stays safe when the policies are written by people who never
meet.

### V4. STS — *credentials that expire, for a principal that does not exist*
`AssumeRole` is the primitive that makes everything else survivable: instead of
handing out a long-lived key, you publish a role that anyone satisfying its
**trust policy** may briefly *become*. What comes back is a triple — access key,
secret, and a **session token** — and the session token is where the whole design
lives. It is not a database handle. It is a self-describing, integrity-protected
bundle carrying the assumed identity, the session policy, the tags and the expiry,
so that the thing verifying it needs **no lookup at all** — which is exactly how
authorization gets fast, and exactly why revocation later gets hard. Then the
sharp edges: role chaining silently truncates your duration, `ExternalId` exists
solely because of the confused deputy, and a role trusting `"AWS": "*"` is a public
front door into your account. Build it in `src/iam_sts/sts.py`.

**Done when ALL true:**
- [ ] `AssumeRole` returns temporary credentials that **authenticate** (V1 verifies them) and **authorize as the role** — a test proves the original caller's own permissions are gone, not merely added to.
- [ ] The **trust policy** decides who may assume: an unnamed principal is refused, and that refusal is distinguishable from "the caller lacks `sts:AssumeRole`" — two different fixes.
- [ ] The session token is **integrity-protected**: a single flipped byte invalidates it, and a holder can neither read nor forge its contents — proven by a test that tries both.
- [ ] Credentials **expire** at their stated instant: a request signed one second after expiry is refused with a distinct `ExpiredToken` error, and the clock source is documented.
- [ ] `DurationSeconds` is honoured, capped by the role's maximum, and **role chaining** applies the documented shorter cap — with chain depth bounded and the bound enforced.
- [ ] **`ExternalId`** is enforced when the trust policy requires it: assuming without it fails, and the test's own docstring names the attack this prevents.
- [ ] **Session policies and session tags** ride inside the token and take effect in V3's chain; **transitive** tags survive a chained assume and non-transitive ones do not.
- [ ] `GetCallerIdentity` reports the **assumed-role ARN and session name**, not the underlying user — the distinction that ends most IAM debugging sessions.

**Proof:** an end-to-end test that signs with a user's key, assumes a role,
re-signs with the temporary credentials, and gets a **different** authorization
result; a token-tamper test; an expiry test; an `ExternalId` test named for the
confused deputy; `docs/25-design.md` records the token format, what protects it,
and the chaining caps.

*Concept to internalize:* the **self-describing bearer credential** — why putting
the identity *inside* a tamper-evident token removes a database lookup from every
request in the company, and what you trade away (revocation) to get it.

### V5. The authorization hot path — *every request in the company waits here*
IAM's p99 is a floor under every other service's p99: nothing in the fleet is
allowed to be faster than authorization. So the entire game is doing the least
possible work per decision — compile a policy once instead of walking JSON on
every call, cache the **decision** rather than the documents, cache denies as
carefully as allows (or a hostile client trivially bypasses your cache), and keep
the cache small enough that one large account cannot evict everything a small one
needed. And then the part that makes this a security problem rather than a
performance one: a cached allow that outlives the policy change which revoked it
is a vulnerability whose severity is measured in **seconds of TTL**. Build the
compiler and the cache in `src/iam_sts/authorizer.py`.

**Done when ALL true:**
- [ ] Policies are **compiled once** into a form the hot path evaluates without re-parsing JSON, and compilation happens off the request path.
- [ ] A repeated identical authorization request is served from cache, with a **measured** hit ratio and p99 reported **separately for hit and miss**.
- [ ] **Negative decisions are cached too**: a client hammering denied requests gets no slower a path than an allowed one — measured, because otherwise the deny path is a free amplification factor.
- [ ] A policy change **invalidates exactly the affected decisions** within a documented propagation window, measured **under load** rather than asserted in an idle test.
- [ ] The cache is **bounded** with a stated eviction policy, and one noisy principal cannot evict the whole account's working set — reproduced deliberately.
- [ ] The cache key includes **every input the decision depended on** — principal, action, resource, and each condition key actually consulted — proven by a test that changes one context value and gets a different decision.
- [ ] The **control plane is not on the hot path**: writing policies at a sustained rate does not degrade authorization p99 beyond a stated bound, measured in the same run.
- [ ] Under a burst larger than the cache, behaviour degrades to **slower, never to wrong** — the miss path runs at full load with zero incorrect decisions.

**Proof:** `bench/` numbers for hit and miss p99 and the hit ratio; an
invalidation-under-load test that measures the propagation window; a
cache-eviction fairness test; a cache-key-completeness test that flips one
condition value; numbers in `docs/25-benchmarks.md`.

*Concept to internalize:* caching an authorization decision is **caching a
security boundary** — the TTL is not a performance knob, it is the maximum
duration of a permission you already revoked.

### V6. Revocation & the audit trail — *the two questions a postmortem asks*
When something goes wrong, nobody asks whether IAM was fast. They ask two things.
**Make it stop** — a leaked key or a live session must be dead *now*, which is
genuinely hard, because the entire point of V4 was to remove the lookup that would
have made revocation free. And **who did what** — every decision, its inputs, and
the statement that decided it, in a trail an auditor can replay months later.
Building this vertical last is the right order: it is the one that makes the other
five accountable. Build the audit log, the revocation registry and the policy
simulator in `src/iam_sts/audit.py`.

**Done when ALL true:**
- [ ] Every authorization decision emits an audit record carrying principal, action, resource, decision, **deciding statement**, and the condition keys actually consulted — **allows included**, not only denies.
- [ ] Audit records **never contain a secret**: no secret key, no session token, no value interpolated out of a credential — asserted by a test that greps the whole audit stream after a signed exchange.
- [ ] A long-lived access key can be **deactivated and deleted**, and the next request using it fails within the documented window **at full load**.
- [ ] A session issued by `AssumeRole` can be **revoked before its expiry** — one session, or every session for a role at once — and the cost of doing so is measured and documented, because V4 deliberately removed the lookup that would have made it free.
- [ ] The audit stream is **ordered and gap-detectable**: a consumer can tell it missed a record, and a burst that outruns the writer **sheds explicitly** rather than dropping silently.
- [ ] A **policy simulator** answers "would this be allowed" without performing the action, returning the same decision *and the same deciding statement* as the live path — the same code, not a parallel implementation that will drift.
- [ ] Audit writing is **off the hot path**: enabling it changes authorization p99 by less than a stated bound, measured both ways.
- [ ] The trail is **tamper-evident**: a record altered or removed after the fact is detectable, and the mechanism is documented.

**Proof:** tests for record completeness and for the absence of secrets; a
revocation test that measures the window under load; a simulator test asserting
decision-and-reason parity with the live path across a random policy corpus;
`docs/25-benchmarks.md` records the audit-on/audit-off p99 delta.

*Concept to internalize:* revocation is the **price of stateless verification** —
and an authorization log is only useful if it records *why*, not just *what*.

---

## Horizontal checklist (the backend fundamentals)

Each item is **done when its criterion is observably true** — same rule as the verticals.

### Protocols
- [ ] SigV4 is **wire-compatible**: a request signed by `botocore` or the `aws` CLI verifies unmodified, and a presigned URL those tools generate works here.
- [ ] The STS surface mirrors the real **Query protocol** — `Action=AssumeRole`, `Version=2011-06-15`, form-encoded parameters, **XML** responses using the real element names — closely enough that `boto3` pointed at this endpoint works.
- [ ] IAM management uses the real action names (`CreateRole`, `PutRolePolicy`, `AttachUserPolicy`, `SimulatePrincipalPolicy`) and returns AWS-shaped errors.
- [ ] Errors carry the real codes and status (`SignatureDoesNotMatch`, `AccessDenied`, `ExpiredToken`, `InvalidClientTokenId`, `MalformedPolicyDocument`, `NoSuchEntity`, `Throttling`) with a documented retryable / non-retryable split.
- [ ] The **authorization endpoint** consumed by projects 23/24/06 has an explicitly versioned contract, and a client written against it does not break when a new policy type is added to the chain.

### Cryptography & secrets
- [ ] Every secret comparison is **constant-time**, and every generated secret, session id and external id comes from a **CSPRNG** — not `random`.
- [ ] Secret access keys are stored so that a dump of the identity store does not yield usable credentials, and the tension between that and SigV4's need for the raw key is written down rather than glossed over.
- [ ] **No secret reaches a log line, an error body, a metric label, a trace attribute or an audit record** — enforced by a test, not by code review.
- [ ] Derived signing keys are cached (they are per date/region/service), the cache is **bounded**, and its lifetime is tied to the scope the key was derived for.
- [ ] The session-token key can be **rotated** with both keys accepted during an overlap window, and tokens signed by the retired key stop verifying afterwards — proven.

### Correctness & safety
- [ ] The service **fails closed** everywhere: an exception, a timeout, an unparseable policy, or an unavailable dependency yields a **deny** — proven by fault injection at each layer, not argued from the code.
- [ ] Every limit is enforced *and* documented: policy size, statements per policy, policies per principal, condition keys per statement, role-chain depth, session-token size.
- [ ] Untrusted input never reaches a wildcard matcher, an ARN parser, or a log line unbounded — a hostile principal name, action or resource cannot exhaust CPU or memory.
- [ ] Timing side channels are **measured, not assumed**: whether a deny is distinguishable from a nonexistent principal by response time is a number in the benchmark doc and a deliberate decision.

### Caching & consistency
- [ ] The decision cache's TTL is stated as a **security** parameter — "a revoked permission survives at most N seconds" — and that N is verified under load.
- [ ] Eventual consistency is **honest**: the propagation window from a control-plane write to every authorizer observing it is measured and documented, including the read-your-writes case.
- [ ] Invalidation is **precise**: changing one policy does not flush every decision in the account, measured as the size and shape of the hit-ratio dip.

### Observability
- [ ] Metrics at `/metrics`: **decisions by outcome** (allow / explicit deny / implicit deny / error), decision latency histogram **split by cache hit and miss**, cache hit ratio, signature verifications and failures **by reason**, `AssumeRole` rate, live session count, audit queue depth and shed count.
- [ ] A span per authorization carrying principal, action, resource, decision and **deciding statement** — and never the credential.
- [ ] **Deny reasons are aggregated** so a spike in one is a graph rather than a support ticket: a deploy that breaks a trust policy must look different from one that breaks a condition key.
- [ ] Signature failures are broken out by cause — skew, unknown key id, mismatch, malformed — because they mean very different things and exactly one of them means you are under attack.

### Python & runtime
- [ ] **`pyright` strict passes clean** — every `# type: ignore` carries a comment justifying it.
- [ ] **No blocking call on the event loop:** runs clean under `PYTHONASYNCIODEBUG=1`. SigV4 is CPU-bound HMAC work sitting on the request path — wherever it is moved off the loop, or deliberately left on it, the reason and the measurement are both recorded.
- [ ] **Bounded caches and buffers sized on purpose:** the decision cache, the derived-key cache, the compiled-policy cache, the replay window and the audit queue all have explicit limits, tuned together against the expected decision rate.
- [ ] **Graceful shutdown** flushes the audit queue and stops accepting decisions on SIGTERM — no acknowledged decision is lost, and what was abandoned is reported.
- [ ] **The GIL's cost is measured, not assumed:** the benchmark states whether decision throughput scales with concurrency, and what `hashlib`'s GIL-release threshold does to the SigV4 path at these payload sizes.
- [ ] **The hot path's cost is attributed:** a profile separates canonicalization, HMAC, policy matching and cache lookup, so the optimization target is chosen from data rather than from intuition.

---

## Cross-cutting scale skills (every project carries these)
- **Backpressure & bounds:** every cache, the replay window, the session table and
  the audit queue are bounded; a hostile caller cannot grow memory by sending
  novel principals, actions or resources.
- **Graceful shutdown:** stop accepting decisions, flush the audit trail,
  checkpoint, and report what was abandoned.
- **Benchmarks with numbers:** `bench/` + `docs/25-benchmarks.md` — hit and miss
  latency distributions, throughput by concurrency, and the measured cost of a
  revocation.

## Definition of done
The project is **done when ALL true:**
1. Every vertical + horizontal box above is checked (each with its Proof).
2. The 🐉 boss fight below is **defeated** — the load generator and the adversarial
   corpus live in `bench/`, the numbers in `docs/25-benchmarks.md`.
3. `docs/25-design.md` records the five decisions this SPEC grades: **the
   canonicalization and replay-prevention rules, the policy matching semantics,
   the evaluation order and its cross-account asymmetry, the session-token format
   and what protects it, and the decision cache's TTL and invalidation stated as a
   security bound.**
4. `make verify` is green — `ruff` clean, `pyright` **strict** with zero errors,
   and `pytest` passing; no `NotImplementedError` remains on a checked path.
5. A **profile** is committed: a `py-spy` flamegraph and a `memray` run in
   `docs/25-benchmarks.md`, naming the top bottleneck and stating how much of a
   decision is signature verification versus policy evaluation versus cache
   lookup.
6. **The tier is wired together:** project **23**, **24** or **06** calls this
   service's authorization endpoint, and a real request to that service is
   correctly denied by a policy written here — demonstrated end to end, not
   described.

## 🐉 Boss fight — The Confused Deputy

> It is 3am and nothing is down. Latency is flat, the error rate is zero, every
> dashboard is green — which is the entire problem, because an hour ago a request
> that should have been denied was allowed, and the graph that would have shown
> you is the one currently reading *fine*. The deputy is not an attacker. It is a
> service that holds permissions on someone else's behalf and was asked, politely,
> to use them. Meanwhile fifty thousand honest requests a second are blocked
> waiting on your answer, an operator is detaching a policy and expecting it to
> **stick**, and a session revoked two seconds ago is still cheerfully signing
> requests. Being fast, being correct, and being current are three different
> problems here, and this boss makes you hold all three at once.

**Arena:** `bench/` load generator against `make run`, four phases. **(1) Hot
path** — sustained authorization decisions over a realistic corpus: hundreds of
principals, thousands of statements, high cache locality. **(2) Adversarial** — a
committed suite of *near-miss* requests fired continuously: wildcard edge cases,
ARN confusables, absent condition keys, expired and tampered session tokens,
replayed signatures, and cross-account calls missing exactly one side of the
grant. **(3) Revocation** — a policy detach and a session revoke issued at full
load, with the time until the first correct denial measured. **(4) Cold** — the
decision cache flushed while the load continues.

**The boss falls when ALL true:**
- [ ] **Zero false allows** across the entire adversarial corpus (≥ **500** distinct near-miss cases), and **zero false denies** across a valid corpus of comparable size. This box is not negotiable and cannot be traded against any number below it.
- [ ] ≥ **20,000 authorization decisions/sec** sustained for 60s, with **p99 ≤ 2ms on cache hits** and **p99 ≤ 15ms on misses**, reported separately — an average across the two is the exact lie this boss exists to teach you about.
- [ ] ≥ **5,000 SigV4 verifications/sec** at **p99 ≤ 10ms**, measured separately from the authorization decision, because the two scale for different reasons.
- [ ] Decision **cache hit ratio ≥ 90%** on the realistic corpus, with the miss path exercised at full load and still returning correct decisions.
- [ ] A **detached policy is enforced within ≤ 1s** at full load, and no decision issued after that instant used the stale policy.
- [ ] A **revoked session stops authenticating within ≤ 1s** at full load, and the cost of that revocation appears in the numbers rather than being hidden by them.
- [ ] The **denied path is no more than 20% slower** than the allowed path — anything worse hands a hostile client a free amplification factor.
- [ ] **Control-plane writes at ≥ 100/sec** throughout the run do not push hot-path p99 past its target: the two planes do not share a bottleneck.
- [ ] Memory is **flat**: after the load stops, RSS returns to within **10%** of the pre-run baseline — no cache, session table, replay window or audit buffer grew without bound.
- [ ] Every decision in the run appears **exactly once** in the audit trail with its deciding statement, and enabling the audit path cost **< 5%** of hot-path p99.

**Proof:** methodology + numbers in `docs/25-benchmarks.md` (hardware noted,
commands reproducible via `bench/`), with hit and miss latency distributions
plotted separately, and the adversarial corpus **committed** under `bench/` so
that "zero false allows" is a claim someone else can re-run rather than a thing
you remember being true. Where CPython cannot reach a target, the **gap and its
cause** — GIL contention on HMAC, JSON parsing, allocation, GC pauses, or a
blocking call on the loop — is the finding, and it is written down rather than
rounded away.

## Suggested order of attack
1. **Verify exactly one signature.** One user, one hard-coded key,
   `GetCallerIdentity`: get a `botocore`-signed request to verify byte-for-byte
   before anything else in the project exists (V1).
2. Add the **document model and the matcher** against a single identity policy —
   one statement, one action, one resource, `Allow` (V2).
3. Turn a statement match into a **decision**: explicit deny, implicit deny, and
   the deciding statement carried in the result (V2 → V3).
4. Add **resource policies** and the same-account / cross-account asymmetry. Write
   the truth table first, then make it pass — this is the one place where writing
   the test first genuinely changes the design (V3).
5. Add **boundaries, SCPs and session policies** on top. The five-layer chain is
   much easier once the two-party case is solid (V3).
6. Add **`AssumeRole`**: trust policy, temporary credentials, the session token,
   expiry — then re-sign with the result and watch the identity change (V4).
7. **Point project 24 (or 23, or 06) at it** and make a real service's request get
   denied by a policy you wrote. This is the moment the whole tier connects, and
   it is worth doing before the optimization work rather than after.
8. Only *now* make it fast: compile policies, cache decisions, measure — then make
   the cache correct under invalidation, which is the harder half (V5).
9. Add **revocation, the audit trail and the simulator**; then benchmark,
   document, and tune (V6).

## Run it
```bash
make setup && make sync    # .env from .env.example, then the venv
make run                   # IAM/STS API on :9025, authorizer on :9026

# Unsigned — expect 403. Every request to :9025 must be SigV4-signed.
curl -i "localhost:9025/?Action=GetCallerIdentity&Version=2011-06-15"

# Signed with the bootstrap credentials, using botocore's own signer.
make whoami

# What the other projects call: a decision, and the statement that made it.
make authorize
```
