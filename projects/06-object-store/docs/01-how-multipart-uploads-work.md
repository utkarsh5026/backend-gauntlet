# How Multipart Uploads Work in S3 — From First Principles

> A beginner-friendly, ground-up guide to what a *multipart upload* actually is,
> why a serious object store cannot live without one, and the one deliberately
> weird detail (the ETag) that separates "an HTTP file server" from something the
> real `aws s3` CLI will talk to. No prior knowledge of S3, HTTP, or hashing
> assumed.
>
> This teaches the **concept** and how the *existing* code is wired to support it.
> The bodies of `initiate` / `upload_part` / `complete` / `abort` in
> `[src/multipart.rs](../src/multipart.rs)` are still `todo!()` — this doc will
> **not** write them for you. It explains the problem, the protocol, the shape of
> the code you already have, and exactly what the finished pieces must produce, so
> that when you fill the `todo!()`s you know what "correct" means.
>
> Anchored to real code: `[src/multipart.rs](../src/multipart.rs)`,
> `[src/routes.rs](../src/routes.rs)`, `[src/streaming.rs](../src/streaming.rs)`,
> `[src/store/mod.rs](../src/store/mod.rs)`, `[src/index.rs](../src/index.rs)`,
> `[src/object.rs](../src/object.rs)`, `[SPEC.md](../SPEC.md)`.

---

## 0. The one sentence to hold onto

**A multipart upload is not one upload — it is a *session*: the client splits a
big object into numbered parts, uploads each part as its own independent request
(in any order, retrying any that fail), and then sends one final "assemble these
parts, in this order" message that stitches them into a single object.**

Everything else — the `uploadId`, the staging area, the four HTTP verbs, the
cursed ETag — is machinery in service of that one idea: *turn one enormous,
fragile upload into many small, independently-retryable uploads plus a cheap
assembly step.*

---

## 1. The problem: why a single `PUT` is not enough

You already built the single-`PUT` path in V2 —
`[stream_to_store](../src/streaming.rs)`. It streams the request body to disk one
chunk at a time, so even a 5 GB body costs O(1) memory. That solved the *memory*
problem. It did **not** solve the *network* problem.

Picture a client uploading a 5 GB video over hotel Wi-Fi as **one** `PUT
/bucket/video.mp4` request. Here is what goes wrong, concretely:

| Failure during a single 5 GB `PUT`             | What it costs you                                                                 |
| ---------------------------------------------- | --------------------------------------------------------------------------------- |
| Wi-Fi drops at 4.9 GB (99% done)               | The whole request is dead. You start over **from byte 0**. All 4.9 GB re-sent.    |
| The client wants to go faster                  | One TCP connection ≈ one lane. You cannot use 10 connections to fill the pipe.    |
| The server restarts mid-upload                 | Nothing was committed; the client has no "resume from here" handle.               |
| The client only knows the total size at the end (live transcode) | A single `PUT` usually wants a `Content-Length` up front.       |
| Two uploads of the *same* huge file            | No way to say "I already have parts 1–50, only send me 51."                       |

The naive fix — "just let the client retry" — doesn't help, because retrying a
single request means re-sending *everything*. The unit of retry is the whole
object, and the whole object is the thing that's too big to reliably send.

**The insight:** make the unit of retry *small*. If the 5 GB object is 500 parts
of 10 MB each, then a dropped connection at 99% costs you **one 10 MB part**, not
5 GB. And since the parts are independent requests, the client can send 10 of
them at once over 10 connections. Resumability and parallelism fall out of the
same decision: *chop the object into independently-addressable pieces.*

That decision is the multipart upload. The SPEC states the mandate directly:

> build the protocol that lets a 5 GB upload survive a flaky network: split it
> into parts, upload them in parallel and out of order, and assemble at the end.
> — `[SPEC.md](../SPEC.md)`, V4

---

## 2. The session: four verbs and a shared secret

Because an upload is now *many* requests that must be tied together, the server
needs to remember "these requests all belong to the same in-progress object."
That memory is the **session**, and the thing that names it is the **`uploadId`**
— an opaque token the server mints and the client echoes on every later request.

There are exactly four operations, and they map one-to-one onto the four methods
of `[Multipart](../src/multipart.rs)` and the four HTTP verbs wired in
`[routes.rs](../src/routes.rs)`:

