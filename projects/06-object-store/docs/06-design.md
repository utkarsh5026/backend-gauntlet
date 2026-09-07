# 06 — Design

The graded design doc: the on-disk layout, the exact durable-commit sequence and
why each `fsync` is where it is, the index format and the blob-then-pointer
invariant, the prefix/delimiter listing algorithm, multipart assembly and the two
ETag formulas, the GC design including the in-flight-PUT race, and — new on the
Python side — which operations run on the event loop and which run in the thread
pool, and why.

---

## 1. On-disk layout

`DATA_DIR` is the whole database. Nothing else is stateful.

```text
DATA_DIR/
├── objects/ab/cd/<64-hex>     FileCas blobs, one file per digest
├── volumes/<uuid>.dat         Haystack needles (packed small objects)
│   ├── needles.json           snapshot of the needle index
│   └── needles.log            append-only WAL for that index
├── cold/ab/cd/<64-hex>.zst    lifecycle-tiered blobs
├── quarantine/<64-hex>        blobs the scrubber caught failing their hash
├── tmp/                       in-flight blob writes, staged
├── uploads/<upload-id>/       multipart staging: session.json + NNNNN.part
└── index/<bucket>/
    ├── metadata.json          bucket-level state (creation date, lifecycle)
    ├── objects/<enc-key>.json one row per key: its version history
    └── tmp/                   in-flight index rows, staged
```

### Why two shard levels

`objects/<64-hex>` in one flat directory melts at a few million entries: ext4's
htree lookups degrade, `readdir` becomes a full scan, and every tool that walks
it — including our own GC — stalls. Sharding on the first two bytes gives
256 × 256 = 65,536 leaves, so ten million blobs sit ~150 per directory.

One level would be a 256× cut, which the same ten million blobs blow through at
~39,000 entries each. Three levels is 16.7M directories, mostly empty, and the
inode cost of the *tree* starts to rival the blobs. Two is what the digest gives
you for free: the hash is uniform, so the shards are uniform, with no
rebalancing and no hot directory.

### Why the index is one file per key

The atomic unit of update is a key. One file per key gets per-key durability out
of `rename` with no write-ahead log and no global lock, and two writers touching
different keys never contend. The filename is `naming.encode_key` — every byte
outside the RFC 3986 unreserved set becomes `%xx`, `/` included — so a key
containing `../` lands as one flat filename with no separators in it at all. The
traversal defence is *removing the concept of a path*, not inspecting for `..`.

---

## 2. The durable commit, and what each fsync defends

```python
os.fsync(temp_fd)
dest.parent.mkdir(parents=True, exist_ok=True)
os.replace(temp, dest)
fsync(dest.parent)
```

| Step | Defends against |
|---|---|
| `fsync(temp)` | A crash after the rename but before the *bytes* reach the platter. The name would exist, pointing at page-cache contents that are gone. |
| `mkdir -p` | A rename failing halfway through publishing because the shard directory does not exist yet. |
| `rename` | Any observable in-between state. `rename` is atomic **within one filesystem**: `dest` flips from absent to complete, so no reader ever sees a truncated blob under its final name. |
| `fsync(parent)` | A crash rewinding the rename even though the file's own bytes were synced. The *name* lives in the directory, not in the file, and a directory entry is dirty page cache like anything else. |

The rename is why the temp must be on the same filesystem as the destination: across
two, `rename` fails outright with `EXDEV`. The index's per-bucket `tmp/` already
is; `durable.atomic_write_sibling` guarantees it by construction by staging next
to the destination.

The failure this prevents is uniquely nasty in a content-addressed store: a
truncated blob has the *right name*, so every future reader trusts it, and the
only thing that can detect it is re-hashing (which is what the scrubber does,
after the fact).

---

## 3. Blob-then-pointer

V2 commits the blob **before** V3 records the pointer. Always.

- Crash in between → an unreferenced blob. Garbage; the GC reclaims it. Costs
  disk.
- Reversed → a key pointing at a blob that does not exist. The object is
  permanently unreadable, nothing on disk explains why, and no amount of later
  repair recovers the bytes. Costs *data*.

