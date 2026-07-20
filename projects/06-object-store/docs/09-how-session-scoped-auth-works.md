# How Session-Scoped Auth Works — From First Principles

> A beginner-friendly, ground-up guide to what *session-scoped authentication*
> actually is in an object store, why Amazon built it for **S3 Express One Zone**,
> and what "amortize auth over many requests" means when you already have
> per-request signing. No prior knowledge of SigV4, IAM, or Express One Zone
> assumed.
>
> **Naming first:** "Express" here is **Amazon S3 Express One Zone** (a
> low-latency S3 storage class). It is **not** the Express.js web framework.
>
> This teaches the **concept**. It will **not** implement the graded auth box
> in `[SPEC.md](../SPEC.md)`, fill any `todo!()`, or write middleware for you.
> It explains the problem, the two-layer credential model, and what "correct"
> looks like so that when you build auth you know what you are aiming at.
>
> Anchored to: `[SPEC.md](../SPEC.md)` (Security + From the field),
> `[RESEARCH.md](../RESEARCH.md)` §Part 7, `[src/routes.rs](../src/routes.rs)`
> (open auth TODO), `[.env.example](../.env.example)`.

---

## 0. The one sentence to hold onto

**Pay the expensive identity check once (`CreateSession`); prove integrity
cheaply on every hot-path request with a short-lived session secret.**

Everything else — scopes, TTLs, refresh, "session tokens" — is machinery in
service of that one idea: *do not redo the hard auth work on every tiny
PUT/GET when the client is about to send ten thousand of them.*

---

## 1. Clarify the name: Express ≠ Express.js

| People say…              | They mean…                                                                 |
| ------------------------ | -------------------------------------------------------------------------- |
| "Express" (this doc)     | **Amazon S3 Express One Zone** — a single-AZ, low-latency S3 tier (2023).  |
| "Express" (Node world)   | The **Express.js** HTTP framework. Unrelated.                              |
| "Session" (this doc)     | A **short-lived credential** minted for auth amortization.                 |
| "Session" (multipart V4) | An **`uploadId` staging area** for assembling a big object. Different word, different thing. |

S3 Express One Zone exists because some workloads (ML training loops, analytics
hot paths, "diskless" brokers) need **single-digit-millisecond** first-byte
latency and enormous request rates. Co-locating storage in one AZ cuts network
hop cost. Session-based auth is the *auth* half of that latency story: at
100k+ requests/sec, verifying a long-lived identity the expensive way on every
request becomes a bottleneck even when the disk is fast.

Your From-the-field item names it directly:

> Session-scoped auth (the Express One Zone trick): a `CreateSession`-style
> endpoint mints a short-lived scoped token so the hot path skips per-request
> HMAC verification — auth cost is paid once per session, not per request
> — `[SPEC.md](../SPEC.md)`

Read that carefully against §0. The SPEC says "skips per-request HMAC" as a
*learning-store shorthand*. Real Express still **signs** every request; what it
skips is the **expensive identity path**. Section 5 spells out that distinction.
Both readings share the same insight: amortize the costly check.

---

## 2. The problem: why every request must be authenticated

An object store's write API is a firehose into your disk. Right now this project
still has an open graded box:

> Authenticate writes (and optionally reads). Real S3 uses **SigV4** request
> signing; a simplified access-key/HMAC scheme is a fair learning target — at
> minimum gate PUT/DELETE behind a credential. An open `PUT /{bucket}/{key}` is
> an open disk for the whole internet.
> — `[SPEC.md](../SPEC.md)`, Security / abuse protection

The code says the same thing next to the PUT path:

```199:200:projects/06-object-store/src/routes.rs
/// TODO(security): still unauthenticated — an open PUT is an open disk for the
/// whole internet. Gate writes behind a credential (SigV4, or a simpler HMAC).
```

Concrete failure modes if you leave it open:

