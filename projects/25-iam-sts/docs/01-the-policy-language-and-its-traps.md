# A Policy Is Not Configuration — It Is a Language

> Teaches why JSON permissions are a programming language with a compiler you have
> to write, where its evaluation rules violate intuition, and why almost every trap
> in it fails in the *generous* direction. No prior knowledge assumed — not of IAM,
> not of ARNs, not of glob matching.
>
> Prepares you for **V2** in [`policy.py`](../src/iam_sts/policy.py) (`parse_arn`,
> `matches_action`, `matches_arn`, `interpolate_variables`, `ConditionEvaluator`,
> `parse_policy`, `match_statement`). The `Arn` and `AuthorizationRequest` types it
> operates on live in [`models.py`](../src/iam_sts/models.py).

---

## The one sentence to hold onto

**Every ambiguity in a policy language resolves toward granting more access than
the author meant, because the author is describing a set and sets are easier to
widen than to narrow.**

Hold that, and each trap below stops being trivia and becomes an instance of one
pattern.

---

## 1. The problem before the solution

Here is a policy. It is four lines and it is doing four dangerous things at once.

```json
{
  "Effect": "Allow",
  "Action": "s3:Get*",
  "Resource": "arn:aws:s3:::bucket/*",
  "Condition": {"StringEquals": {"aws:PrincipalTag/team": "${aws:username}"}}
}
```

Read it the way its author did: *"let people read things in this bucket, if they're
on the matching team."* Now read it the way an evaluator does:

| Fragment | What the author meant | What it actually says |
|---|---|---|
| `s3:Get*` | "read operations" | every current and future action starting `Get` — `GetBucketPolicy`, `GetBucketAcl` |
| `arn:aws:s3:::bucket/*` | "the objects in there" | every object that will *ever* exist there, including ones a future feature creates |
| `StringEquals` on a tag | "must match" | if the tag is **absent from the request**, this fails — but its negated twin would *pass* |
| `${aws:username}` | "the caller's name" | attacker-influenced text spliced into the middle of a pattern |

Nothing here is malformed. It parses, it stores, it evaluates, and it grants more
than intended in at least two directions. There is no error to grep for — which is
the recurring theme of this entire project.

**The move that makes this tractable** is the one the module boundary already
makes: V2 owns *one statement against one request*. Composing many policies from
several authorities is V3's problem. Keeping them apart is the only reason either
is testable.

---

## 2. ARNs are structured, and string-matching them is a vulnerability

An ARN looks like a string. It is a six-field record with a fixed layout:

```
arn : aws : s3 : us-east-1 : 111122223333 : bucket/key.txt
 │     │     │       │            │              │
 │     │     │       │            │              └─ resource (may contain : and /)
 │     │     │       │            └──────────────── account id
 │     │     │       └───────────────────────────── region
 │     │     └───────────────────────────────────── service
 │     └─────────────────────────────────────────── partition (aws, aws-cn, aws-us-gov)
 └───────────────────────────────────────────────── literal "arn"
```

The `Arn` dataclass in [`models.py`](../src/iam_sts/models.py) is exactly these
fields, frozen and hashable so it can sit in a decision cache key without being
re-serialized on every lookup.

Note what it deliberately does *not* do: split `resource` into a type and an id.
The separator differs by service — `:` for some, `/` for others, both for a few —
so splitting it in the shared vocabulary would bake one service's convention into
every other. That split is per-service, and it is part of your job.

### Why flat string matching breaks

The tempting implementation is: flatten both sides to strings, run a glob. Here is
what that actually does, measured with Python's `fnmatch`:

| Pattern | Target | Flat glob says |
|---|---|---|
| `arn:aws:s3:::my-bucket*` | `arn:aws:s3:::my-bucket-public` | ✅ match — *correct, and surprising* |
| `arn:aws:s3*` | `arn:aws:s3-outposts:us-east-1:111122223333:outpost/op-1/bucket/x` | ✅ match — **a different service entirely** |
| `arn:aws:iam::*:role/Deploy` | `arn:aws:iam::111122223333:role/Deploy` | ✅ match — intended |
| `arn:aws:iam::*:role/Deploy` | `arn:aws:iam::111122223333:x:role/Deploy` | ✅ match — **the `*` slid across a `:`** |

Row 2 and row 4 are the vulnerability. In row 4 the `*` was written to mean "any
account id" — one field. Flattened, it happily swallowed a `:` and an extra segment
along with it. The pattern's author constrained six fields; the matcher enforced
four.

