# Five Authorities, Any One of Which Can Say No

> Teaches why "does the policy allow it?" is the wrong question, why deny-by-default
> with no way to negate the default is the only composition rule that stays safe,
> and why cross-account access follows a *different* rule from same-account access —
> the single most misunderstood thing in AWS. No prior knowledge assumed.
>
> Prepares you for **V3** in [`evaluation.py`](../src/iam_sts/evaluation.py)
> (`PolicyEvaluator.evaluate`, `._evaluate_layer`, `._is_cross_account`). It builds
> on the matcher from [doc 01](./01-the-policy-language-and-its-traps.md) and the
> `Decision` / `PolicyType` enums in [`models.py`](../src/iam_sts/models.py).

---

## The one sentence to hold onto

**Authorization is the *intersection* of independent authorities, never the union —
so the composition rule has to be one that stays safe when the policies were written
by people who never met.**

---

## 1. The problem before the solution

You have a matcher. Given one statement and one request it says match or no match,
`Allow` or `Deny`. Surely a decision is now: *walk the user's policies, return
whether any statement allowed it?*

Try that against four situations that all really happen.

| Situation | What "any statement allowed it" produces | What should happen |
|---|---|---|
| Your admin granted `s3:*`. The company forbids S3 in this region entirely. | allow | deny — the company outranks your admin |
| You have `s3:GetObject`. The bucket belongs to another company. | allow | deny — they never agreed |
| You have `Allow *`, but you're a contractor with a boundary capping you at read-only | allow | deny — the cap is the point |
| You assumed a role for one task and accepted a narrowing session policy | allow | deny — you agreed to be narrowed |

Every row fails the same way, and it is not a matcher bug. **The model is wrong.**
The question is not "did the principal's own permissions allow it". It is: *did
every party with standing to object decline to object, and did at least one party
with standing to grant actually grant?*

Those parties are different people with different authority over you, and they do
not coordinate. That is the entire design problem.

---

## 2. The five authorities

```
┌─────────────────────────────────────────────────────────────┐
│ SCP (Service Control Policy)                                │
│   Written by: the organization. Above your admin.           │
│   Can: take away.   Cannot: give.                           │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Permission boundary                                     │ │
│ │   Written by: your admin, about you.                    │ │
│ │   Can: cap.   Cannot: grant.                            │ │
│ │ ┌─────────────────────────────────────────────────────┐ │ │
│ │ │ Session policy                                      │ │ │
│ │ │   Written by: whoever called AssumeRole.            │ │ │
│ │ │   Can: narrow further.   Cannot: widen.             │ │ │
│ │ │ ┌─────────────────────────────────────────────────┐ │ │ │
│ │ │ │ Identity policy      ← what you were given      │ │ │ │
│ │ │ │ Resource policy      ← what the owner offered   │ │ │ │
│ │ │ └─────────────────────────────────────────────────┘ │ │ │
│ │ └─────────────────────────────────────────────────────┘ │ │
│ └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

These are the six values of `PolicyType` in [`models.py`](../src/iam_sts/models.py)
(the sixth, `TRUST`, is a resource policy on a role, evaluated at `AssumeRole` time
in [doc 03](./03-self-describing-credentials.md), not on the request path).

The vital distinction, and the one that makes the whole model coherent:

> **Only identity and resource policies can *grant*. The other three can only
> *subtract*.**

An SCP that "allows" `s3:*` gives nobody anything. It merely fails to take S3 away.
A principal holding a permission boundary and no identity policy can do **nothing
at all** — the boundary is a ceiling with no floor under it.

That asymmetry is why the model composes safely across organizational boundaries.
The company can constrain your admin without knowing who your admin is. Your admin
can constrain you without knowing what you'll be granted. Neither has to trust the
other, because neither can be *widened* by the other.

`PolicySet` in [`evaluation.py`](../src/iam_sts/evaluation.py) is exactly these
inputs as a value — assembled by the authorizer, handed here. That matters more than
it looks; see §7.

---

## 3. Deny wins, and the default is deny

Two rules, and it is worth seeing why *both* are needed.

### Rule 1: an explicit `Deny` anywhere ends it

Not "within its layer". Anywhere, at any layer, immediately, regardless of how many
`Allow`s exist.

Why? Because a deny is the only tool for expressing "no, really, never" — and if a
deny could be outvoted, it would express nothing. Any grant anywhere would defeat
it, and grants are exactly what proliferate in a large organization. **A deny that
can be outvoted is not a deny.**

This produces a useful asymmetry inside a layer:

```
walking one layer's statements:
   hit an Allow  →  record it, KEEP WALKING     (a later Deny still wins)
   hit a Deny    →  stop, you have the answer   (nothing can change it)
