<!-- status:
state: active            # active | paused | blocked | done | not-started
blocked-on: ~            # free text, or ~ for none
-->

# Project 06 — S3-compatible Object Store

> "Store a blob, hand it back by name." It sounds like `write(file)` /
> `read(file)` with an HTTP coat of paint — and for a 10 KB file on a laptop, it
> is. Every word in *S3-compatible object store* is a trap that only springs at
> scale. Objects aren't 10 KB, they're **5 GB**, so the instant you write
> `let body = req.bytes()` you've put a movie in RAM and one upload OOM-kills the
> box — you must **stream** bytes through to disk and never hold more than a
> chunk (and let a slow disk push **backpressure** onto a fast client). The same
> bytes get uploaded a thousand times — the same Docker layer, the same avatar —
> so naming a blob by *where the user put it* stores it a thousand times; naming
> it by **the hash of its content** stores it once (**content addressing** +
> dedup). A write interrupted by a crash — power, OOM, `kill -9` — must **never**
> be observable half-done, or a reader streams a truncated file and trusts it;
> that forces the **temp-file → fsync → atomic rename → fsync-dir** dance that
> real filesystems make you do by hand. Uploads of multi-GB objects die halfway
> across a flaky network, so a usable store needs **multipart**: resumable,
> parallel, out-of-order parts assembled at the end. The keyspace is **flat**
> (`a/b/c.jpg` is one opaque key, not three directories) yet every client expects
> to "list a folder", so you fake a hierarchy with **prefix/delimiter** listing.
> Deleting a key can't delete its bytes — another key may share them — so delete
> is a pointer drop and reclamation is a separate **GC**. And to be *S3*-
> compatible (so `aws s3` / the AWS SDK actually talk to you), the **ETag** must
> follow S3's exact, slightly cursed formula. It's `write()`/`read()` wrapped in
> a streaming, durability, dedup, and protocol-compatibility problem. That's the
> rung.

## What it does (the easy part)
- A path-style **S3 HTTP API**: `PUT /{bucket}` to create a bucket,
  `PUT /{bucket}/{key}` to store an object (the body is streamed),
  `GET /{bucket}/{key}` to fetch it (streamed, with HTTP `Range` support),
  `DELETE /{bucket}/{key}` to remove a key, and `GET /{bucket}` to list a
  bucket with `prefix` / `delimiter` / pagination.
- **Multipart uploads**: `POST /{bucket}/{key}?uploads` to initiate,
  `PUT /{bucket}/{key}?uploadId=…&partNumber=N` to upload a part,
  `POST /{bucket}/{key}?uploadId=…` to complete (assemble), and
  `DELETE /{bucket}/{key}?uploadId=…` to abort.
- A `GET /healthz` for liveness.

> There is **no database and no docker-compose** here: the filesystem *is* the
> store. The parts you'd normally get from S3/MinIO — content addressing, the
> durable atomic commit, multipart assembly, the prefix listing, the ETag — are
> the things you build on top of plain files. The whole point is that "an object
> store" is a layout discipline over a directory, not a service you call.

---

## Vertical challenges (build these yourself — this is the learning)

### V1. The content-addressed blob store — *the durable, dedup'd CAS, from scratch*
In `src/store.rs`, build the layer that turns bytes into a durably-stored,
content-named blob. This is the foundation everything else writes through.
- **Name a blob by its content, not its key.** The blob's filename is the
  SHA-256 of its bytes (hex). Two different keys with identical content resolve
  to the same digest → the bytes are stored **once** (dedup). The key→digest
  mapping is V3's job; V1 only owns "given finished bytes + their digest, store
  them safely and idempotently".
- **The atomic durable commit is the lesson.** You cannot write straight to
  `objects/<hash>`: a crash mid-write leaves a file that *looks* complete (right
  name) but is truncated, and every future reader trusts it. The discipline:
  write to a **temp** file, `fsync` it, then **atomically `rename`** it onto the
  final path, then **`fsync` the parent directory** so the rename itself
  survives a crash. `rename` within a filesystem is atomic — that's the whole
  trick. If the digest already exists, drop the temp file (dedup, no rewrite).
- **Fan out the directory.** `objects/<64-hex-chars>` in one flat directory
  melts at a few million entries. Shard by the leading hash bytes:
  `objects/ab/cd/abcd…`. Pick the fan-out and justify it.

*Concept to internalize:* content-addressed storage and why the hash is the
name; dedup as a free consequence; and the temp→fsync→rename→fsync-dir sequence
that is the *only* way to make "the file is fully there or not there at all" true
across a crash.