One direction is a cleanup problem, the other is corruption. That asymmetry is
the entire reason the ordering is a contract rather than a preference.

The index-as-a-service split (`INDEX_URL`) makes the invariant visible: with the
index in another process, the ordering stops being something one function can
accidentally reverse and becomes an ordering between two systems. Kill the index
mid-PUT and you get exactly the predicted failure — an orphan blob, never a
dangling key.

### The per-key lock

`Index.put` holds a per-key `asyncio.Lock` across read-current → check
precondition → write pointer. Without it the classic lost update applies: two
writers read the same history, both append, the second wins, and the first
version vanishes from the history it should have joined. `If-Match` would also
degrade from compare-and-swap into a compare *then* a swap with a gap between.

The lock is cheap because the blob is already durable by then: it spans a small
JSON read and an atomic rename, not the upload. That is what stops a 5 GB PUT
from blocking every other writer to the same key for five minutes.

---

## 4. Listing: the folder illusion

The keyspace is flat. `ListObjectsV2` invents a tree per request:

1. Read every row in the bucket; keep the live ones under `prefix`. A delete
   marker at `latest` is not listed — the key reads as absent.
2. With a `delimiter`, split the remainder *after* `prefix` at its first
   occurrence. Keys with one become a common prefix (the folder); keys without
   stay objects (the files).
3. Merge objects and prefixes into **one** list sorted by name. S3 orders them
   together, and a client paging through must see a single ordered sequence.
4. Drop everything at or before the continuation token, cut at `max_keys`, and
   return the last name kept as the next token.

The token is the last *name*, not an offset. An offset into a list that changes
between pages skips or repeats keys; "resume after this name" stays correct
across concurrent writes.

---

## 5. Multipart, and the two ETag formulas

A session is `uploads/<upload-id>/`: a `session.json` recording the target
bucket, key and content type, plus one `NNNNN.part` per part. The session file
is what lets a `Complete` succeed after a process restart, which for an upload
that legitimately takes hours is not a corner case.

`Complete` sorts by part number **first** — the client's list arrives in
whatever order its threads finished, and concatenating in that order produces a
corrupt object with a perfectly valid-looking ETag. Each part's staged MD5 is
re-verified against the client's claim as it is read, which catches the client
that retried part 3 onto different bytes and is completing with a stale ETag.

| | Formula |
|---|---|
| Single PUT | `md5(bytes).hexdigest()` |
| Multipart | `md5(concat(raw md5 of each part, in part order)).hexdigest() + "-" + N` |

Two things follow. The `-N` suffix is how a client knows the object was
multipart and must not verify it by re-hashing the bytes — without it every
SDK's integrity check fails on every large object. And the value is computable
without reading the object back, which is why it can be returned the instant
assembly finishes.

The way to get this wrong is to hash the parts' *hex* digests instead of their
raw 16 bytes. The result is well-formed, plausible, and rejected by the AWS SDK.

---

## 6. GC, and the in-flight-PUT race

Mark: every digest referenced by a committed index row **and** by an in-flight
row staged under a bucket's `tmp/`. For a `BlobKind.MANIFEST` row, expand to
every chunk the manifest names — miss that and shared chunks get reaped while
another key still needs them, silently corrupting objects nobody touched.

Sweep: walk the blob tree, remove anything unreferenced *and* older than the
grace window.

The race: a PUT commits its blob, then writes its index row. A GC running in
that gap sees an unreferenced blob and, naively, deletes an upload that is about
to succeed. **Two independent guards close it:**

- The **tmp scan** catches writes whose row is staged but not yet renamed.
- The **grace window** (60 s in production, 0 in tests) catches the smaller gap
  before staging.

Either alone leaves a hole, which is why both are there. Tests run with grace at
zero specifically so the tmp scan is the thing under test rather than a sleep.

A corrupt row under `tmp/` is skipped rather than fatal — a half-written temp is
the *expected* shape of a crash mid-PUT, and letting one wedge the collector
would mean a single bad crash stops all reclamation forever. A corrupt
*committed* row is a different matter and propagates.

---

## 7. Tiering: hash-then-compress

