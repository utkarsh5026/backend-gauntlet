# Proving You Hold a Secret Without Ever Sending It

> Teaches what a message authentication code actually buys you, why AWS derives a
> *new* key for every date/region/service instead of signing with your secret, and
> why the boring-looking "canonicalization" step is where every afternoon goes. No
> prior knowledge assumed — not of HMAC, not of SigV4, not of AWS.
>
> Prepares you for **V1** in [`sigv4.py`](../src/iam_sts/sigv4.py)
> (`SigV4Verifier.canonical_request`, `.string_to_sign`, `.derive_signing_key`,
> `.verify`, plus `SigningKeyCache` and `ReplayGuard`). The types it hands back
> live in [`models.py`](../src/iam_sts/models.py).

---

## The one sentence to hold onto

**A signature is not the secret and it is not encryption — it is evidence that
whoever produced it knew the secret *and* was talking about exactly this request,
and nothing else.**

Every design choice below falls out of trying to make "exactly this request" mean
something two independent programs can agree on, byte for byte.

---

## 1. The problem before the solution

You have a secret. So does the server. You want the server to believe a request
came from you. The obvious moves all fail, and it is worth watching them fail
because each failure names a property you actually need.

### Attempt 1: send the secret

```
POST / HTTP/1.1
X-Secret: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

Now the secret exists in: your process, the TLS terminator, the load balancer's
access log, the reverse proxy's debug dump, the APM trace, and the browser
extension your intern installed. TLS protects it *in transit between two hops* and
does nothing about the seven places it gets written down at either end.

More fundamentally: **you gave away the thing itself in order to prove you had
it.** Anyone who sees one request can make every future request.

### Attempt 2: send a hash of the secret

```
X-Proof: sha256(secret) = 9f86d081884c7d65...
```

Better — the secret is not literally there. But that hash is a *constant*. It is
now the password. Anyone who captures one request replays it forever, and can also
issue completely different requests with it. We removed the word "secret" and kept
every problem.

### Attempt 3: hash the secret together with the request

```
X-Proof: sha256(secret + request_bytes)
```

Now the proof is different per request, and you cannot produce it without the
secret. This is genuinely close, and it is where most hand-rolled schemes stop.

It has a specific, famous flaw. SHA-256 is built from a loop that absorbs the
message block by block and carries the running state forward; the digest **is**
that internal state. So an attacker who holds `sha256(secret + msg)` and knows
`len(secret)` can load that state back into the function and keep hashing —
producing a valid `sha256(secret + msg + padding + attacker_suffix)` for a message
they never had the secret to sign. That is a **length-extension attack**, and it
is why the construction below is `hmac`, not "hash with the key glued on".

### What we actually need

| Requirement | Why the naive attempts fail it |
|---|---|
| The secret never transmitted | Attempt 1 sends it |
| The proof differs per request | Attempt 2 is a constant |
| Not forgeable from one capture | Attempt 3 is length-extendable |
| Not replayable later | All three: a captured proof works forever |
| Not reusable elsewhere | All three: a proof for one service works against another |
| Both sides compute the same bytes | Nobody has said what `request_bytes` *means* yet |

The last two rows are the interesting ones, and they are what SigV4 spends its
complexity on.

---

## 2. HMAC — the primitive, in one paragraph

**HMAC** is a specific way of combining a key and a message so that the
length-extension trick does not work. It hashes twice, with the key mixed in
differently each time:

```
HMAC(key, msg) = H( key⊕opad ‖ H( key⊕ipad ‖ msg ) )
```

You do not need to remember the padding constants. You need two facts:

1. **It is keyed.** Without the key you cannot produce the output, and seeing
   outputs does not reveal the key.
2. **The output is final.** Because the outer hash consumes the inner digest, an
   attacker holding the result cannot extend it — they would need to run the outer
   hash, which needs the key.

In Python this is `hmac.new(key, msg, hashlib.sha256)`. Reach for that and never
for `hashlib.sha256(key + msg)`. The difference is a real attack that has been the
answer to this exact question since 2009.

---

## 3. Deriving a key by scope — one secret becomes a fleet of narrow ones

Here is the move that makes SigV4 more than "HMAC the request".

Instead of signing with your secret, you first **derive** a signing key by chaining
four HMACs, each one narrowing the scope:

```
kDate    = HMAC("AWS4" + secret, "20260824")       ← valid only on this date
kRegion  = HMAC(kDate,           "us-east-1")      ← ...only in this region
kService = HMAC(kRegion,         "sts")            ← ...only for this service
kSigning = HMAC(kService,        "aws4_request")   ← ...only for this scheme
```

Real values, computed with the well-known AWS example secret
`wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY`:

| Step | Result (hex) |
|---|---|
| `kDate` | `6bae6fed3480d7e7075cb8f779107692b98fc79bc49720d5dc8cf2b93beb8609` |
| `kRegion` | `08b0a4bace572af12d8c90b77bf8f371620bf1f04a81881ebec00e337517a23c` |
| `kService` | `9395d2c5b752cf8e5b62681ab37914dab41f73a93aff298572896c6b2400b7c4` |
| `kSigning` | `0f0579cd5979867eaf2d8afdf6b7e9b65bad6b617581d34ad1a211b265eea934` |

**Why bother?** Because each link is one-way. Given `kService`, you can compute
`kSigning`, but you cannot walk back to `kRegion`, and certainly not to the secret.

That turns one long-lived credential into a tree of narrow, self-expiring ones:

```
             secret  (lives for years)
                │
      ┌─────────┴─────────┐
   kDate(Aug 24)      kDate(Aug 25)        ← each dead the next day
      │
   ┌──┴──┐