### V2. Streaming bodies, end to end — *bounded memory + backpressure*
In `src/streaming.rs`, wire the HTTP body to V1's writer so an object of *any*
size costs O(1) memory. This is where "10 KB on a laptop" and "5 GB in prod"
stop being the same program.
- **Upload:** pull the request body **one chunk at a time**, and for each chunk
  (a) enforce a running byte-count cap (reject early — don't let a client stream
  you out of disk), (b) write it to the temp file, and (c) feed it to **two**
  hashers at once: SHA-256 (the V1 content name) and MD5 (the S3 ETag, V2/V4).
  On clean EOF, finalize and hand off to `store.commit_temp`. **Never** collect
  the body into a `Vec<u8>` — that's the bug the whole vertical exists to avoid.
- **Backpressure is implicit and you must understand why:** because you only ask
  for the next chunk after the last one is written, a slow disk slows the
  *client* instead of ballooning memory. Buffer the whole body and you've traded
  backpressure for an OOM.
- **Partial-upload cleanup:** a client that disconnects mid-PUT (or trips the
  size cap) must not leave a temp file behind. The write path needs a cleanup on
  *every* early exit — the unhappy paths are the point.
- **Download:** stream the blob file back as the response body (a `ReaderStream`
  over the file), again never loading it whole. This is also where `Range`
  serving (V4 / horizontal) hooks in.

*Concept to internalize:* streaming I/O as the difference between O(1) and O(n)
memory; backpressure as a property you get for free *if* you don't buffer; and
that the error/cancel paths (not the happy path) are what make streaming safe.

### V3. The bucket/key namespace + a crash-safe index — *flat keyspace, faked folders, GC*
In `src/index.rs`, build the `(bucket, key) → blob` mapping and the rules that
keep it consistent with V1's blobs across crashes and deletes.
- **The keyspace is flat.** `a/b/c.jpg` is a single opaque key; the `/`s mean
  nothing to storage. But `ListObjectsV2` must *pretend* it's a tree: `prefix`
  filters keys, and `delimiter` (`/`) rolls every key sharing the next path
  segment up into one **common prefix** (a "folder"). Implement prefix +
  delimiter + pagination (`max-keys` + a continuation token, keys in sorted
  order). This collapse is the entire illusion of folders in S3.
- **The write order is a crash-consistency contract.** V2 commits the blob to
  disk *before* V3 records the pointer. Hold that invariant: **blob durable →
  then** index entry. Crash in between and you have an unreferenced blob (garbage
  the GC reclaims) — **never** a key pointing at a blob that isn't there. Make
  the index update itself atomic (write-temp+rename, or append+fsync of a log).
- **Delete drops the pointer, not the bytes.** Another key may share that digest
  (dedup), so deleting a key can't `rm` the blob. Reclamation is a separate
  **mark-and-sweep GC**: collect every digest the live index references, then
  delete blobs nothing points at — while being careful not to reap a blob from a
  PUT that committed its bytes but hasn't written its index entry *yet*.

*Concept to internalize:* a flat keyspace vs. the hierarchical *listing* layered
over it (prefix/delimiter); the blob-then-pointer write order as the rule that
guarantees no dangling references; and refcount-by-GC as why "delete" is cheap
and reclamation is lazy.

### V4. Multipart upload + the S3 ETag — *resumable, parallel uploads & wire compat*
In `src/multipart.rs`, build the protocol that lets a 5 GB upload survive a flaky
network: split it into parts, upload them in parallel and out of order, and
assemble at the end.
- **The session is state.** `Initiate` mints an `uploadId` and a staging area;
  `UploadPart` streams one numbered part into it (reusing the V2 loop) and
  returns that part's ETag (its MD5); parts may arrive in any order and be
  retried (a re-upload of part N overwrites). `Complete` takes the client's
  ordered part list, **validates** each part's ETag against what you staged,
  concatenates the parts **in part-number order** while SHA-256-hashing the whole
  thing into the final blob (commit via V1), then indexes it (V3). `Abort`
  discards the session and reclaims its staged parts.
- **The ETag is the compatibility test, and it's deliberately weird.** For a
  single PUT, `ETag = hex(md5(bytes))`. For a *multipart* object it is **not**
  the MD5 of the bytes — it is `hex(md5(concat(decoded part MD5s))) + "-" + N`,
  where N is the part count. The `-N` suffix is how a client *knows* an object
  was multipart and must not re-MD5 it to verify. Get this exactly right or the
  AWS SDK rejects your responses — that's the line between "an HTTP file server"
  and "S3-compatible".

