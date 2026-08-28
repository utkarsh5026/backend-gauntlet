# Credentials That Expire, for a Principal That Does Not Exist

> Teaches what a bearer token actually is, why putting the identity *inside* a
> tamper-evident bundle removes a database lookup from every request in the company,
> what you trade away to get that, and why `ExternalId` exists. No prior knowledge
> assumed — not of STS, not of MACs (see [doc 00](./00-proving-possession-without-transmission.md)),
> not of the confused deputy problem.
>
> Prepares you for **V4** in [`sts.py`](../src/iam_sts/sts.py)
> (`SessionTokenCodec.encode` / `.decode`, `SecurityTokenService.assume_role`,
> `.resolve`, `.caller_identity`, `.reap_expired`). It feeds the `Identity` type
> that [doc 00](./00-proving-possession-without-transmission.md)'s verifier returns.

---

## The one sentence to hold onto

**If a credential carries its own identity in a form the verifier can check without
asking anyone, then verification costs no round trip — and revocation costs you a
mechanism you have to build on purpose.**

That sentence is the whole vertical, including the bill that comes due in
[doc 05](./05-revocation-and-the-audit-trail.md).

---

## 1. The problem before the solution

Alice needs to run a deployment script on a build box. The script needs permission
to write to S3.

**The obvious answer:** create a user for the build box, generate an access key, put
it in the box's config.

Watch what that key becomes over the next two years:

| Time | What happened |
|---|---|
| Day 0 | Key created, scoped to what the script needs |
| Month 2 | Script needs one more permission. Granted. Nobody removes anything |
| Month 5 | A second script reuses "the build box key" — easier than a new one |
| Month 9 | Key is in a Dockerfile, which is in a git history, which is public for four hours |
| Month 14 | Alice leaves. The key is not hers, so offboarding does not touch it |
| Month 20 | Nobody can say what breaks if it is rotated, so nobody rotates it |
| Month 24 | The key has 40 permissions, unknown holders, and no expiry |

Nothing here was a mistake exactly. Each step was locally reasonable. **The failure
is structural: a credential with no expiry accumulates permissions and holders
monotonically, because nothing ever forces a review.**

Now list the properties we actually want:

| Want | Because |
|---|---|
| Credentials that expire on their own | Leakage becomes bounded rather than permanent |
| Permissions attached to a *job*, not a person | Survives offboarding, reviewable in isolation |
| A record of who is behind each use | Audit needs the human, not the shared key |
| No secret sitting in a config file at all | The file is the leak |
| Verification without a lookup | Every request in the company waits on it |

`AssumeRole` is the primitive that gets all five.

---

## 2. The shape of the answer: roles

Split the two things a "user with a key" conflates:

```
   a ROLE                                  a PRINCIPAL
   ─────────                               ───────────
   a set of permissions                    someone who authenticates
   + a trust policy saying who             holds a credential
     may briefly become it
                     ▲                              │
                     └───────  AssumeRole  ─────────┘
```

A role is permissions **nobody holds**. It has no credentials. It is a costume, and
the trust policy is the list of who may wear it.

`AssumeRole` hands back a *triple*, not a pair:

```
AccessKeyId      ASIA5EXAMPLE...      ← note ASIA, not AKIA
SecretAccessKey  <ephemeral>
SessionToken     <the interesting part>
```

> **`ASIA` vs `AKIA` is not decoration.** The prefix — `STS_TEMPORARY_KEY_PREFIX`
> in [`sts.py`](../src/iam_sts/sts.py) — is how a human reading a log, and a secret
> scanner reading a repository, can tell at a glance whether a leaked credential
> expires on its own. Making a security-relevant property visible in the *shape* of
> an identifier is a cheap trick that pays out every time someone greps.

The first two slot straight into [doc 00](./00-proving-possession-without-transmission.md)'s
SigV4 flow. The third is where the design lives.

---

## 3. The session token, and the lookup that isn't there

Here is the fork in the road. The verifier receives `ASIA5EXAMPLE...` and must
answer *whose is this, and what secret verifies its signature?*

### Design A: the token is a database handle

```
token = "sess_8f3a91c2"          ← an opaque row id

verify:  SELECT * FROM sessions WHERE id = 'sess_8f3a91c2'
```

| | |
|---|---|
| ✅ | Revocation is trivial — `DELETE`, and the next request fails |
| ✅ | Small token |
| ❌ | **A database round trip on every request in the company** |
| ❌ | That database is now the availability floor for every service in the fleet |
| ❌ | It must be replicated everywhere requests are verified, consistently |

### Design B: the token *is* the record

```
token = base64( {session_id, role_arn, session_name, assumed_by,
                 issued_at, expires_at, session_policy, tags, ...}
                ‖ MAC(key, payload) )

verify:  check the MAC. Read the fields. Done. No lookup at all.
```

