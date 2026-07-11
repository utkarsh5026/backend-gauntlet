# How ETags Work — From First Principles

> A beginner-friendly, ground-up guide to the **ETag**: the one HTTP header that
> quietly does three unrelated jobs — proving an upload wasn't corrupted, letting
> a browser skip a download it already has, and stopping two writers from
> clobbering each other. No prior knowledge of HTTP caching, hashing, or S3
> assumed.
>
> This teaches the **concept** and shows it running over real HTTP. The multipart
> `-N` ETag formula gets a full worked derivation in
> `[01-how-multipart-uploads-work.md](01-how-multipart-uploads-work.md)`; this doc
> is the *why an ETag exists at all* half, and it complements that one rather than
> repeating it.
>
> Anchored to real code: `[src/object.rs](../src/object.rs)` (the `ETag` type),
> `[src/routes.rs](../src/routes.rs)` (where `If-None-Match`/`Range` hook in),
> `[SPEC.md](../SPEC.md)` (the horizontal "Conditional requests" box). Two runnable
> demos live in `[docs/examples/](examples/)` — you can watch every behavior in
> this doc happen on the wire.

---

## 0. The one sentence to hold onto

**An ETag is a *fingerprint of a resource's current bytes*: the server attaches it
to a response, and the rule is simply "if the bytes change, the fingerprint
changes; if the bytes are identical, the fingerprint is the same."** Everything
else — 304s, 412s, the cursed `-N` — is a use of that one property.

Because it's a fingerprint the client can compare for equality *without* holding
the bytes, it lets client and server answer three expensive questions cheaply:

| Question | Mechanism | Answer |
| --- | --- | --- |
| "Did my upload arrive intact?" | ETag = `md5(bytes)`, recompute locally | integrity |
| "Do I already have the latest version?" | `If-None-Match` | caching (`304`) |
| "Did someone change this since I read it?" | `If-Match` | concurrency (`412`) |

---

## 1. Start generic: an ETag is a version fingerprint (forget S3)

Forget object stores for a moment. Any HTTP server can attach an ETag to any
resource:

```
GET /avatar.jpg
< HTTP/1.1 200 OK
< ETag: "a1b2c3d4"
```

The `"a1b2c3d4"` is opaque — the client is **not** supposed to parse it, only
compare it for equality against one it saw before. Think of it as a version
number the server picks, except instead of `v1, v2, v3` it derives the value from
the content itself, so it can be computed statelessly by anyone holding the bytes.

The contract is exactly: **same bytes ⟺ same ETag.** That biconditional is the
whole thing. Both directions are load-bearing:

- bytes change → ETag must change, or a cache serves a stale file forever;
- bytes identical → ETag must match, or every conditional request is a cache miss.

---

## 2. The S3 choice: ETag = md5(bytes), which buys integrity for free

HTTP lets the server pick *any* fingerprint scheme. S3 picked a specific one: for
a normal single `PUT`, **the ETag is the hex MD5 of the object's bytes.** Your
store's type says so directly:

```rust
// src/object.rs
/// The S3 `ETag` of an object. **Not** the same thing as the content digest:
///   - single PUT → `hex(md5(bytes))` (V2).
///   - multipart  → `hex(md5(concat(decoded part md5s)))` + `"-" + N` ...
pub struct ETag(pub String);
```

Choosing a *content hash* (rather than a counter or a timestamp) buys a third job
the table above listed first — **integrity verification** — for free:

> Because the ETag is a hash the client can *also* compute, after uploading you
> can MD5 your local copy and check it equals the ETag the server returned. Match
> ⇒ the bytes arrived uncorrupted. A random version number can't prove this; only
> a hash of the content can.

This is why V2's stream loop runs **two** hashers at once (see
`[SPEC.md](../SPEC.md)`, V2):

| Hash | Audience | Becomes |
| --- | --- | --- |
| **SHA-256** | *you* (internal) | the blob's filename — content addressing, dedup (V1) |
| **MD5** | *the client / AWS SDK* (wire) | the `ETag` you return |

Two hashes, two consumers. SHA-256 names the blob on disk; MD5 is the S3-shaped
fingerprint you hand back. In your `[put_object](../src/routes.rs)` that MD5 is
what rides out on the response:

```rust
return Ok((StatusCode::OK, [(ETAG, part.etag.0)]).into_response());
```

---