| Attack / accident                         | What happens                                              |
| ----------------------------------------- | --------------------------------------------------------- |
| Stranger `PUT`s garbage into your buckets | Your disk fills; your data is overwritten or polluted.    |
| Stranger `DELETE`s keys                   | Objects vanish; dedup/refcount/GC cannot save intent.     |
| Open multipart initiate                   | Staging dirs leak forever — a cheap DoS.                  |
| Credentials appear in logs/errors         | The secret escapes; every past log is a breach.           |

So you need *some* credential check. Session-scoped auth is **not** a substitute
for that baseline. It is an optimization *on top of* it.

---

## 3. Per-request signing — the baseline mental model

Before Express, classic S3 (and every serious S3-compatible store) uses
**request signing**:

1. The client holds a long-lived **access key id** + **secret access key**.
2. For each request, the client computes a **signature** over canonical request
   bits (method, path, headers, payload hash, timestamp, …) using the secret —
   typically via **HMAC**.
3. The signature rides on the request (e.g. an `Authorization` header). The
   **secret itself never crosses the wire**.
4. The server, which also knows the secret (or can derive the signing key),
   recomputes the HMAC and accepts only if it matches — ideally with a
   **constant-time** compare so timing doesn't leak the secret.

Real AWS calls this **Signature Version 4 (SigV4)**. Your SPEC allows a
**simplified** access-key + HMAC scheme as a fair learning target
(`[.env.example](../.env.example)` already sketches `ACCESS_KEY_ID` /
`SECRET_ACCESS_KEY`). The exact wire format matters for AWS CLI interop; the
*idea* matters for the Express trick:

> Proving you know a secret ≠ sending the secret.

### What "expensive" means (two different costs)

| Cost                             | Where it shows up                                                                 | How big is it? |
| -------------------------------- | --------------------------------------------------------------------------------- | -------------- |
| **Local HMAC verify**            | Your single-process learning store, after you look up the key                     | Microseconds — usually fine even at high RPS |
| **Identity / policy resolution** | Real S3: map access key → principal → IAM policies → allow/deny for *this* action | Can dominate when you do it on every tiny request |

S3 Express was designed for the second world: at Express-scale request rates,
re-resolving "who is this and what may they do?" on every GET/PUT is too slow
and too chatty. Your learning store may only feel the *local* HMAC cost, but
the **architecture** you are studying amortizes the *identity* cost.

---

## 4. The Express insight: `CreateSession`

S3 Express One Zone adds a verb roughly shaped like:

**`CreateSession`** — client presents long-lived credentials (SigV4 as usual);
server returns **temporary session credentials** with a short lifetime and a
declared scope (read / write / read-write). The SDK caches them and **auto-
refreshes** on the order of minutes (~5 in the real product).

Then the hot path looks like normal signed requests — but the signing secret is
the **session** secret, not the long-lived one. Verifying that session is a
**local, cheap** check: is this session still valid, unexpired, and scoped for
this action?

```
┌──────────────┐     CreateSession          ┌──────────────┐
│   Client     │  (long-lived key / SigV4)  │ Object store │
│              │ ─────────────────────────► │              │
│              │ ◄───────────────────────── │              │
│              │  session key + secret      │              │
│              │  + scope + expiry          │              │
│              │                            │              │
│              │  PUT/GET/… × thousands     │              │
│              │  (signed with session      │              │
│              │   secret — cheap verify)   │              │
│              │ ─────────────────────────► │              │
│              │                            │              │
│   (TTL ends) │  CreateSession again       │              │
│              │ ─────────────────────────► │              │
└──────────────┘                            └──────────────┘
```

Or as a sequence:

```mermaid
sequenceDiagram
  participant Client
  participant Store
  Client->>Store: CreateSession with longLivedKey
  Store-->>Client: sessionAccessKey plus sessionSecret plus TTL
  loop Many hot path requests
    Client->>Store: PUT or GET signed with sessionSecret
    Store-->>Client: OK after cheap local verify
  end
  Note over Client,Store: Session expires; client refreshes
```

**The insight:** you did not remove authentication from the hot path. You
**moved the expensive check** to a rare control-plane call and left a cheap
integrity check on the data plane.

---

## 5. The two-layer credential model

Hold this table in your head:

| Layer | Credential                         | How often used              | What it proves                                      | Cost to verify                          |
| ----- | ---------------------------------- | --------------------------- | --------------------------------------------------- | --------------------------------------- |
| L1    | Long-lived access key + secret     | Rarely (`CreateSession`)    | "This principal exists and may open a session"      | Expensive (identity + policy in real S3) |
| L2    | Session access key + session secret| Every hot-path request      | "This request was signed by a live, scoped session" | Cheap (local HMAC + TTL/scope check)    |

### Learning-store simplifications (honest tradeoffs)

Your From-the-field item allows a simpler shape than full AWS Express:

| Approach                         | Hot path does…                              | Still teaches the Express idea? | Closest to real Express? |
| -------------------------------- | ------------------------------------------- | ------------------------------- | ------------------------ |
| **Temp key pair (recommended)**  | Sign with session secret (same HMAC code)   | Yes                             | Yes                      |
| Opaque bearer token + server map | Lookup token, check TTL/scope               | Partially (amortize L1)         | No — often skips signing |
| Self-contained signed JWT-ish    | Verify MAC on claims, check `exp`           | Partially                       | No                       |

The **temp key pair** path has the best learning reward: you keep per-request
integrity, and you still feel why `CreateSession` exists. Bearer tokens are
fine pedagogy for "sessions" in general web apps; they undersell the object-
store lesson if they make you think Express "turns off HMAC."

---

## 6. Scope, TTL, and refresh

A session that can do everything forever is just a second long-lived key with
extra steps. Three knobs make sessions worth minting:

### Scope

At minimum, Express-style scopes:

| Scope        | Typical allow set                          |
| ------------ | ------------------------------------------ |
| `Read`       | GET / HEAD / List                          |
| `Write`      | PUT / DELETE / multipart writes            |
| `ReadWrite`  | Both                                       |

Optional tighter binds (great blast-radius control): a specific **bucket** or
**key prefix**. A leaked `Read` session on `logs/` should not let someone
`DELETE` production objects in another bucket.

### TTL (time to live)

Sessions are **short-lived** (minutes, not months). Why:

| Property        | Why it matters                                              |
| --------------- | ----------------------------------------------------------- |
| Stolen session  | Dies on its own when `exp` passes.                          |
| Revocation      | Even without a denylist, damage window is bounded.          |
| Policy change   | Next `CreateSession` picks up new allow/deny; old sessions age out. |

### Refresh

Clients (and the AWS SDK) call `CreateSession` again before expiry. From the
server's point of view that is just another L1-authenticated mint. From the
client's point of view the app never sees 401 storms mid-workload — the SDK
rotates under the hood.

Never log session secrets, long-lived secrets, or full `Authorization` headers.
Treat them like passwords: they must not appear in tracing fields, error
bodies, or `/stats`.

---

## 7. What this is *not*

| Confused with…                         | Why it's different                                                                 |
| -------------------------------------- | ---------------------------------------------------------------------------------- |
| **Multipart `uploadId` sessions**      | Those stage *bytes* for one object assemble. Auth sessions stage *permission*. Same English word, unrelated protocols. See `[docs/01-how-multipart-uploads-work.md](01-how-multipart-uploads-work.md)`. |
| **"Skip auth on the hot path"**        | Real Express still authenticates each request; it skips the *expensive identity* path. |
| **Express.js middleware sessions**     | Cookie/`express-session` for browsers. Different problem domain.                   |
| **Presigned URLs**                     | One URL embeds a signature for a *specific* operation/time window. Sessions cover *many* operations until TTL. Related cousin, not the same trick. |
| **Replacing the graded auth box**      | You still need L1 (long-lived gate). Session auth is L2 on top.                    |

---

## 8. How this maps to *this* project

Two checklist rows, two jobs:

| Checklist item | Where | Job |
| -------------- | ----- | --- |
| **Authenticate writes** (graded, still open `[ ]`) | `[SPEC.md](../SPEC.md)` Security | Build **L1**: gate PUT/DELETE (and optionally reads) behind a credential — SigV4 or simplified HMAC. |
| **Session-scoped auth** (From the field, ungraded `[~]`) | `[SPEC.md](../SPEC.md)` From the field; `[RESEARCH.md](../RESEARCH.md)` §Part 7 | Build **L2**: `CreateSession`-style mint + hot path that trusts short-lived scoped session creds. |