us-east-1  eu-west-1                       ← each useless in the other region
   │
 ┌─┴─┐
sts   s3                                   ← each useless against the other
```

An attacker who somehow extracts `kSigning` out of a server's memory gets the
ability to sign **`sts` requests, in `us-east-1`, on 2026-08-24, and nothing else.**
Tomorrow it is scrap. Compare that to leaking the secret, which is unbounded in
every dimension at once.

The four HMACs cost almost nothing — they are over inputs of ~10 bytes — which is
why it is affordable to do this per request rather than inventing a key-management
system to avoid it.

> **The `"AWS4"` prefix is a domain separator.** It ensures a signing key can never
> collide with an HMAC someone computes with the raw secret for an unrelated
> purpose. Prefixing a constant before hashing, so that two different uses of one
> key can never produce the same bytes, is a pattern you will see everywhere once
> you start looking.

The `CredentialScope` dataclass in [`sigv4.py`](../src/iam_sts/sigv4.py) is exactly
this tuple — `date/region/service/aws4_request` — and it is frozen and hashable so
it can key `SigningKeyCache` directly. That is not incidental: the derived key is a
*pure function* of (secret, scope), which is what makes caching it safe at all.

---

## 4. Canonicalization — the part that will consume your afternoon

We still have not said what `request_bytes` means. This is the whole ballgame.

The client and the server never exchange the thing being signed. Each one
independently renders the HTTP request into a byte string and hashes it. **If the
two renderings differ by a single space, the signatures differ completely, and the
only feedback anyone gets is `SignatureDoesNotMatch`.** No detail. No hint about
which of the six lines disagreed.

The obvious approach — "hash the raw bytes of the request" — does not survive
contact with reality. Between the client and you sit proxies, load balancers and
frameworks that legitimately reorder headers, re-case them, re-encode the path, add
`Via` and `X-Forwarded-For`, and change `Connection`. A signature over raw bytes
would break on every one of those.

So instead both sides agree on a **canonical form**: a normalization that throws
away everything a proxy is allowed to change and keeps everything that carries
meaning.

### A real one, end to end

Here is an actual request, signed by `botocore` — the same library the SPEC
requires you to verify against. Timestamp pinned to `20260824T120000Z` so you can
reproduce it exactly.

```
POST / HTTP/1.1
Host: localhost:9025
Content-Type: application/x-www-form-urlencoded; charset=utf-8
X-Amz-Date: 20260824T120000Z