*Concept to internalize:* multipart as a resumable, parallelizable upload
*session*; assembly order and per-part validation; and the multipart ETag formula
as a concrete, testable definition of protocol compatibility.

---

## Horizontal checklist (the backend fundamentals)

### Protocols / API
- [x] Path-style S3 routing: bucket + key (keys contain `/` → a wildcard route),
  with the multipart verbs dispatched on query params (`?uploads`,
  `?uploadId`, `?partNumber`). Sensible status codes via `AppError`:
  `404 NoSuchKey` / `NoSuchBucket`, `400` for a malformed request,
  `413` when an object/part exceeds the cap.
- [x] **HTTP `Range` requests** on GET: `Range: bytes=a-b` → `206 Partial
  Content` with a `Content-Range` header, serving only that slice of the blob
  (this is what makes the store usable for video seek / resumable download).
- [x] **Conditional requests:** `If-None-Match` on the ETag → `304 Not Modified`;
  return `ETag`, `Content-Length`, `Content-Type`, `Last-Modified` on GET/HEAD.
- [x] **S3 XML wire format** for `ListBucketResult` and the multipart
  init/complete bodies (the scaffold returns JSON as a placeholder — switch
  to XML for real `aws s3` / SDK compatibility). Note where it's faked.
  (Lifecycle config stays JSON; CompleteMultipartUpload also accepts a JSON
  body from the playground when `Content-Type: application/json`.)
- [x] **Disable axum's default body limit** (objects stream; the 2 MB default
  would truncate every real upload) and enforce your *own* `MAX_OBJECT_SIZE`
  in the stream loop instead. Graceful shutdown that lets in-flight streams
  finish.

### State & durability

- [x] The atomic commit (V1) holds under a crash: a `kill -9` during a PUT leaves
  **either** the whole object **or** nothing — never a truncated blob under
  its final name. Demonstrate it (kill mid-write, then read back).

- [x] The blob-then-pointer order (V3) holds: a crash between commit and index
  leaves an orphan blob (GC-able), never a dangling key.

- [x] Dedup is real: PUT the same bytes under two keys and assert one blob on
  disk. Delete one key and the blob survives until the other key is gone too.

### Security / abuse protection
- [ ] Authenticate writes (and optionally reads). Real S3 uses **SigV4**
  request signing; a simplified access-key/HMAC scheme is a fair learning
  target — at minimum gate PUT/DELETE behind a credential. An open
  `PUT /{bucket}/{key}` is an open disk for the whole internet.
- [x] Validate and **cap** everything the caller controls: object & part size,
  bucket names (S3 rules: 3–63 chars, lowercase, no leading `/`), key length,
  part numbers. Reject path traversal — a key must never escape the data dir
  (`../../etc/...`); resolve through the content-addressed layout, not raw
  user paths.
- [x] Never trust the client-supplied `Content-Length` for accounting — count the
  bytes you actually stream.

### Observability
- [x] Counters: objects PUT / GET / DELETE, multipart initiated / completed /
  aborted, **dedup hits** (a PUT whose content already existed), GC blobs
  reclaimed, range requests served.

- [x] Gauges: total bytes stored, blob count, in-flight uploads, open multipart
  sessions (a climbing count of never-completed sessions is a leak/abuse
  signal).

- [x] Histograms: upload/download **throughput** (bytes/sec) and object-size
  distribution; a `tracing` span per request carrying `bucket`, `key`, and
  `size`. Never log object bodies.

Proof: `tests/observability.rs`, `tests/http_api.rs`, and multipart module tests.

---

## Cross-cutting scale skills
- Bounded memory: a *proven* O(1)-memory path for an object far larger than RAM
  (stream a multi-GB PUT/GET and watch RSS stay flat).
- Crash consistency: a defined, demonstrated answer to "we died mid-write" — the
  atomic commit (V1) and the blob-then-pointer order (V3).
- Backpressure: a slow consumer/disk slows the producer/client instead of
  buffering — the natural result of never collecting a whole body.
- Reclamation: an explicit story for shared content — delete is a pointer drop,
  GC reclaims, and the GC↔in-flight-PUT race has a stated resolution.

## Definition of done
1. All vertical + horizontal boxes checked.
2. A `bench/` load test (a Rust or `k6`/`s3-bench` client, or even the `aws s3`
   CLI pointed at your endpoint) reporting: sustained **upload/download
   throughput** (MB/s) and memory (**RSS stays flat** while streaming an object
   many times larger than RAM — the V2 payoff); **dedup** proof (N identical
   PUTs → 1 blob on disk, and the storage saved); a **crash test** that
   `kill -9`s mid-PUT and shows no truncated object is ever served (V1); and a
   **multipart** run that uploads a large object in parallel parts and verifies
   the assembled object's **ETag matches S3's `-N` formula** (V4). Numbers in
   `docs/06-benchmarks.md`.