```
                          the shared secret: uploadId
                                     │
   ┌─────────────┐   returns   ┌─────┴──────┐
   │  Initiate   │ ──────────▶ │  uploadId  │  the client now holds this
   └─────────────┘             └────────────┘
         │                                        one part at a time,
         ▼                                        in ANY order, retryable
   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
   │ UploadPart 1│   │ UploadPart 3│   │ UploadPart 2│  ...
   └─────────────┘   └─────────────┘   └─────────────┘
         │   each returns that part's ETag (its MD5)
         ▼
   ┌───────────────────────────────────────────────┐
   │ Complete: "assemble parts 1,2,3 in THIS order" │ ──▶ one finished object
   └───────────────────────────────────────────────┘
         │
         └── (or) Abort: "throw the whole session away"
```

| Verb                        | HTTP (from `[routes.rs](../src/routes.rs)`)          | Method on `Multipart`                          | Job                                                        |
| --------------------------- | ---------------------------------------------------- | ---------------------------------------------- | --------------------------------------------------------- |
| `InitiateMultipartUpload`   | `POST /{bucket}/{key}?uploads`                       | `[initiate](../src/multipart.rs)`              | Mint a fresh `uploadId`; create a staging area.           |
| `UploadPart`                | `PUT /{bucket}/{key}?uploadId=…&partNumber=N`        | `[upload_part](../src/multipart.rs)`           | Stream one numbered part into the session; return its ETag.|
| `CompleteMultipartUpload`   | `POST /{bucket}/{key}?uploadId=…`                    | `[complete](../src/multipart.rs)`              | Validate + concatenate parts in order → one object.       |
| `AbortMultipartUpload`      | `DELETE /{bucket}/{key}?uploadId=…`                  | `[abort](../src/multipart.rs)`                 | Discard the session and its staged parts.                 |

Notice the routes **reuse the object routes** and dispatch on query parameters —
there is no separate `/multipart/` URL space. A `PUT` with `?uploadId&partNumber`
is a part; a `PUT` without them is an ordinary single-shot object. That branch is
literally in the code, in `[put_object](../src/routes.rs)`:

```rust
// UploadPart: a PUT carrying ?uploadId & ?partNumber (V4).
if let (Some(upload_id), Some(part_number)) = (q.upload_id.as_deref(), q.part_number) {
    let part = state.multipart.upload_part(/* … */).await?;
    return Ok((StatusCode::OK, [(ETAG, part.etag.0)]).into_response());
}
// …otherwise: a plain single PUT (V2 → V3).
```

So the HTTP layer is already done for you. The four `todo!()`s live one layer
down, inside `Multipart`.

---

## 3. "The session is state" — where does the state live?

A session has to remember two kinds of thing between requests:

1. **The target** — which `(bucket, key, content_type)` this upload will
   eventually become. The client tells you *once*, at `Initiate` time. `Complete`
   fires much later and the request body at that point is just a list of parts —
   so if you didn't persist the target, `complete` would have nowhere to put the
   assembled object.
2. **The staged parts** — the actual bytes of each numbered part, uploaded so far,
   waiting to be assembled.

Look at what `[Multipart](../src/multipart.rs)` is given to work with:

```rust
pub struct Multipart {
    root: PathBuf,        // the "uploads/" staging area, created in open()
    store: Arc<Store>,    // V1 — commit the finished blob here
    index: Arc<Index>,    // V3 — record (bucket,key) → blob here
}
```

and `[open](../src/multipart.rs)` sets up the staging root:

```rust
let root = root.as_ref().join("uploads");
std::fs::create_dir_all(&root)?;
```

So the mental model is a directory tree: **one subdirectory per live session**,
named by its `uploadId`, holding the staged parts plus a small record of the
target. This mirrors exactly the pattern the rest of the project already uses —
V1's `[tmp/](../src/store/mod.rs)` staging dir and V3's per-bucket `[tmp/](../src/index.rs)`
dir. Nothing new conceptually: *in-flight things live in a staging area; finished
things get atomically moved into their permanent home.*