```

You may short-circuit on the first `Deny`. You may **not** short-circuit on the
first `Allow`. That shape is the whole model in two lines, and it is exactly what
`_evaluate_layer` has to implement.

### Rule 2: silence means no

No matching statement anywhere → deny. And critically, **there is no way to write a
policy that flips the default.** No `"DefaultEffect": "Allow"`, no root-level
escape.

Why is that non-negotiable? Because the default is what applies to everything
nobody thought about — every service that shipped last week, every resource created
this morning, every action nobody's threat model included. Defaulting to deny means
**the set of things you have not considered is safe by construction.** Any language
that lets you flip it has a single line of config that voids every other line.

### Three-valued, not two

Hence `Decision` in [`models.py`](../src/iam_sts/models.py) has three values:

| Value | Meaning | The fix |
|---|---|---|
| `ALLOW` | something granted, nothing objected | — |
| `EXPLICIT_DENY` | a statement said `Deny` | **find that statement and change it** |
| `IMPLICIT_DENY` | nothing matched | **the `Allow` you expected is missing or doesn't match** |

Those two denials are the same outcome and completely different bugs. Collapsing
them into `False` is the single most common way an authorization system becomes
undebuggable: the operator stares at a policy that plainly grants the action and has
no way to learn that a boundary three levels up denied it.

And a layer needs three states too — hence `LayerVerdict.is_silent` in
[`evaluation.py`](../src/iam_sts/evaluation.py). A layer that says *nothing*
composes differently from one that says *no*: a silent SCP blocks, a silent resource
policy does not (in the same-account case). Collapsing silence into a boolean is
exactly how those two get confused.

---

## 4. The cross-account asymmetry

This is the part worth slowing down for. It is the concept the vertical exists to
teach.

```
same-account:    identity policy  OR   resource policy       ← either suffices
cross-account:   identity policy  AND  resource policy       ← both, independently
```

Most people learn the OR, meet the AND during an outage, and conclude AWS is being
arbitrary. It is the opposite of arbitrary. **It falls directly out of who owns
what.**

### Same account — one authority, two voices

Both policies were written under a single administrative authority. The account
owns the principal *and* the resource. When the identity policy says "Alice may
read this bucket", that is the account speaking. When the bucket policy says "Alice
may read me", that is the same account speaking. Requiring both would mean requiring
one authority to say the same thing twice, which adds no safety and enormous
friction.

So: **either voice suffices.**

### Cross account — two authorities, neither speaking for the other

```
   Account A (111122223333)              Account B (999988887777)
   ┌──────────────────────┐              ┌──────────────────────┐
   │  Alice               │              │  the bucket          │
   │  identity policy:    │  ──request→  │  resource policy:    │
   │  "Alice may read     │              │  "A's Alice may      │
   │   B's bucket"        │              │   read me"           │
   └──────────────────────┘              └──────────────────────┘
        A's admin wrote this                  B's admin wrote this