3. A short `docs/06-design.md`: the on-disk layout and fan-out; the exact
   durable-commit sequence and *why each fsync is there*; the index format and
   the blob-then-pointer invariant; the prefix/delimiter listing algorithm; the
   multipart assembly + the two ETag formulas; and the GC design including the
   in-flight-PUT race.

## Suggested order of attack
1. Get a single object round-tripping in memory first to prove the routing
   (`PUT`/`GET`/`DELETE /{bucket}/{key}`), then immediately rip out the in-memory
   buffer.
2. Build V1's CAS: hash → sharded path → temp→fsync→rename→fsync-dir. Unit-test
   that the same bytes commit to one path and that an interrupted commit never
   appears under the final name.
3. Wire V2 streaming through V1: stream the PUT body to a temp file hashing as
   you go, stream the GET back. Upload something bigger than RAM and watch memory
   stay flat. Add the size cap and the disconnect cleanup.
4. Build V3: the `(bucket,key)→digest` index with the blob-then-pointer order,
   then `ListObjectsV2` with prefix/delimiter/pagination, then delete + the GC.
   Prove dedup (two keys, one blob) and that delete doesn't drop shared bytes.
5. Build V4 multipart: initiate/uploadpart/complete/abort, assembly in order,
   and the multipart ETag. Verify against `aws s3 cp` of a large file.
6. Add `Range`/conditional GET, auth + caps + traversal guards, switch
   `ListBucketResult` to S3 XML, add the metrics, then benchmark and document.

## Run it
```bash
cp .env.example .env         # then set DATA_DIR (where blobs live) etc.
cargo run -p object-store
#   The scaffold compiles and serves. `GET /healthz` is fine; the first real
#   PUT/GET/list hits a todo!() in V1/V2/V3 — that panic is your worklist.

# Create a bucket and (once V1/V2 are done) round-trip an object:
curl -X PUT  localhost:9000/my-bucket
curl -X PUT  localhost:9000/my-bucket/hello.txt --data-binary @hello.txt
curl         localhost:9000/my-bucket/hello.txt

# The gold standard once you're S3-compatible — point the real AWS CLI at it:
aws --endpoint-url http://localhost:9000 s3 cp ./big.bin s3://my-bucket/big.bin
```

## 🔬 From the field

<!-- Adoption backlog distilled from RESEARCH.md by /harvest. NOT graded:
     [~] = open, [✔] = adopted — not counted toward graded progress;
     shown under FROM THE FIELD in status detail.
     Tick a box when the idea has actually landed in this project. -->

### API & protocol extras

- [✔] Conditional writes: `PUT` with `If-None-Match: *` is atomic create-once
  (two racing creators → exactly one 200, the loser gets 412) and
  `If-Match: <etag>` is compare-and-swap — the primitive that lets the store
  double as a lock service / commit pointer *(→ RESEARCH.md §Part 7; proof:
  `src/index.rs` `Precondition` + conditional-write tests in `src/routes.rs`)*

- [✔] Checksum-validated uploads: a PUT that declares a checksum (`Content-MD5`
  / `x-amz-checksum-*`) not matching the streamed bytes is rejected and leaves
  nothing durable *(→ RESEARCH.md §Part 4; proof: `src/streaming.rs`
  `CheckSumAlgorithm::verify` + streaming checksum tests)*

- [✔] Object versioning: an overwrite is a new immutable version behind an
  atomic pointer flip — the previous version stays retrievable by version id,
  and delete becomes a removable delete marker *(→ RESEARCH.md §Part 1; proof:
  `src/object.rs` `VersionKind::DeleteMarker` + `?versionId=` GET/HEAD/DELETE
  tests in `src/routes.rs`)*

- [~] Session-scoped auth (the Express One Zone trick): a `CreateSession`-style
  endpoint mints a short-lived scoped token so the hot path skips per-request
  HMAC verification — auth cost is paid once per session, not per request
  *(→ RESEARCH.md §Part 7)*
  
- [✔] Lifecycle rules: objects expire (or migrate to a compressed cold tier)
  after a configured age, and a GET of a tiered object still round-trips
  transparently *(→ RESEARCH.md §Part 5; proof: `src/lifecycle.rs` +
  `tests/lifecycle_acceptance.rs` + `bench/hot_vs_cold`)*