## 3. Job two — caching: turn a download into a 0-byte "still current"

Here's the everyday payoff. A browser cached `avatar.jpg` with ETag
`"a1b2c3d4"`. On the next visit it doesn't blindly re-download — it *asks*:

```
GET /avatar.jpg
> If-None-Match: "a1b2c3d4"     ← "only send bytes if they differ from this"
< HTTP/1.1 304 Not Modified     ← server: same ETag; here's an empty body
```

A 2 MB image download becomes a ~200-byte "nope, still current." Multiply by every
CSS/JS/image asset on every page load and you see why ETags are load-bearing for
the whole web, not just object stores.

This is the horizontal box your project still owes, and the hook point already
has a `TODO` sitting on it in `[get_object](../src/routes.rs)`:

```rust
/// TODO(V4 / protocol): honour a `Range:` header → `206 Partial Content`, and
/// `If-None-Match` on the ETag → `304 Not Modified`.
async fn get_object(/* … */) -> Result<Response, AppError> {
    let _ = &headers; // TODO(V4): Range / If-None-Match live here.
```

The logic you'll add is small: read `If-None-Match`, compare it to `meta.etag`, and
if they match return `304` with an empty body instead of streaming the blob.

**The flip side matters just as much:** when the object *changes*, its bytes
change, so its MD5 (hence its ETag) changes, so the same conditional GET now
*fails* to match and the full new bytes come down. The cache busts itself, purely
because the fingerprint moved. You get correct cache invalidation for free — that's
the "bytes change → ETag changes" half of the contract earning its keep.

---

## 4. Job three — concurrency: `If-Match` stops the lost update

The subtle one nobody sees until it bites. Two users edit the same object:

```
Both read the object:                 ETag "02ab…4f"
Alice PUT  If-Match: "02ab…4f"   → 200  (current ETag still matched; her write lands,
                                          the ETag moves to "1c98…3e")
Bob   PUT  If-Match: "02ab…4f"   → 412  Precondition Failed
```

Bob is still guarding on the **stale** ETag `02ab…4f`, but Alice already moved the
object to `1c98…3e`. The server sees `current ≠ If-Match` and returns **412**,
refusing Bob's overwrite instead of silently erasing Alice's edit (the classic
"lost update" bug). This is optimistic concurrency — a lock-free
compare-and-swap using the ETag as the version token. It's the same primitive
etcd, CouchDB, and most REST document APIs lean on.

> **S3 caveat (so this doc doesn't mislead you):** real S3 does *not* implement
> `If-Match` write-locking on `PutObject` the way a REST document store does —
> S3 objects are last-writer-wins, with versioning as the escape hatch. Section 4
> teaches the *general HTTP ETag concurrency pattern* (which the `demo` shows and
> which your `AppError` could support), not a claim that `aws s3` rejects
> concurrent PUTs. The `304` caching and the two ETag *formulas* are exactly S3.

---

## 5. Job one, revisited — why multipart needs a *different* fingerprint

Now the store-specific twist, cross-linked to the multipart doc rather than
re-derived here. The `md5(bytes)` scheme has a fatal problem for a 5 GB object
assembled from 500 parallel parts: to compute `md5(whole object)` you'd have to
**re-read all 5 GB in order through one hasher**, just to produce a header — which
defeats the entire point of parallel multipart.

So S3 defined a fingerprint computable from *only the per-part MD5s it already
has*, with no re-read:

```
md5_concat = md5( fromhex(part1_md5) ++ fromhex(part2_md5) ++ … )   ← raw 16-byte md5s
ETag       = hex(md5_concat) + "-" + N                              ← N = part count
```

It's a **hash of the hashes**. Two consequences to internalize:

1. It is **not** `md5(bytes)` — a client that tried to verify integrity by MD5-ing
   the downloaded object would get a totally different value.
2. The **`-N` suffix is a protocol signal**: "I'm the multipart kind — do *not*
   md5 my bytes to check me." Get the formula wrong (hash the hex text instead of
   the raw bytes, drop the suffix, miscount parts) and the AWS SDK concludes the
   object is corrupt and rejects your response. That's the line between "an HTTP
   file server" and "S3-compatible."

The full worked derivation (with real hashes, and how `complete` computes it
alongside the SHA-256 CAS digest) is Section 5 of
`[01-how-multipart-uploads-work.md](01-how-multipart-uploads-work.md)`.