Suggested learning order (conceptually — you write the code):

1. **Close L1 first.** Without long-lived auth, `CreateSession` has nothing trustworthy to mint from.
2. **Then add L2.** Same signing/verify machinery, new credential kind, TTL + scope.
3. **Prove the amortization story.** Many authenticated requests after one session mint; expired session rejected; wrong scope rejected; secrets never logged.

The horizontal Protocols section of the SPEC is already done; session auth is
not "the last protocol checkbox" in the graded tracker — it is the stretch item
that makes the *auth* story match modern S3 Express. The README's open work is
the graded HMAC/SigV4 gate; this doc is the mental model for the stretch that
sits on top.

---

## 9. Mental-model summary

| It looks like…                              | It actually is…                                                                 |
| ------------------------------------------- | ------------------------------------------------------------------------------- |
| "Express auth skips HMAC"                   | Expensive **identity** is amortized; cheap **integrity** still runs per request. |
| A session token is like an `uploadId`       | Auth session = permission lease. Multipart session = byte staging.              |
| One long-lived key on every request         | L1 rarely; L2 (short-lived) on the hot path.                                    |
| Auth is a boolean middleware flag           | Auth is **who** + **what scope** + **until when**.                              |
| Refresh is a client nicety                  | Refresh is how short TTL stays usable under sustained load.                     |
| Bearer token ≡ Express                      | Bearer is a simplification; temp signed session keys are the closer model.      |

---

## 10. Concepts to internalize

You own this topic when you can explain:

- [ ] Why an open `PUT /{bucket}/{key}` is an open disk, and why secrets must never appear in logs or error bodies.
- [ ] How request signing proves possession of a secret without sending the secret (HMAC / SigV4 at the idea level).
- [ ] The difference between **local HMAC cost** and **identity/policy resolution cost** — and which one Express was designed to amortize.
- [ ] The **two-layer** model: long-lived key → `CreateSession` → short-lived session secret on the hot path.
- [ ] Why scope (Read / Write / ReadWrite) and short TTL exist, and what refresh is for.
- [ ] Why multipart `uploadId` "sessions" and auth "sessions" are unrelated.
- [ ] How this project's **graded** auth box (L1) relates to the **From-the-field** Express item (L2) — L2 does not replace L1.

**Depth probes:**

- If your store is a single process and HMAC is microseconds, is session auth still worth building? What do you learn either way?
- How would you revoke a session *before* TTL without a denylist? What breaks if you add one?
- Could a session be bound to a single bucket prefix? What attacks does that stop?

**Trap:** implementing "session auth" as "first request checks the API key, later requests skip all checks." That is connection affinity theater, not Express. A stolen TCP connection or a second client must not inherit permission. Every request still needs a verifiable credential — just a cheaper one.

---

## 11. Where to look next

| Subtopic                                      | File / symbol |
| --------------------------------------------- | ------------- |
| Graded auth requirement                       | `[SPEC.md](../SPEC.md)` § Security / abuse protection |
| From-the-field Express item                   | `[SPEC.md](../SPEC.md)` § From the field → Session-scoped auth |
| Industry notes (Express One Zone, numbers)    | `[RESEARCH.md](../RESEARCH.md)` §Part 7 |
| Open PUT still unauthenticated                | `[src/routes.rs](../src/routes.rs)` TODO(security) |
| Env placeholders for access keys              | `[.env.example](../.env.example)` |
| Unrelated "session" (multipart)               | `[docs/01-how-multipart-uploads-work.md](01-how-multipart-uploads-work.md)` |
| Interop tests that currently skip SigV4       | `[tests/object_store_interop.rs](../tests/object_store_interop.rs)` |

When you are ready to implement: build L1 until writes reject without a valid
credential, then add a `CreateSession`-shaped mint and teach the hot path to
accept L2. This document's job is done when that plan feels obvious — not when
the code is written.