```

Now consider each half alone.

**If A's identity policy alone sufficed:** A's admin could write `Allow s3:* on
arn:aws:s3:::b-bucket` and read B's data. Any account could grant itself access to
any other account's resources by writing a policy in its own account. The account
boundary would mean nothing.

**If B's resource policy alone sufficed:** B could write a policy naming Alice, and
Alice would gain a permission her own admin never granted and cannot see. Every
account in the world could hand your employees permissions behind your back — and a
confused or compromised employee now carries authority your organization never
issued.

**Both directions are unacceptable, so both are required.** The AND is not a
restriction bolted on; it is the *definition* of a trust boundary. Cross-account
access exists only where two independent authorities agreed, separately, in their
own accounts, each visible to their own auditors.

### The truth table

The SPEC asks for a matrix test over {same, cross} × {identity} × {resource}. Here
is its shape — deriving each cell from the two rules above is the exercise:

| identity | resource | same-account | cross-account |
|---|---|---|---|
| `Allow` | `Allow` | allow | allow |
| `Allow` | silent | allow | **deny** (implicit) |
| silent | `Allow` | allow | **deny** (implicit) |
| silent | silent | deny (implicit) | deny (implicit) |
| `Deny` | `Allow` | deny (explicit) | deny (explicit) |
| `Allow` | `Deny` | deny (explicit) | deny (explicit) |

Rows 2 and 3 are the whole lesson: **identical policies, opposite outcomes, decided
purely by whether two account ids match.**

Write this table first, then make it pass. The SPEC's "Suggested order of attack"
singles this out as the one place where writing the test first genuinely changes the
design — and it is right. Discovering the asymmetry after you have a working
evaluator means restructuring it.

### Which makes `_is_cross_account` load-bearing

It is one comparison and it decides between OR and AND — the most consequential
input to the whole decision. And it is trickier than `!=`:

- A **service principal** (`lambda.amazonaws.com`) has no account at all.
- An **assumed role**'s account is the *role's*, not the assumer's — which is
  exactly how cross-account access is usually done, and means the answer changes
  after an `AssumeRole`.
- The **resource owner** is not always the account field of its ARN. Some services
  leave it empty. This is why `PolicySet.resource_owner_account` is a required
  field rather than something inferred, and why `AuthorizeRequestBody` in
  [`routes.py`](../src/iam_sts/routes.py) makes the *calling service* supply it —
  it knows for certain; you would have to guess.

Every one of those cases decides whether the rule is OR or AND. Decide each, and
record it.

---

## 5. Every decision carries its reason

`AuthorizationResult` in [`models.py`](../src/iam_sts/models.py) carries
`deciding_policy_type`, `deciding_policy_id`, `deciding_statement_id` and `reason`.
Populate them on **every** path — allows exactly as much as denies.

Why allows matter as much:

- **The audit trail.** [Doc 05](./05-revocation-and-the-audit-trail.md) records why
  each decision went the way it did. "Allowed" with no reason tells a post-incident
  auditor nothing about *which* of forty statements opened the door.
- **The simulator's parity criterion.** V6's simulator must return the same decision
  *and the same deciding statement*. It cannot reproduce what you never produced.
- **Deny-reason aggregation.** The SPEC's observability section wants a deploy that
  breaks a trust policy to look *different on a graph* from one that breaks a
  condition key. That needs a bounded set of reason phrases — which is why `reason`
  is documented as "safe to aggregate as a metric label": keep it a fixed vocabulary,
  not an interpolated string. An interpolated reason is an unbounded-cardinality
  metric label, and that takes down your metrics backend rather than your service.

---

## 6. Total and fail-closed

> Any error inside evaluation — a bad policy, a missing context key, an unexpected
> exception — produces a **deny with a reason**. Never an allow, never a 500.

The SPEC makes this a criterion rather than a note because the natural shape of this
function is a chain of early returns, and that shape has an equally natural bug: an
exception in layer three skips layers four and five and lands somewhere optimistic.

Consider what each failure mode means if unhandled:

| Failure | Unhandled result | Correct result |
|---|---|---|
| Policy doesn't parse | exception → 500 → caller retries or fails open | deny, reason: malformed |
| Context key missing | `KeyError` mid-layer | deny, or the operator's documented absent-key rule |
| A layer raises | remaining layers skipped | deny, reason: evaluation error |
| Evaluation times out | caller's own timeout fires | deny |

There is a general rule underneath, and it is worth stating: **under partial
failure, an authorization service must get slower or stricter — never more
permissive.** Every other service in the tier can degrade by shedding load. This one
degrades by denying. A 500 from here is ambiguous to the caller, and a caller that
resolves ambiguity in your favour is a caller who just failed open on your behalf.

Fault injection at each layer is the proof, not an argument from reading the code.

---

## 7. Why the evaluator is pure

`PolicyEvaluator` does no I/O, holds no clock, touches no store. Everything arrives
in the `PolicySet`. That constraint is doing three jobs at once:

1. **V5 can cache the result safely.** Every input is visible in the arguments, so
   the cache key can cover all of them. A function that secretly reads a clock or a
   store cannot be cached correctly — you would be caching a decision that depended
   on something the key never saw.
2. **V6's simulator reuses this exact code.** A simulator that reimplements
   evaluation is a simulator that will drift, silently, and the day it matters is the
   day someone trusts it about production. Look at `build_state` in
   [`main.py`](../src/iam_sts/main.py): one `PolicyEvaluator` is constructed and
   handed to *both* the `Authorizer` and the `PolicySimulator`. That shared
   reference is what makes the parity criterion achievable rather than aspirational.
3. **Testing needs no fixtures.** Construct a `PolicySet`, call, assert. The
   six-row truth table in §4 becomes six lines.

This is a pattern worth taking with you: **push I/O to the edges, keep the judgement
pure.** The cache bug and the policy bug then live in different modules and cannot
be mistaken for each other.

---

## 8. Mental model

| Idea | Why |
|---|---|
| Only identity & resource policies grant | Lets independent authorities constrain without trusting each other |
| Deny wins everywhere, immediately | A deny that can be outvoted expresses nothing |
| Default is deny, unflippable | Everything nobody thought about is safe by construction |
| Implicit ≠ explicit deny | Same outcome, opposite fix |
| Silent ≠ deny, per layer | Silent SCP blocks; silent resource policy doesn't (same-account) |
| Same-account OR | One authority, two voices |
| Cross-account AND | Two authorities, neither speaks for the other |
| Reason on every path | Audit, simulator parity, deny-reason graphs |
| Fail closed, totally | Degrade slower or stricter, never more permissive |
| Pure evaluator | Cacheable, simulatable, testable |

---

## Where you'll build this

[`src/iam_sts/evaluation.py`](../src/iam_sts/evaluation.py) — three
`NotImplementedError`s: `PolicyEvaluator.evaluate`, `._evaluate_layer`,
`._is_cross_account`.

**Build it in the SPEC's order, not all at once.** Steps 3–5 of "Suggested order of
attack" stage it deliberately: first turn a statement match into a *decision* with
explicit/implicit deny and a deciding statement; then add resource policies and the
same/cross-account asymmetry (truth table first); only then layer on boundaries,
SCPs and session policies. The five-layer chain is much easier once the two-party
case is solid, and much harder if you attempt it first.

This doc unlocks the V3 criteria about deny precedence, implicit vs explicit deny,
the cross-account asymmetry, boundaries and SCPs capping without granting, session
policies only narrowing, deciding statements on every path, and fail-closed
totality.

What it does not hand you: the actual layer ordering (a workable one is sketched in
the scaffold's TODO — understanding *why* that order, and defending yours in
`docs/25-design.md`, is the graded part), and the three `_is_cross_account` edge
cases from §4, which are yours to decide and record. `/hint` for nudges, `/quest`
to build it as a session.

Next: [self-describing credentials](./03-self-describing-credentials.md) — now make
the principal itself temporary.