**Segment-wise matching removes this class by construction.** Match field against
field, with the wildcard confined inside a field, and no pattern can ever reach
across a separator — not because you remembered to test it, but because there is
no code path that could.

That is the difference between "we tested for that" and "that cannot happen", and
it is worth reaching for the second whenever it is available.

The other half is **parsing**. `parse_arn` is the boundary between "a string an
attacker sent" and "a structured thing the matcher trusts". Split with a bounded
`maxsplit` so the resource keeps its own colons; reject fewer than six parts rather
than padding them out; reject an account that is neither digits nor empty. Padding
a short ARN is how a crafted fragment lands its resource name in the account field.

---

## 3. Wildcards: bounded cost is a criterion, not a nicety

Two `*` characters and a hostile input can cost you a CPU core.

Here is the naive recursive glob everyone writes first — on a `*`, either consume
it or consume one input character and try again:

```python
def naive(p, s):
    if not p:  return not s
    if p[0] == '*':
        return naive(p[1:], s) or (bool(s) and naive(p, s[1:]))
    return bool(s) and (p[0] in ('?', s[0])) and naive(p[1:], s[1:])
```

Correct. Now feed it a pattern that cannot match, so it must exhaust every
possibility. Measured, on the pattern `('*a' × n) + 'b'` against `n×2` letters `a`:

| n | Pattern length | Input length | Time |
|---|---|---|---|
| 6 | 13 | 12 | 1.39 ms |
| 8 | 17 | 16 | 21.56 ms |
| 10 | 21 | 20 | 396.73 ms |
| 12 | 25 | 24 | **6227.52 ms** |

Roughly ×16 per two characters. A **24-character resource name** — shorter than a
real S3 key — takes six seconds. At that point one request occupies a core, and a
handful of them occupy your service. That is the SPEC's bounded-cost criterion, and
it is a timing test on adversarial input rather than a code review.

Two escape hatches that are not:

**Don't reach for `re`.** Compiling a user-supplied policy string into a regex is
ReDoS with extra steps — you have handed the attacker a regex engine.

**Don't reach for `fnmatch` without reading it.** It supports `[seq]` character
classes you never intended to offer. Measured:

```
pattern 's3:Get[abc]'  vs action 's3:Get[abc]'  →  False   ← the literal doesn't match itself
pattern 's3:Get[abc]'  vs action 's3:Geta'      →  True    ← it became a wildcard
```

A policy author who writes a literal `[` gets matching semantics they did not ask
for, in both directions. The honest option is a small explicit matcher over `*` and
`?` only — one you can reason about and bound.

### Case sensitivity is not uniform

The rule that catches everyone: **action matching is case-insensitive; resource
matching generally is not.** Treating them the same is a genuine vulnerability in
one direction (`S3:GETOBJECT` sailing past a deny written in lowercase) and a
baffling non-match in the other.

The SPEC asks you to decide this *per segment*, test it, and record which is which
in `docs/25-design.md`. That is a real research task, not a lookup — service
documentation is the source, and the answer is not uniform even across AWS.

---

## 4. `Not*` — the inversion nobody reads correctly

Every list field in `Statement` ([`policy.py`](../src/iam_sts/policy.py)) has a
`Not` twin: `not_actions`, `not_resources`, `not_principals`.

They are not sugar. Consider:

```json
{"Effect": "Allow", "NotAction": ["iam:*"], "Resource": "*"}
```

The author reads: *"don't allow IAM."* The evaluator reads: **"allow every action
in AWS that is not IAM."** Including:

- every service that existed when this was written,
- every service AWS shipped since,
- every service AWS ships next year, automatically.

```
        the universe of all actions
  ┌────────────────────────────────────────┐
  │                                        │
  │   ┌────────┐                           │
  │   │ iam:*  │      ← everything outside │
  │   └────────┘        this box is granted│
  │                     including things   │
  │                     invented in 2029   │
  └────────────────────────────────────────┘
```

`Allow` + `NotAction` is a **grant over an open-ended set**. `Deny` + `NotAction`
is different again and equally counterintuitive: *deny everything except IAM*.

The SPEC asks for one test whose entire purpose is to demonstrate `NotAction`
granting far more than its author intended. Write that test with a rude name. It is
the kind of thing that stops a code review dead, which is the point.