### The digest-vs-ETag confusion, in one table

These are two different fingerprints of the same object, for two different
audiences — don't conflate them:

| | Value | Hash of | Purpose | Lives in |
| --- | --- | --- | --- | --- |
| **Digest** (SHA-256) | `hex(sha256(bytes))` | the object's *bytes* | blob filename, dedup key | `[object.rs](../src/object.rs)` `Digest` (V1) |
| **ETag** (single PUT) | `hex(md5(bytes))` | the object's *bytes* | wire integrity / caching | `[object.rs](../src/object.rs)` `ETag` (V2) |
| **ETag** (multipart) | `hex(md5(part md5s)) + "-N"` | the *parts'* MD5s | wire integrity / SDK compat | `[object.rs](../src/object.rs)` `ETag` (V4) |

---

## 6. Watch it happen — the runnable demos

Two zero-dependency Python programs in `[docs/examples/](examples/)` make every
claim above observable. Run them:

```bash
cd docs/examples
python3 etag_demo.py       # spins a tiny object store, exercises all 3 ETag jobs over HTTP
python3 multipart_demo.py  # the multipart session on real files, out-of-order + retry
```

`etag_demo.py` stands up an in-memory object store implementing S3-style ETag
semantics, then a client hits it and prints the **actual request/response
headers**. Abridged real output:

```text
STEP 1 — single PUT: the ETag is md5(bytes), an integrity fingerprint
  client-side md5(bytes) = 45ef99dac168d9220fe4ea8c62a1d478  (we can predict the ETag)
  --> PUT /photos/cat.jpg   [Content-Type: image/jpeg]
  <-- 200 OK   ETag: "45ef99dac168d9220fe4ea8c62a1d478"
  -> server ETag matches our local md5: True

STEP 3 — CACHING: re-GET with If-None-Match -> 304, no bytes resent
  --> GET /photos/cat.jpg   [If-None-Match: "45ef…478"]
  <-- 304 Not Modified   (empty body)          ← a 44-byte object became ~0 bytes

STEP 4 — the object CHANGES; the ETag changes; the cache is busted
  --> PUT /photos/cat.jpg                       ← new bytes
  <-- 200 OK   ETag: "02ab10a08b76181987370e6a1be17a4f"
  --> GET /photos/cat.jpg   [If-None-Match: "45ef…478"]   (client's OLD etag)
  <-- 200 OK   (43B body)                       ← 200 now, not 304: full bytes come down

STEP 5 — CONCURRENCY: two writers, If-Match stops the lost update
  Alice PUT  [If-Match: 02ab…4f]  <-- 200 OK   ETag "1c98…3e"   (her write lands)
  Bob   PUT  [If-Match: 02ab…4f]  <-- 412 Precondition Failed   (refused; stale etag)

STEP 6 — MULTIPART: same bytes, but the ETag is md5(part-md5s)-N
  One PUT  ETag = 61892276af25e23cf843ea594874ff03      (plain md5 of the 30 bytes)
  3 parts  ETag = df538a786ea1d7c59a65487c9e3bac03-3    (md5 of the 3 part-md5s, + '-3')
```

`multipart_demo.py` mirrors `[src/multipart.rs](../src/multipart.rs)` one-to-one on
real files — `initiate → upload_part → complete → abort` — uploading parts *out of
order* with a retry to prove assembly is by part number, not arrival order, and
deriving the `-N` ETag by hand so there's no magic left in it.

---

## 7. Mental-model summary

| The instinct | The correction |
| --- | --- |
| "An ETag is just a random version id." | It's a **content hash**, so the client can independently verify integrity. |
| "ETags are only about caching." | Three jobs: integrity, caching (`If-None-Match`→304), concurrency (`If-Match`→412). |
| "Same object always has the same ETag." | Only the same **bytes** do. Edit it and the ETag *must* move — that's how caches invalidate. |
| "The object's ETag is `md5(bytes)`." | For **multipart** it's `md5(concat(part md5s)) + "-N"` — a hash of hashes, never re-md5'd. |
| "ETag and the content digest are the same." | Different hashes (MD5 vs SHA-256), different audiences (wire vs disk). |

For the multipart protocol itself — the session, staging, assembly order, and the
`-N` derivation in full — read
`[01-how-multipart-uploads-work.md](01-how-multipart-uploads-work.md)`.