| | |
|---|---|
| ✅ | **Zero round trips.** Verification is one HMAC over a few hundred bytes |
| ✅ | Any node in any region can verify, immediately, with no replication lag |
| ✅ | No shared datastore in the hot path — nothing to be the availability floor |
| ❌ | Bigger token (it carries the record) |
| ❌ | **Revocation is now a problem you must solve on purpose** |

Design B is what real STS does and what the SPEC asks for. This is the concept:

> **You bought latency with statelessness, and paid for it in revocation.**

Say that out loud, because it is the kind of trade that is invisible in the code and
obvious in the postmortem. `_sessions` exists in `SecurityTokenService` — but read
its comment: authentication does **not** consult it. It is there for observability
and for V6. A session missing from that dict is not thereby revoked; a session
present in it is not thereby valid.

---

## 4. Integrity, confidentiality, and which one is required

Two properties. They are not the same, and only one is negotiable.

**Integrity is required.** A holder must not be able to edit their own role, expiry,
session policy or tags. Without integrity the token is a *suggestion*: flip
`"role": "Reader"` to `"role": "Admin"` and hand it back.

The mechanism is a MAC — `hmac` with `compare_digest`. And here the length-extension
point from [doc 00](./00-proving-possession-without-transmission.md) becomes load
bearing rather than academic: `sha256(key + payload)` lets an attacker *append* to
your payload. If your parser takes the last occurrence of a repeated key, or your
format tolerates trailing data, appending `,"role":"Admin"` is a valid, correctly-
MAC'd token. Use `hmac`. Or an AEAD from `cryptography`. Not a hand-rolled
construction.

**Confidentiality is a decision.** Real STS tokens are opaque and appear encrypted.
You could equally ship a signed-but-readable payload:

| | Signed + readable | Encrypted |
|---|---|---|
| Debugging | trivial — decode and look | needs the key and a tool |
| Leaks to the holder | session policy, tags, role arn | nothing |
| Complexity | one MAC | key management, nonces, AEAD |

Note the middle row's punchline: the holder is usually the principal the token
*describes*, so "leaking" it to them may be leaking nothing at all. Decide, and
record it in `docs/25-design.md` — the SPEC grades the token format and what
protects it.

### Two fields to include before you think you need them

The scaffold TODO names both, and both are the kind of thing that is free now and
expensive later:

- **A key id.** The horizontal checklist requires key rotation with two keys
  accepted during an overlap window. That is impossible if the token does not say
  which key signed it. Adding the field later means invalidating every live session
  at once.
- **The issue time**, not just the expiry. V6 revokes "every session for this role
  issued before T" — the mechanism real STS exposes as *Revoke sessions*. It needs
  `issued_at` inside the token. Same story: add it now or take an outage later.

There is a general lesson here about versioned formats: **the fields you cannot add
later are the ones that identify the format itself.** A key id and a version tag
cost two bytes and buy you every future migration.

### Verify before you parse

The `decode` TODO makes a point that is easy to get backwards. It is tempting to
deserialize first — you need the key id to pick the key, after all. But everything
past the key id must be treated as hostile until the MAC checks out. Parse only the
*framing*, verify, then parse the payload. Otherwise your JSON parser is running on
attacker-controlled bytes with no authentication in front of it.

And raise the **same** error for a bad MAC and a malformed token. Distinguishing
them tells an attacker when they have the framing right, which is exactly the
foothold they were looking for.

---

## 5. The trust policy — a resource policy on a role

Who may assume a role? A policy that lives *on the role* and names principals. That
is a resource policy by any other name, which is why `PolicyType.TRUST` exists in
[`models.py`](../src/iam_sts/models.py) as a distinct value: same machinery,
evaluated at `AssumeRole` time rather than on the request path.

```json
{
  "Effect": "Allow",
  "Principal": {"AWS": "arn:aws:iam::111122223333:root"},
  "Action": "sts:AssumeRole",
  "Condition": {"StringEquals": {"sts:ExternalId": "a1b2c3d4-secret"}}
}
```

**Do not write a second, simpler matcher for this.** Evaluate the caller against it
with V3's machinery. The reason is right there in the `Condition` block: a trust
policy with conditions is exactly where `ExternalId` is enforced, and a simplified
matcher that skips conditions silently skips the protection in §6.

### Two failures that must stay distinguishable

| Failure | Meaning | Fix |
|---|---|---|
| The trust policy does not name you | The role's owner never invited you | edit the **role's** trust policy |
| You lack `sts:AssumeRole` | Your own admin didn't grant the action | edit **your** identity policy |