One structural rule worth enforcing early: a field and its `Not` twin must never
both be set. That is malformed, not merely unusual, and `parse_policy` should refuse
it rather than silently picking one.

---

## 5. Conditions — three levels, three different combinators

The `Condition` block is three levels deep, and each level composes **differently**:

```
Condition ──► operator ──► key ──► [values]
              ▲            ▲        ▲
              AND          AND      OR
```

- Every **operator** block must pass.
- Within an operator, every **key** must pass.
- Within a key, **any one value** passing is enough.

Getting one of those three backwards produces a policy that is *mostly* right —
which is worse than one that is obviously wrong, because it survives review.

```json
"Condition": {
  "StringEquals":   {"aws:PrincipalTag/team": ["platform", "infra"]},
  "IpAddress":      {"aws:SourceIp": "10.0.0.0/8"}
}
```

Reads as: *(team is platform **OR** infra) **AND** source IP is in 10/8.*

### The absent-key trap

This is the single highest-value thing in this doc.

A condition key is a fact about the request. The request may simply **not contain
it** — no source IP recorded, no tag set, no MFA claim. What happens then depends
on the operator, and the pattern is not guessable:

| Situation | Result | Consequence |
|---|---|---|
| `StringEquals` on an absent key | fails | statement doesn't match — safe |
| `StringNotEquals` on an absent key | **passes** | statement matches *vacuously* |
| `...IfExists` suffix | passes when absent | explicit, intentional, reviewable |

Row 2 is where accidental grants come from. Think about it as a set operation and
it stops being arbitrary: "is the team not `finance`?" — with no team at all, the
answer is honestly *yes*. Vacuous truth is correct logic and a security problem.

Now put that in a policy:

```json
{"Effect": "Allow", "Action": "s3:*", "Resource": "*",
 "Condition": {"StringNotEquals": {"aws:PrincipalTag/quarantine": "true"}}}
```

Intended: *everyone except quarantined principals.* Actual: everyone except
principals who have the tag **and** whose tag says `true`. A principal with **no
tags at all** — a brand new role, a service principal, anything not yet enrolled in
your tagging discipline — sails straight through.

The `...IfExists` suffix exists precisely so that "pass when absent" is something
you *wrote down*, rather than something you inherited from set theory.

### Set operators over multi-valued keys

Keys can be multi-valued — `aws:PrincipalTag/team` with several tags,
`aws:TagKeys`. That is why `ContextValue` in [`models.py`](../src/iam_sts/models.py)
is `str | list[str]` rather than `str`: a single `str` would quietly make these
operators untestable.

- `ForAllValues:` — *every* value in the request key must be in the policy's list.
- `ForAnyValue:` — *at least one* must be.

And the trap, again vacuous: **`ForAllValues:` over an empty or absent key is
true.** "All zero of my values are in your allowed set" is impossible to falsify.
Another test worth a rude name.

### Types, and failing closed