```
data/
├── objects/          ← V1: committed, content-addressed blobs (finished objects)
├── index/            ← V3: (bucket,key) → digest pointers
└── uploads/          ← V4: one dir per in-flight session
    └── <uploadId>/
        ├── target        (bucket, key, content_type — persisted by initiate)
        ├── 00001          part 1's bytes
        ├── 00003          part 3's bytes   ← note: 2 hasn't arrived yet
        └── …
```

> The doc comments in `[multipart.rs](../src/multipart.rs)` describe *what* each
> method must produce; the layout above is the natural shape, but the exact
> on-disk encoding is yours to choose when you implement the `todo!()`s. The point
> here is only: **a session is a directory of staged parts + a target record.**

---

## 4. Uploading a part reuses V2 — you already wrote the hard part

Here is the reassuring part. `[upload_part](../src/multipart.rs)`'s signature is
almost identical to V2's `[stream_to_store](../src/streaming.rs)`:

```rust
// upload_part — the V4 signature
pub async fn upload_part<S>(&self, upload_id: &str, part_number: u32,
                            body: S, max_part_size: u64) -> Result<PartETag, AppError>
where S: Stream<Item = Result<bytes::Bytes, axum::Error>> + Unpin;

// stream_to_store — the V2 loop you already built
pub async fn stream_to_store<S>(store: &Store, body: S, max_size: u64)
    -> Result<Stored, AppError>
where S: Stream<Item = Result<bytes::Bytes, axum::Error>> + Unpin;
```

A part **is just a small object**. It streams in over HTTP exactly like a
single-shot `PUT` body: pull `bytes::Bytes` chunks from the stream, write them to
a temp file, hash as you go, enforce a size cap, clean up on error. The V2 loop in
`[streaming.rs](../src/streaming.rs)` already does *all* of that — the same
chunk-pump, the same "delete the temp on error/oversize" discipline:

```rust
loop {
    match body.next().await {
        None => break,
        Some(Ok(bytes)) => {
            total_file_size += bytes.len() as u64;
            if total_file_size > max_size { /* remove temp, EntityTooLarge */ }
            sha_hasher.update(&bytes);
            md5_hasher.update(&bytes);
            temp_file.write_all(&bytes).await?;
        }
        Some(Err(err)) => { /* remove temp, surface the error */ }
    }
}
```

The differences for a part are small and stated in the `todo!()` note in
`[upload_part](../src/multipart.rs)`:

- Stage the finished temp file **under this session, keyed by `part_number`** (in
  `uploads/<uploadId>/` rather than committing it to the global blob store yet) —
  it isn't a real object, it's a piece of one.
- **Overwrite on retry:** if part 3 is uploaded twice (the first attempt timed
  out), the second write replaces the first. Part number is the identity.
- Validate `part_number` is in S3's legal range **1..=10000**, and enforce
  `max_part_size`.
- Return the part's **MD5** as its ETag — that's what `PartETag` carries:

```rust
pub struct PartETag {
    pub part_number: u32,
    pub etag: ETag,   // = hex(md5(part bytes))
}
```

Why hand the MD5 back to the client? Two reasons, both about *validation at
assembly time*: the client stores each part's ETag and echoes the full list back
in `Complete`, so the server can (a) confirm no part was corrupted in transit and
(b) — as we're about to see — reconstruct the final object's ETag from them.

---

## 5. The cursed ETag — the whole reason V4 is hard

Now the one detail that trips *everyone*, and the reason this vertical exists.

### 5.1 First, what an ETag even is

An **ETag** ("entity tag") is a short string an HTTP server returns to identify a
specific version of a resource. Clients use it for two things: **integrity** ("did
I get the bytes I expected?") and **conditional requests** (`If-None-Match: <etag>`
→ "only send me the body if it *changed*"). S3 returns it on every object.

The subtlety is entirely in *how S3 computes it*. Read the doc comment in
`[object.rs](../src/object.rs)` — the codebase spells out both formulas:

```rust
/// The S3 `ETag` of an object. **Not** the same thing as the content digest:
///   - single PUT → `hex(md5(bytes))` (V2).
///   - multipart  → `hex(md5(concat(decoded part md5s)))` + `"-" + N`, where N
///     is the part count (V4). The `-N` suffix is how a client knows the object
///     was multipart and must not re-MD5 it to verify.
pub struct ETag(pub String);
```