Same symptom, opposite fixes, different people. Collapsing them into "AccessDenied"
sends someone to the wrong team for an afternoon. (Contrast with
[doc 00](./00-proving-possession-without-transmission.md) §7, where unknown/inactive/
revoked keys are *deliberately* collapsed — because there, distinguishing them
builds an oracle for an unauthenticated attacker. Here both parties are
authenticated and the distinction leaks nothing. The rule is not "always collapse"
or "always distinguish"; it is *who learns what, and were they already entitled to
know it.*)

> **A role trusting `{"AWS": "*"}` is a public front door into your account.** It is
> one copy-pasted example away at all times, and it looks like a placeholder rather
> than an incident.

---

## 6. `ExternalId` and the confused deputy

The confused deputy is the boss fight's namesake, so it is worth getting exactly
right. **The deputy is not an attacker.** It is an honest service that holds
authority on someone else's behalf and is tricked into using it for the wrong
someone.

### The setup

A monitoring vendor needs read access to your AWS account. You create a role trusting
the vendor's account, and give the vendor its ARN. The vendor does this for a
thousand customers.

```
   Vendor account (the deputy)
   ┌────────────────────────────────┐
   │ holds AssumeRole authority     │
   │ into 1000 customer accounts    │
   └───────┬───────────────┬────────┘
           │               │
     ┌─────▼─────┐   ┌─────▼─────┐
     │ Customer A│   │ Customer B│
     │ role/Mon  │   │ role/Mon  │
     └───────────┘   └───────────┘
```

### The attack

Customer B — a legitimate paying customer, with a normal account on the vendor's
dashboard — types **Customer A's role ARN** into the "your role ARN" field.

The vendor's software does what it always does: assumes the configured role, reads
data, displays it on B's dashboard. Every component behaved correctly. The vendor's
credentials were genuinely the vendor's. A's trust policy genuinely trusts the
vendor. And B is now reading A's data.

The trust policy cannot help. It says *"the vendor may assume me"* — and the vendor
did.

### The fix

Add a shared secret that only **you** and **the vendor** know, required as a
condition on the assume:

```json
"Condition": {"StringEquals": {"sts:ExternalId": "a1b2c3d4-secret"}}
```

A's `ExternalId` is a value the vendor associated with **A's account** in its own
records. When B supplies A's role ARN, the vendor sends *B's* external id, and A's
trust policy refuses.

The elegance is in who holds what: **the customer writes the condition, in their own
account, protecting themselves.** They do not have to trust the vendor's input
validation. That is why the SPEC insists `ExternalId` comes out of policy evaluation
as a condition on `sts:ExternalId` rather than a separate `if` in your assume-role
code — making it a *condition* is what lets the customer write the policy that
protects them.

The generalization is worth carrying: **when a deputy holds authority for many
principals, the request must carry proof of *which* principal it is acting for — and
that proof must be verifiable by the party being protected, not by the deputy.**
The same shape appears in CSRF tokens and in OAuth's `state` parameter.

The SPEC asks for a test whose docstring names this attack. Write the docstring
first.

---

## 7. Chaining, and why it truncates

An assumed role can assume another role. That is **role chaining**, and it is how
access legitimately crosses several accounts. It has two hard bounds, and they are
different rules — which is why [`config.py`](../src/iam_sts/config.py) keeps them as
separate settings rather than one `min()`:

| Setting | Default | Rule |
|---|---|---|
| `max_session_duration_seconds` | 43200 (12 h) | ceiling on a direct assume |
| `chained_session_max_duration_seconds` | 3600 (1 h) | **hard cap once you are already a session** |
| `max_role_chain_depth` | 2 | how many hops at all |

**The truncation surprises everyone.** You ask for 12 hours, you are already in a
session, you silently get 1 hour. People discover this at hour two of a long job,
when the credentials expire mid-run and the error names something unrelated.

Why cap it? Each hop is a place where a session policy could have narrowed you and
did not, and where the audit trail gets one link longer. A chain is a
credential-laundering path through however many accounts trust each other
transitively — and transitive trust is not a thing anyone designed, it is a thing
that emerges. Bounding depth and duration keeps that emergent graph shallow.

### Tags, and what survives a hop

Sessions carry tags — inputs to the policy variables and conditions from
[doc 01](./01-the-policy-language-and-its-traps.md). On a chained assume:

```
   session 1                        session 2 (chained)
   tags: {team: platform,           tags: {team: platform}
          scratch: yes}                    ▲
   transitive: (team,)                     └─ scratch was dropped
```

**Only transitive tags survive.** Non-transitive ones are dropped at the next hop.
That is what stops a tag — and therefore a permission that conditions on it — from
silently propagating down a chain nobody audited. A tag that grants access should
have to be re-asserted deliberately at each hop, or it becomes an ambient property
of a credential three accounts away from where anyone is looking.