A blob is named by the SHA-256 of its bytes. Compress the bytes and the hash
changes, so tiering is a fork the design has to defend:

- *compress-then-hash* — the cold file is named by the compressed hash. Breaks
  dedup (the same plaintext compresses differently under different settings) and
  orphans every index entry pointing at the old digest. **No.**
- *hash-then-compress* — identity stays the **plaintext** digest. Compression
  becomes a physical *encoding* of a blob rather than a new identity, and the
  digest, ETag and dedup key never move. **This one.**

So a blob has one fixed logical digest and a physical representation that
tiering flips between `objects/<h>` (raw) and `cold/…/<h>.zst`. `Lifecycle.locate`
resolves which before opening, and that indirection is the entire "transparent"
in transparent tiering.

The migration mirrors the PUT dance: compress into a temp beside the cold path,
fsync, rename, fsync the directory, and only *then* unlink the hot copy. A crash
between the rename and the unlink leaves both copies — harmless, reads still
work, the next sweep finishes it. Unlink first and a crash before the cold copy
is durable destroys the object.

### The subtlety: decisions are per-object, transforms are per-blob

`last_modified` lives on the version, but a blob is **shared** across keys by
dedup. A blob can back a 90-day-old key *and* one written this morning. So a blob
is cold-eligible only when its **youngest referrer** is past `tier_after_days` —
a `max(last_modified)` over everything pointing at it. Compute it per key and you
freeze objects that are being actively read, and the only symptom is a latency
regression nobody can explain.

Cold reads cannot seek. A ranged GET into a tiered blob decodes from byte zero
and discards the prefix. That is the cold tier's real price, and it belongs in
this document rather than in someone's incident notes.

---

## 8. The event loop and the thread pool

`fsync` has no asynchronous form on Linux. Every runtime's "async file I/O",
tokio's included, is a thread pool wearing a nicer face. This port makes that
explicit rather than hiding it, so the boundary is visible at the call site.

**On the loop:** request parsing, routing, the per-key lock, JSON
(de)serialisation of index rows, and every `await` between chunks.

**In the thread pool** (`asyncio.to_thread`), deliberately:

| Operation | Why |
|---|---|
| Every chunk write in the stream loop | It is the write *and* the backpressure — the loop does not ask for chunk N+1 until N is on disk |
| Every chunk read in the response body | Same, downstream |
| `publish_temp` (three fsyncs and a rename) | Blocking by nature |
| The scrubber's re-hash pass | Reads and hashes gigabytes; belongs nowhere near the loop |
| Directory walks (GC sweep, occupancy scans) | `readdir` over 65,536 shards is not instant |
| Haystack commit, compaction, checkpoint | Append + fsync + index rewrite |

**The tuning knob is the chunk size, and it is coupled to the pool size.** A
64 KiB chunk over a 5 GB object is 80,000 round trips through the pool. The
default `ThreadPoolExecutor` is `min(32, cpu_count + 4)` workers, which is
plenty of *concurrency* but does nothing about per-hop overhead. Raising the
chunk size cuts hops at the cost of memory per in-flight request; the current
256 KiB (upload) / 64 KiB (download) is a starting point, not a measured
optimum — see `docs/06-benchmarks.md`.

**Nothing CPU-bound stays on the loop.** Both hashers run inside the same
`to_thread` call as the write, and `hashlib` releases the GIL on large buffers,
so hashing genuinely overlaps. What does not overlap is the interpreter overhead
per chunk, which is why the profile matters more than the throughput figure.

---

## 9. What is deliberately not here

- **SigV4.** Auth is a simplified access-key/HMAC scheme plus presigned URLs.
  Real SigV4 signs headers, the payload hash and a scoped derived key; that is
  project 25's problem, and the HMAC brain is the same.
- **Erasure coding on the request path.** `erasure/` is a working codec lab —
  RS(4,2), LRC, the nines calculator — but nothing there stores a byte. Wiring
  shards under `objects/` is a separate problem with its own placement and
  repair design.
- **A real third-party interop test.** The XML shape is asserted against this
  project's own parser, which is strictly weaker than it sounds: a schema can be
  self-consistently wrong. Closing that needs a client nobody here wrote.