`StringEquals` on a numeric key. A date operator on something unparseable. A
CIDR operator on `not-an-ip`. Each of these must be a **deny** — never a crash
(that's a 500 on the hot path) and never a coerced comparison that happens to
succeed (that's an accidental allow).

### Return the consulted keys

`ConditionEvaluator.evaluate` returns `(satisfied, consulted_keys)` rather than a
bare bool, and `StatementMatch` carries `consulted_keys` through. That is not
bookkeeping — it is a **V5 requirement arriving early**.

The decision cache key must cover every input the decision depended on. If a policy
conditions on `aws:SourceIp` and your cache key is principal+action+resource, then
the first request from one IP poisons the answer for every other IP. That is a
security bug that presents as "works on my machine".

Reconstructing which keys were consulted later means reimplementing this dispatch.
Return them now, including for statements that did **not** match — a key consulted
by a non-matching statement still influenced the decision. It is *why* it did not
match.

---

## 6. Policy variables — interpolation into a pattern

```json
"Resource": "arn:aws:s3:::home/${aws:username}/*"
```

One policy, per-user scoping, no per-user policies. Genuinely elegant. And it
splices request-derived text into the middle of a matching pattern, which should
make you sit up.

**The failure mode is failing open.** If `${aws:PrincipalTag/team}` is missing and
you substitute an empty string:

```
"arn:aws:s3:::home/${aws:PrincipalTag/team}/*"   →   "arn:aws:s3:::home//*"
```

Depending on your matcher, that now matches everything under `home/`. A missing tag
just widened a grant.

Raising, or returning a pattern that provably matches nothing, are both defensible.
Substituting nothing is not. That is the `interpolate_variables` criterion:
**fail closed on an unresolvable variable.**

Also handle the escapes — `${*}`, `${?}`, `${$}` exist so a policy can contain a
literal `*` that is not a wildcard. Without them there is no way to write one.

> **A version trap worth knowing.** `"Version": "2012-10-17"` supports variables.
> `"2008-10-17"` is still accepted by the real service and silently does **not**
> interpolate — it treats `${aws:username}` as a literal string. So an old version
> string turns a variable into a constant, and a policy that looks scoped to one
> user matches nobody, or everybody, depending on where the variable sat. That is
> why `POLICY_VERSION` is pinned in [`policy.py`](../src/iam_sts/policy.py) and why
> accepting an unknown version silently changes what a policy *means*.

---

## 7. Validate at write time, not at evaluation time

`parse_policy` raises `MalformedPolicyDocument` / `LimitExceeded` rather than
storing anything questionable. The limits live in
[`config.py`](../src/iam_sts/config.py):

| Setting | Default | Guards against |
|---|---|---|
| `max_policy_bytes` | 6144 | one enormous document |
| `max_statements_per_policy` | 100 | linear evaluation cost per request |
| `max_policies_per_principal` | 10 | many documents multiplying that cost |
| `max_condition_keys_per_statement` | 32 | condition-block blowup |

Plus: nesting depth. `json.loads` on deeply nested input is a stack overflow
waiting to be someone's denial of service.

**Why write time specifically?** A policy that only fails when evaluated fails on
the hot path — where the only safe answer is to deny. And a whole account failing
closed at once looks exactly like an outage, arriving at the least convenient
moment. Write time is when there is a human present, an error message they will
read, and no blast radius beyond their own request.

This is a general principle worth naming: **push validation to the boundary where
failure is cheap.** The control plane has a human and no SLA. The data plane has
neither.

---

## 8. Mental model

| Trap | Direction it fails | Defence |
|---|---|---|
| Flat-string ARN matching | Generous — `*` crosses `:` | Segment-wise matching, structural parse |
| Naive backtracking glob | Availability — exponential | Bounded matcher + adversarial timing test |
| `fnmatch`'s `[seq]` | Both — unrequested semantics | Explicit `*`/`?`-only matcher |
| `NotAction` | Generous — open-ended set | A test that demonstrates it, named loudly |
| Absent key + negated operator | Generous — vacuous pass | Pin every negated operator; use `...IfExists` |
| `ForAllValues:` on empty | Generous — vacuous pass | Test the empty case explicitly |
| Unresolvable variable | Generous — `//*` | Fail closed; never substitute empty |
| `"Version": "2008-10-17"` | Either — variable becomes literal | Reject unknown versions |
| Late validation | Availability — deny storm | Validate at write time |

Notice the middle column. Seven of nine fail toward *more access*. That is the one
sentence from the top, cashed out.

---

## Where you'll build this

[`src/iam_sts/policy.py`](../src/iam_sts/policy.py) — seven
`NotImplementedError`s: `parse_arn`, `matches_action`, `matches_arn`,
`interpolate_variables`, `ConditionEvaluator.evaluate`, `parse_policy`,
`match_statement`.

**The highest-value first move is a table-driven test.** `match_statement` is a
pure function of `(statement, request) → match`, which means the entire vertical
can be pinned by a list of tuples. Write that list *before* the matcher, and encode
every trap in this doc into it — `NotAction`, the absent-key cases, the ARN
confusables, the empty `ForAllValues`. You cannot accidentally write a matcher that
passes them; you have to actually mean it.

Then a property test over `parse_arn` — hypothesis is already a dev dependency in
[`pyproject.toml`](../pyproject.toml) — asserting no segment can escape into the
next. And the bounded-cost timing test from §3.

This doc unlocks the V2 criteria about structural ARN parsing, `Not*` semantics,
the condition operator families, absent-key behaviour, variable interpolation and
write-time validation. What it deliberately does not decide for you: the per-segment
case-sensitivity rules (research, then record), and which matching semantics you
implement versus deliberately skip — the SPEC grades that you wrote down *both*.
`/hint` for nudges, `/quest` to build it as a session.

Next: [composing five authorities](./02-composing-independent-authorities.md) —
one statement matching is not a decision.