- [✔] Interop beyond the AWS CLI: the Rust `object_store` crate (Arrow's)
  performs put/get/list/multipart against your endpoint unpatched
  *(→ RESEARCH.md §Part 6, Recommendations 4; proof: `tests/object_store_interop.rs`)*

- [~] A Mountpoint-style FUSE veneer: the bucket mounts as a read-only
  filesystem whose reads are served by ranged GETs — file API on top, object
  semantics underneath *(→ RESEARCH.md §Part 7; teach-yourself:
  [`docs/03-how-fuse-mountpoint-works.md`](docs/03-how-fuse-mountpoint-works.md))*

### Storage-engine labs

- [~] Erasure-coding lab: RS(4,2) over GF(2⁸) with log/antilog tables — a blob
  split into 6 shards reconstructs bit-exact after any 2 are deleted
  *(→ RESEARCH.md §Part 3)*
  
- [~] Local Reconstruction Codes on top of the RS lab: with (k, l, r) local
  groups, repairing a single lost shard reads only its local group (≈ k/l
  shards), not all k — measure the repair-read fan-in both ways
  *(→ RESEARCH.md §Part 3)*
- [~] Your own durability number: a calculator that turns (k, m, per-shard
  annual failure rate, repair window) into nines, Backblaze-style, with the
  result and its assumptions in the bench doc *(→ RESEARCH.md §Part 3)*
- [~] Small-object packing (Haystack "needles"): thousands of tiny objects
  occupy a handful of append-only volume files instead of one file each, and
  GET still streams each one correctly *(→ RESEARCH.md §Part 6)*
- [✔] Transparent compression: blobs are Zstd-compressed at rest with dedup
  intact, and the design doc states the hash-then-compress vs compress-then-hash
  choice and why *(→ RESEARCH.md §Part 6; proof: cold-tier zstd in
  `src/lifecycle.rs`, hash-then-compress rationale in its module docs —
  compression applies to lifecycle-tiered blobs, not the hot tree)*
- [~] Chunk-level dedup (content-defined chunking): two large objects differing
  by a small edit share most of their on-disk bytes — whole-object dedup only
  ever shares identical files *(→ RESEARCH.md §Part 6)*

### Architecture labs

- [✔] Index-as-a-service: the S3 front-end and the `(bucket,key)→blob` index run
  as two processes; with `INDEX_URL` set, PUT/GET/list still round-trip; killing
  the index process fails metadata ops cleanly while blob files may already
  exist (distributed blob-then-pointer)
  *(→ RESEARCH.md §Part 2; `src/index_backend.rs`, `src/index_server.rs`,
  `object-store-index` bin; teach-yourself:
  [`docs/05-how-index-as-a-service-works.md`](docs/05-how-index-as-a-service-works.md))*

### Durability & correctness practice

- [✔] Property-based tests attack every vertical's invariant with random inputs
  (naming safety, chunking-independent digests, listing/GC laws, the multipart
  ETag) — `tests/property.rs` *(→ RESEARCH.md §Recommendations 5)*
- [✔] Reference-model checking (the ShardStore method): the same random op
  sequence drives the real store and a tiny in-memory model, and their
  observable state never diverges — `tests/reference_model.rs`
  *(→ RESEARCH.md §Part 2 & 8)*
- [✔] Continuous scrubbing: a background auditor re-hashes stored blobs; a
  deliberately flipped byte on disk is detected, quarantined, and surfaced as a
  metric before any reader is served the corrupt bytes *(→ RESEARCH.md §Part 4;
  proof: `src/store.rs` `Scrubber` + quarantine gate on reads; teach-yourself:
  [`docs/04-how-continuous-scrubbing-works.md`](docs/04-how-continuous-scrubbing-works.md))*
- [~] A crash-injection harness: property tests kill the commit sequence at
  every step boundary (not one hand-picked `kill -9`) and assert every reachable
  post-crash state is all-or-nothing *(→ RESEARCH.md §Part 8)*
- [~] The GC ↔ in-flight-PUT race under a model checker: the stated resolution
  is exercised with Loom (exhaustive) or Shuttle (randomized) interleavings,
  not just reasoned about *(→ RESEARCH.md §Part 2 & 8; teach-yourself:
  [`docs/08-how-loom-and-shuttle-work.md`](docs/08-how-loom-and-shuttle-work.md)
  + intro demos in `tests/loom_shuttle_intro.rs`)*
- [✔] A durability review for the commit path: a written threat list ("think
  like an adversary") with the guardrail that answers each threat, kept next to
  the design doc *(→ RESEARCH.md §Part 4; proof:
  [`docs/07-durability-review.md`](docs/07-durability-review.md) — blob publish,
  version pointer flip, cold-tier migration)*