### 5.2 The trap

The intuitive guess is: *the ETag of the assembled object is `md5` of the whole
assembled bytes.* That is what a single `PUT` does, so surely multipart is the
same? **No.** For a multipart object the ETag is **not** the MD5 of the bytes at
all. It is a *hash of hashes*:

```
per-part ETag_i = hex( md5( bytes of part i ) )          ← computed in upload_part
multipart ETag  = hex( md5( concat( hex_decode(ETag_i) for each part, in order ) ) )
                  + "-" + N                                ← N = number of parts
```

Read that carefully — there are two non-obvious moves:

1. You concatenate the **raw 16-byte MD5 digests** of the parts (`hex_decode` each
   part ETag back to bytes first — *not* the hex text), MD5 that concatenation,
   and hex-encode the result.
2. You append `"-N"`, the part count. That suffix is a **flag**: it tells any
   client "this object was multipart — do **not** try to verify it by MD5-ing the
   bytes, because that number will never match."

Why did S3 do it this way? Because it never wants to re-read a 5 GB object to
compute its ETag. Each part's MD5 is computed *once*, while the part streams in.
`Complete` then only needs the 16-byte digests — a few kilobytes even for
thousands of parts — to produce the final ETag. It's an O(parts) operation, not
O(bytes). The weirdness is a performance decision frozen into a wire format.

### 5.3 A real worked example (verified with actual hashes)

Let's trace a tiny two-part upload all the way to the ETag. Part 1 is ten `A`
bytes, part 2 is four `B` bytes (absurdly small — real parts are ≥5 MiB — but the
*math* is identical). These digests were computed with a real MD5/SHA-256
implementation, not hand-waved:

```
part 1 bytes = "AAAAAAAAAA"  (10 bytes)
part 2 bytes = "BBBB"        (4 bytes)

Step 1 — per-part ETag = hex(md5(part bytes)):        [returned by upload_part]
  ETag_1 = 16c52c6e8326c071da771e66dc6e9e57
  ETag_2 = f50881ced34c7d9e6bce100bf33dec60

Step 2 — hex_decode each ETag to its raw 16 bytes, concatenate (in part order):
  raw = 16c5…9e57  ++  f508…ec60     →  32 bytes total (16 + 16)

Step 3 — md5 that 32-byte blob, hex-encode:
  md5(raw) = 896e2924e57dd6cfb040aee6a9c58e70

Step 4 — append "-N", N = 2:
  multipart ETag = 896e2924e57dd6cfb040aee6a9c58e70-2      ✅ this is what Complete returns
```

Contrast with the two hashes it is **not**:

```
WRONG  md5(whole 14 bytes "AAAAAAAAAABBBB") = 245277f6f4ba689b14dfd381013a8fc7
       ^ the naive "just md5 the object" answer — a client would reject this.

sha256(whole 14 bytes) = 72b00b26928cb16a56214b72f59410b80d61211b5091c8564b93d14afa64f7d0
       ^ this is the CAS *digest* (V1's blob name), a DIFFERENT thing from the ETag.
```

That last line is the second thing people conflate. **The ETag and the content
digest are two different hashes serving two different purposes:**

| Value               | Hash            | Computed over                        | Used for                             | Where in code                                  |
| ------------------- | --------------- | ------------------------------------ | ------------------------------------ | ---------------------------------------------- |
| `Digest` (CAS name) | SHA-256         | the assembled object's raw bytes     | naming/deduping the blob on disk     | `[store.blob_path](../src/store/mod.rs)` (V1)      |
| `ETag` (wire tag)   | MD5-of-MD5s + N | the *parts'* MD5s                    | HTTP integrity / SDK compatibility   | `[object.rs](../src/object.rs)` (V4)           |

`complete` computes **both** in one pass: it SHA-256-hashes the concatenated bytes
(to get the digest it commits under, via V1) *and* assembles the multipart ETag
from the per-part MD5s it already has.

> This is the acceptance test for V4, verbatim from the `todo!()` in
> `[multipart.rs](../src/multipart.rs)` tests: *"the completed object's ETag
> matches `hex(md5(concat(part_md5s)))-N` (cross-check against `aws s3 cp` of the
> same file + part size)."* If your formula is right, real AWS tooling agrees byte
> for byte; if it's wrong, the SDK rejects you.