---

## 8. `GetCallerIdentity` — three strings that all feel like "me"

The call that ends most IAM debugging sessions, because the answer is frequently not
the one the caller expected.

After Alice assumes `DeployRole` with session name `ci-run-4471`, there are three
plausible answers:

| String | What it is |
|---|---|
| `arn:aws:iam::111122223333:user/alice` | who Alice is |
| `arn:aws:iam::111122223333:role/DeployRole` | the role she assumed |
| `arn:aws:sts::111122223333:assumed-role/DeployRole/ci-run-4471` | **what she is right now** |

The third is correct, and it is the one that appears in the audit trail. That is
precisely why people cannot find their own requests: they search for `user/alice`
and get nothing, because every request was made by
`assumed-role/DeployRole/ci-run-4471`.

Hence `PrincipalType.ASSUMED_ROLE` is a distinct value from `ROLE` in
[`models.py`](../src/iam_sts/models.py), and `Principal.session_name` is set only
for it. The role is the thing you configure; the assumed role is the thing that
shows up in the log with a session name attached.

And note what `AssumeRole` does to your permissions: it **replaces** them, it does
not add to them. Alice-as-DeployRole cannot do the things Alice could do. The SPEC
asks for a test proving the original caller's own permissions are *gone* — a
surprisingly easy criterion to fail with an implementation that merges instead of
swaps.

---

## 9. Expiry is enforced by the token, not by a table

`reap_expired` sweeps expired sessions out of `_sessions`. Its docstring is emphatic
about what it is not, and the distinction is worth internalizing:

> Housekeeping only. A session missing from the table is not thereby revoked, and a
> session present in it is not thereby valid.

Expiry is enforced by the token's **own claim**, checked against the clock on every
`resolve`. The reaper only stops the dict from growing. Confusing bookkeeping with
enforcement is how a background sweep becomes a security control by accident — and
then someone tunes its interval for performance and quietly widens a window nobody
knew was security-relevant.

The clock source deserves a line in `docs/25-design.md`. `SignedRequest.received_at`
in [`sigv4.py`](../src/iam_sts/sigv4.py) is wall clock on purpose — the client's
timestamp is wall clock, so the comparison must happen in that domain. Same
reasoning here.

---

## 10. Mental model

| Idea | Buys | Costs |
|---|---|---|
| Roles: permissions nobody holds | Survives offboarding; reviewable in isolation | An extra call before real work |
| Self-describing token | Zero-lookup verification, any node, any region | Revocation becomes a designed mechanism |
| MAC over the payload | Holder cannot edit their own identity | Key management + rotation |
| Key id inside the token | Rotation with an overlap window | Two bytes, if you add it now |
| Issue time inside the token | Per-role "revoke sessions before T" | Two bytes, if you add it now |
| Trust policy = resource policy on a role | Reuses V3; conditions work; `ExternalId` works | Must not be a second simpler matcher |
| `ExternalId` | Kills the confused deputy | The customer must set and keep a secret |
| Chain truncation & depth cap | Bounds emergent transitive trust | Surprises people at hour two |
| Transitive tags only | Grants can't ride a chain silently | One more field to reason about |

---

## Where you'll build this

[`src/iam_sts/sts.py`](../src/iam_sts/sts.py) — six `NotImplementedError`s:
`SessionTokenCodec.encode` / `.decode`, `SecurityTokenService.assume_role`,
`.resolve`, `.caller_identity`, `.reap_expired`.

The join with V1 is `resolve`, and it is worth seeing why the seam is where it is.
`SigV4Verifier` takes its secret lookup as an injected function rather than reaching
for a store. For a long-lived `AKIA` key that resolution is a store lookup; for an
`ASIA` key it is **a decode of the token the client already sent** — no store, no
round trip. Keeping it a function is what lets V4 plug in without V1 learning
anything about sessions.

The SPEC's step 6 is the moment to aim for: assume a role, re-sign with the returned
credentials, and watch the identity *change*. An end-to-end test that signs with a
user's key, assumes, re-signs, and gets a **different authorization result** proves
V1 and V4 fit together in a way neither vertical's own tests can.

This doc unlocks the V4 criteria about trust-policy enforcement, token integrity,
expiry, chaining caps, `ExternalId`, transitive tags and `GetCallerIdentity`'s
assumed-role ARN. It deliberately does not choose your token format, your
confidentiality stance, or where the temporary secret comes from — those are the
graded decisions, recorded in `docs/25-design.md`. `/hint` for nudges, `/quest` to
build it as a session.

Next: [caching a security boundary](./04-caching-a-security-boundary.md) — now make
all of this fast, without making it wrong.