Action=GetCallerIdentity&Version=2011-06-15
```

`botocore` renders that into this canonical request — six parts, newline-separated:

```
POST                                                    ← 1. HTTP method
/                                                       ← 2. canonical URI
                                                        ← 3. canonical query (empty)
content-type:application/x-www-form-urlencoded; charset=utf-8
host:localhost:9025                                     ← 4. canonical headers
x-amz-date:20260824T120000Z
                                                        ←    (blank line ends them)
content-type;host;x-amz-date                            ← 5. signed header names
ab821ae955788b0e33ebd34c208442ccfc2d406e2edc5e7a39bd6458fbb4f843   ← 6. SHA-256 of body
```

Read line 4 carefully. Header names are **lowercased**, sorted alphabetically, and
joined to their values with `:` and no space — while the *value* keeps its internal
`; charset=utf-8` spacing exactly. Line 5 repeats just the names, semicolon-joined,
in the same order. Line 6 is the hex SHA-256 of the raw body bytes.

That whole blob is then hashed, and the hash goes into a four-line **string to
sign**:

```
AWS4-HMAC-SHA256
20260824T120000Z
20260824/us-east-1/sts/aws4_request
8cfed82f59176253a9a899e1161822a005ce6eaa3a2e4d5251d7ba4690e558d5
```

Finally:

```
signature = HMAC(kSigning, string_to_sign)
          = 2fe7db72b24666e23b7e2f99dafe48857151ac162bdebdbdd06092a0ed5357b9
```

That value is `botocore`'s. It is also what you get by hand-chaining the four
HMACs from §3 and running one more over those four lines — the two agree, which is
the whole point of the exercise.

> **Why the extra indirection?** The signature is over a *hash of* the canonical
> request, not the canonical request itself. That keeps the signed document a fixed
> four lines whether the body is empty or 5 MB — and it is precisely why the payload
> hash has to be its own line back in the canonical request.

### The empty-body constant

A request with no body still has a payload hash: the SHA-256 of zero bytes,

```
e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

You will see this constant in every AWS trace you ever read. Recognizing it on
sight saves real debugging time — it means "GET with no body", not "something went
wrong".

### Where the bugs actually live

| Rule | The plausible-looking bug |
|---|---|
| Header names lowercased and sorted | Sorting *before* lowercasing — `X-Amz-Date` sorts before `content-type`, but `x-amz-date` sorts after |
| Header values trimmed, internal runs collapsed | Collapsing whitespace *inside a quoted string*, where it is significant |
| Query params URI-encoded first, *then* sorted | Sorting the raw names, so `%2F` and `/` land in different places |
| Repeated query names | Forgetting to sort the values too |
| Only `SignedHeaders` participate, in that order | Including every header you received, so an added `X-Forwarded-For` breaks it |
| Path normalization | Normalizing when the service does not (S3 signs the raw path; most others sign the normalized one) |

**The debugging technique that works:** when a signature mismatches, print your
canonical request and `botocore`'s and diff them character by character. Do not
squint at the hex — the digests are designed to tell you nothing. Nearly every
mismatch is whitespace or an encoding of `/`.

This is also why the SPEC's very first criterion is that a **real SDK's** signature
verifies unmodified. Writing your own signer and checking it against your own
verifier proves only that you agree with yourself.

---

## 5. Scope narrowing, demonstrated

The claim in §3 was that a signature is useless outside its scope. Here it is,
measured — same request, same secret, same timestamp, only the scope changed:

| Scope | Signature |
|---|---|
| `sts` / `us-east-1` (the real one) | `2fe7db72b24666e23b7e2f99dafe4885…` |
| `iam` / `us-east-1` | `16730e4e9067b5cc545e44e1ff660f5a…` |
| `sts` / `eu-west-1` | `deac0e6e227bbd0b4a751a1bae918dc6…` |

Completely unrelated outputs. That is the avalanche property of a good MAC: one bit
of input change scrambles every output bit, so there is no partial credit and no
way to adapt a signature from one scope to another.

The same table is your **tamper test**. Change one byte of the body —
`Version=2011-06-15` → `2011-06-16` — and the signature moves from `2fe7db72…` to
`5d7f5d8f1e389941d15c6a781f527871…`. The SPEC asks you to prove this field by
field, for every signed field, which is a table-driven test and not eight separate
ones.

---

## 6. What a signature still cannot tell you

A valid signature proves *someone with the key produced this exact request*. It
does **not** prove:

### …that the request is fresh

SigV4 is deterministic. The same request signed at the same second produces the
same bytes, forever. Capture one off the wire — from a log aggregator, a proxy, a
crash dump — and replay it verbatim. If it was `DeleteBucket`, once was enough.

Two mechanisms, and you need both:

- **A clock-skew window.** The signed timestamp must be within N seconds of now.
  This is what `sigv4_clock_skew_seconds` (default `300.0` in
  [`config.py`](../src/iam_sts/config.py)) exists for. It bounds the replay
  opportunity to a few minutes rather than forever.
- **A replay guard.** Remember signatures you have already accepted and refuse a
  repeat. `ReplayGuard` in [`sigv4.py`](../src/iam_sts/sigv4.py) is where this
  goes.

The elegant part is how those two interact: **the skew window bounds how much the
replay guard has to remember.** A signature older than the window is already
refused for being stale, so the guard never needs more than the last N seconds of
signatures. "Remember everything forever" becomes a fixed-size problem — the only
reason it is affordable at all.

The design questions the SPEC wants written down are the ones the scaffold's TODOs
name: eviction must be bounded work per call (a periodic full scan at 20k req/s is
a periodic latency spike shaped exactly like a GC pause), and this is per-process
state, so across a fleet it is either shared — a round trip on the hot path — or
per-node, meaning a replay works once per node. There is no free option. Pick, and
record what it costs.

### …that the caller is the caller

A signature proves possession of a key. Whether that key still *should* work is a
completely different question, answered by revocation — and revocation is
[doc 05](./05-revocation-and-the-audit-trail.md)'s problem.

### …that the signed request is the request you received

This is the subtle one, and it is a real vulnerability rather than a rough edge.

`SignedHeaders` is chosen by the **client**, and the client may be hostile. A
request can carry a syntactically perfect signature over a set of headers that
happens to exclude the ones that carry meaning — the security token, the target
action, the `Host`. Everything covered matches; everything that matters was left
out and can be modified freely in flight.

So "the signature verified" is only as strong as "the signature covered the fields
I care about." Deciding the minimum required set, and enforcing it, is yours.

---

## 7. Comparison is part of the crypto

You have recomputed the expected signature. You compare it to the one that arrived.

```python
if computed == provided:      # ← a security bug
```

Python's `==` on strings short-circuits at the first differing byte. It returns
faster for a wrong answer that shares a longer prefix. That difference is small,
but it is *systematic*, and an attacker who can measure it recovers the correct
signature one byte at a time: try all 16 values for position 0, keep the slowest,
move to position 1. Sixty-four positions × 16 tries is a thousand requests, not a
brute force over 2²⁵⁶.

`hmac.compare_digest` compares every byte regardless and returns in time
independent of where the first difference falls. Use it for signatures, session
token MACs, external ids — anything where being *close* must not be observably
different from being wrong.

The same reasoning drives a rule that looks like unhelpful error messages: unknown
key id, inactive key and revoked key must all raise `InvalidClientTokenId` with the
*same* message. Distinguishing them builds an oracle that tells an attacker which
key ids exist. See the error taxonomy in [`errors.py`](../src/iam_sts/errors.py) —
that collapsing is deliberate.