---

## 6. Completing: assemble in order, then reuse V1 + V3

`[complete](../src/multipart.rs)` is where the session becomes a real object. Its
signature receives the client's ordered claim:

```rust
pub async fn complete(&self, upload_id: &str, parts: Vec<PartETag>)
    -> Result<ObjectMeta, AppError>;
```

The `parts` vector is *the client's assertion*: "the object is these parts, in
this order, and here is each one's ETag." The `todo!()` note lays out the four
obligations. In plain terms:

1. **Validate.** For each claimed part, the ETag the client sent must match the
   ETag of what you actually staged in `upload_part`. A mismatch means the client
   and server disagree about the bytes — reject it. (Real S3 also enforces a 5 MiB
   minimum on every part *except the last*; the note leaves that to your judgment.)
2. **Concatenate in part-number order**, streaming, while SHA-256-hashing the whole
   thing → the CAS digest. Order matters absolutely: parts arrived out of order,
   but the object is `part1 ++ part2 ++ … ++ partN`. Get the order wrong and you've
   assembled a corrupt file with a plausible-looking ETag.
3. **Commit via V1.** Hand the assembled temp file to
   `[store.commit_temp](../src/store/mod.rs)` — the same atomic
   temp→fsync→rename→fsync-dir dance every finished object goes through. Dedup is
   free: if those exact assembled bytes already exist, `commit_temp` drops the temp
   and keeps the one blob.
4. **Compute the multipart ETag** (Section 5), build the `[ObjectMeta](../src/object.rs)`,
   `[index.put](../src/index.rs)` it (V3), and **delete the session's staging dir.**

Steps 3 and 4 are almost entirely *code you already wrote in V1 and V3*. That's
the payoff of the layered design: `complete` orchestrates, it doesn't reinvent.
The ordering contract from V3 still holds — **blob durable first, index entry
second** — so a crash between them leaves a GC-able orphan blob, never a dangling
key. (See `[index.rs](../src/index.rs)`'s module comment on that write-order
contract, and the GC that reclaims orphans.)

`[abort](../src/multipart.rs)`, by contrast, is the easy sibling: delete the
session's staging dir, tolerate "already gone," and map an unknown `uploadId` to
`AppError::NoSuchUpload`. Nothing was ever committed to `objects/` or `index/`, so
there is nothing to unwind — the staged parts just evaporate.

---

## 7. End-to-end trace: a 3-part upload through every layer

Let's follow one real upload of a `sunset.mp4` object into bucket `photos`,
watching each HTTP request hit the wired route and call into `Multipart`. Assume
three parts, and the client deliberately uploads them **out of order** and retries
part 2 once.

```
① POST /photos/sunset.mp4?uploads              → post_object() sees q.uploads
   Content-Type: video/mp4                        → multipart.initiate("photos","sunset.mp4","video/mp4")
   ──────────────────────────────────────────▶  persists target, mints uploadId="U7"
   ◀── 200 { "uploadId": "U7" }                   creates uploads/U7/

② PUT /photos/sunset.mp4?uploadId=U7&partNumber=3   → put_object() sees uploadId+partNumber
   <10 MB body streams in>                          → multipart.upload_part("U7", 3, body, cap)
   ──────────────────────────────────────────────▶  streams to uploads/U7/00003, md5 as it goes
   ◀── 200  ETag: "c1d2…"  (part 3's md5)            (part 3 arrives BEFORE parts 1,2 — fine)

③ PUT …&partNumber=1   ──▶ upload_part("U7",1,…)  ──▶  uploads/U7/00001,  ETag "a1b2…"

④ PUT …&partNumber=2   ──▶ (connection drops at 90% — client got no ETag, retries)

⑤ PUT …&partNumber=2   ──▶ upload_part("U7",2,…)  ──▶  OVERWRITES uploads/U7/00002, ETag "b3c4…"

⑥ POST /photos/sunset.mp4?uploadId=U7            → post_object() sees uploadId (no ?uploads)
   body lists parts: [ {1,"a1b2…"},                 → multipart.complete("U7", [part1,part2,part3])
                       {2,"b3c4…"},                     1. validate each ETag vs staged parts ✔
                       {3,"c1d2…"} ]                     2. concat 00001++00002++00003, sha256 → digest
   ──────────────────────────────────────────────▶    3. store.commit_temp(temp, digest)   [V1]
                                                        4. etag = md5(md5₁‖md5₂‖md5₃) + "-3"
                                                           index.put(ObjectMeta{…})          [V3]
                                                           rm -rf uploads/U7/
   ◀── 200 { "etag": "…-3" }                        the object now GETs like any other
```

From this point on, `sunset.mp4` is indistinguishable from a single-`PUT` object.
`GET /photos/sunset.mp4` in `[get_object](../src/routes.rs)` looks up the V3
pointer, opens the V1 blob, and streams it back — it has no idea the object was
ever multipart. The only trace is the `-3` on its ETag. That's the whole point:
multipart is a *delivery mechanism*; the stored object is just an object.

---

## 8. Mental-model summary

| It looks like…                                    | It actually is…                                                                              |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| "A big upload"                                    | Many small independent uploads + one assemble step — the *session*.                          |
| The `uploadId` is a filename                      | An opaque token naming a **staging directory** of parts + a target record.                   |
| A part is a special kind of thing                 | Just a small object; it streams in through the **same V2 loop** as any `PUT`.                |
| Parts must arrive in order                        | They arrive in **any** order; only *assembly* (`complete`) is ordered, by part number.       |
| The object's ETag is `md5(bytes)`                 | For multipart it's `md5(concat(part md5s)) + "-N"` — a **hash of hashes**, never re-MD5'd.    |
| ETag and content digest are the same hash         | Two different hashes: **MD5-of-MD5s** (wire ETag) vs **SHA-256 of bytes** (CAS blob name).    |
| `complete` writes a new storage engine            | It **reuses** V1 `commit_temp` (atomic, dedup'd) and V3 `index.put` (the pointer).           |
| `abort` has to clean up committed data            | Nothing was committed — it just deletes the staging dir.                                      |

---

## 9. Where to look in the code

| Subtopic                                   | File / symbol                                                              |
| ------------------------------------------ | ------------------------------------------------------------------------- |
| The four session methods (the `todo!()`s)  | `[src/multipart.rs](../src/multipart.rs)` — `initiate/upload_part/complete/abort` |
| Per-part return value (part# + MD5)        | `[PartETag](../src/multipart.rs)`                                          |
| HTTP verb → method dispatch on query params| `[put_object](../src/routes.rs)`, `[post_object](../src/routes.rs)`, `[delete_object](../src/routes.rs)` |
| The streaming loop a part reuses           | `[stream_to_store](../src/streaming.rs)` (V2)                             |
| Atomic, dedup'd commit of the finished blob| `[Store::commit_temp](../src/store/mod.rs)` (V1)                              |
| The `(bucket,key) → blob` pointer + order  | `[Index::put](../src/index.rs)` (V3)                                       |
| Both ETag formulas, spelled out            | `[ETag](../src/object.rs)` doc comment                                     |
| The stored-object record `complete` returns| `[ObjectMeta](../src/object.rs)`                                           |
| The vertical's ticket + acceptance criteria| `[SPEC.md](../SPEC.md)` — V4                                               |

---

## 10. Check yourself

If these click, you understand multipart; if not, re-read the linked section:

1. Why does a dropped connection at 99% cost a single 10 MB part instead of the
   whole 5 GB? *(§1 — the unit of retry is the part.)*
2. What two things must `initiate` persist so that `complete`, firing minutes
   later with only a part list, knows where the object goes? *(§3.)*
3. Part 2 is uploaded twice because the first attempt timed out. What happens to
   the first copy, and why is part *number* (not arrival order) the identity? *(§4.)*
4. Given per-part ETags `16c5…9e57` and `f508…ec60`, why is the object's ETag
   `896e2924e57dd6cfb040aee6a9c58e70-2` and **not** `md5` of the concatenated
   bytes? Walk the four steps. *(§5.3.)*
5. Which hash names the blob on disk, and which one goes in the `ETag` header —
   and why are they different algorithms? *(§5.3 table.)*
6. Why can `complete` reuse `commit_temp` and `index.put` unchanged instead of
   writing new storage code? *(§6.)*