---

## 8. Order of operations, and why it is not arbitrary

The `verify` TODO in [`sigv4.py`](../src/iam_sts/sigv4.py) sketches an order.
Worth understanding *why* it is that order, because each step's position is an
argument:

| Step | Why here |
|---|---|
| Parse the `Authorization` header | Cheapest, and everything else needs its fields. Runs pre-authentication on behalf of anyone at all — so bound the work |
| Check the skew window | Before any crypto. A stale request is refused regardless of signature, and HMAC is the expensive part |
| Resolve key id → secret | Needed to derive. Unknown/inactive/revoked collapse to one error |
| Recompute and compare | `compare_digest`, never `==` |
| Consult the replay guard | **After** verification. Recording an *unverified* signature lets anyone poison the guard with garbage |

That last row is the one people get backwards, and it converts a defence into a
denial-of-service surface.

---

## 9. Two envelopes, one document

The same signed document arrives two ways, and both are graded:

| | Header form | Presigned URL |
|---|---|---|
| Where the signature rides | `Authorization:` header | `X-Amz-Signature` query param |
| Other fields | header params | `X-Amz-Algorithm`, `X-Amz-Credential`, `X-Amz-Date`, `X-Amz-SignedHeaders` |
| Validity window | the skew window (~5 min) | `X-Amz-Expires` seconds from `X-Amz-Date` |
| Typical lifetime | minutes | hours to days |
| Excluded from the canonical query string | n/a | those six `X-Amz-*` params |

A presigned URL is a **bearer credential shaped like a link**. It gets pasted into
chat, saved in browser history, and forwarded. `presign_max_expiry_seconds`
(default `604800.0` — seven days) is therefore a security control wearing a config
field's clothes, not a tuning knob.

---

## 10. Mental model

| Idea | What it buys | What it costs |
|---|---|---|
| HMAC over the request | Proof of possession without transmission | Both sides must render identical bytes |
| Key derived by date/region/service | A leaked signing key is narrow and self-expiring | Four extra HMACs per request (negligible) |
| Canonicalization | Survives proxies that legitimately rewrite requests | Every normalization rule is a place to disagree with real SDKs |
| Sign a *hash* of the canonical request | Signed document is 4 lines regardless of body size | Payload hash needs its own line |
| Clock-skew window | Bounds replay opportunity; bounds guard memory | Clients need roughly-correct clocks |
| Replay guard | Kills replay inside the window | Per-process state, or a hot-path round trip |
| `compare_digest` | No timing oracle | None worth mentioning — just use it |

---

## Where you'll build this

[`src/iam_sts/sigv4.py`](../src/iam_sts/sigv4.py) — eight `NotImplementedError`s:
`SigningKeyCache.get_or_derive`, `ReplayGuard.check_and_record`,
`SigV4Verifier.parse_authorization`, `.canonical_request`, `.string_to_sign`,
`.derive_signing_key`, `.verify`, `.verify_presigned`.

Start where the SPEC's "Suggested order of attack" says: **verify exactly one
signature.** One user, one hard-coded key, one `GetCallerIdentity`, signed by
`botocore`, verified byte-for-byte — before anything else in the project exists.
`make whoami` in [`makefile.py`](../makefile.py) is wired to do exactly that.

This doc unlocks the V1 criteria about the derivation chain being scoped, tamper
detection per signed field, constant-time comparison, the skew window and the
replay mechanism. The two it deliberately does **not** hand you are the ones the
SPEC is actually grading: your canonicalization has to match a real SDK's, and the
replay guard's eviction and fleet-wide story are decisions with costs you have to
name. For graduated nudges on those, `/hint`; to build it as a guided session,
`/quest`.

Next: [the policy language](./01-the-policy-language-and-its-traps.md) — you know
*who* is asking; now you need a language for what they may do.
